import logging
import os
import signal
import sys
import time

import darkdetect
from pynput import keyboard as pykeyboard
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtGui import QCursor, QGuiApplication

from config_manager import ConfigManager
from playback_window import PlaybackWindow
from selection import SelectionCapture, SelectionHolder
from settings_window import SettingsWindow
from tts_engine import WorkerProcess, download_models, models_present


class ReadingToolApp(QtWidgets.QApplication):
    hotkey_triggered_signal = QtCore.Signal()

    def __init__(self, argv):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)
        logging.debug("Initializing ReadingToolApp (light daemon)")

        self.config_manager = ConfigManager()
        self.config = self.config_manager.config

        self.capture = SelectionCapture()
        self.playback_window = None
        self.tray_icon = None
        self.tray_menu = None
        self.settings_window = None
        self.registered_hotkey = None
        self.hotkey_listener = None
        self.current_text_holder = None
        self._read_active = False
        self._model_downloading = False
        self._active_workers = []

        self._init_tray()
        self._register_hotkey()
        self._setup_sigint()
        self._maybe_download_models()

        self.recent_triggers = []
        self.TRIGGER_WINDOW = 1.5
        self.MAX_TRIGGERS = 3

    # --- TTS worker management -------------------------------------------
    def start_read(self, text, voice, speed, on_event):
        """Spawn a TTS Worker for one Read. Returns the WorkerProcess."""
        debug_mode = bool(self.config.get("debug_mode", False))
        worker = WorkerProcess(on_event=on_event)
        worker.start(text, voice, speed, debug_mode=debug_mode)
        self._active_workers.append(worker)
        return worker

    def stop_read(self, worker):
        """Kill a worker immediately (Stop / close window)."""
        if worker in self._active_workers:
            self._active_workers.remove(worker)
        worker.kill()

    # --- tray ------------------------------------------------------------
    def _init_tray(self):
        icon_path = os.path.join(os.path.dirname(sys.argv[0]), "icons", "app_icon.png")
        if os.path.exists(icon_path):
            self.tray_icon = QtWidgets.QSystemTrayIcon(QtGui.QIcon(icon_path), self)
        else:
            self.tray_icon = QtWidgets.QSystemTrayIcon(self)
        self.tray_icon.setToolTip("ReadingTool")
        self.tray_menu = QtWidgets.QMenu()
        self.tray_icon.setContextMenu(self.tray_menu)
        self._update_tray_menu()
        self.tray_icon.show()

    def _update_tray_menu(self):
        self.tray_menu.clear()
        self._apply_theme(self.tray_menu)
        settings_action = self.tray_menu.addAction("Settings")
        settings_action.triggered.connect(self._show_settings)
        quit_action = self.tray_menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)

    @staticmethod
    def _apply_theme(menu):
        if darkdetect.isDark():
            palette = menu.palette()
            palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#2d2d2d"))
            palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#ffffff"))
            menu.setPalette(palette)

    def _show_settings(self):
        self.settings_window = SettingsWindow(self)
        self.settings_window.show()

    # --- hotkey ----------------------------------------------------------
    @staticmethod
    def _to_pynput_hotkey(hotkey_str):
        return "+".join(
            f"{t}" if len(t) <= 1 else f"<{t}>" for t in hotkey_str.split("+")
        )

    def _register_hotkey(self):
        try:
            if self.hotkey_listener is not None:
                self.hotkey_listener.stop()
                self.hotkey_listener = None
            shortcut = self.config.get("shortcut", "ctrl+alt+r")
            parsed = self._to_pynput_hotkey(shortcut)
            pykeyboard.HotKey.parse(parsed)
            self.registered_hotkey = shortcut

            def on_activate():
                logging.debug("Hotkey pressed")
                self.hotkey_triggered_signal.emit()

            self.hotkey_listener = pykeyboard.GlobalHotKeys({parsed: on_activate})
            self.hotkey_listener.start()
            logging.debug(f"Registered hotkey: {parsed}")
        except Exception as e:
            logging.error(f"Failed to register hotkey: {e}")

    def reregister_hotkey(self):
        self._register_hotkey()

    def on_hotkey_pressed(self):
        if self._check_trigger_spam():
            logging.warning("Hotkey spam detected - quitting")
            self._quit()
            return

        # Toggle: if a Read is active OR the window is open -> stop + close.
        if self._read_active or (
            self.playback_window and self.playback_window.isVisible()
        ):
            self._stop_and_close()
            return

        self._begin_read()

    # --- read lifecycle --------------------------------------------------
    def _begin_read(self):
        self._read_active = True
        self.current_text_holder = SelectionHolder()
        self.capture.capture_async(self.current_text_holder)
        self._show_playback_window()

        def _wait_and_play():
            if not self.current_text_holder.ready.wait(timeout=3.0):
                logging.warning("Timed out waiting for selected text capture")
            text = (
                self.current_text_holder.text if self.current_text_holder else ""
            ) or ""
            voice = self.config.get("voice", "af_heart")
            speed = float(self.config.get("speed", 1.0))
            QtCore.QMetaObject.invokeMethod(
                self,
                "_auto_start_playback",
                QtCore.Qt.ConnectionType.QueuedConnection,
                QtCore.Q_ARG(str, text),
                QtCore.Q_ARG(str, voice),
                QtCore.Q_ARG(float, speed),
            )

        import threading

        threading.Thread(target=_wait_and_play, daemon=True).start()

    @QtCore.Slot(str, str, float)
    def _auto_start_playback(self, text, voice, speed):
        if self.playback_window is None:
            return
        self.playback_window.auto_start(text, voice, speed)

    def _show_playback_window(self):
        if self.playback_window is not None:
            if self.playback_window.isVisible():
                self.playback_window.close()
            self.playback_window = None
        self.playback_window = PlaybackWindow(self)
        self.playback_window.state_changed.connect(self._on_playback_state)

        cursor_pos = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor_pos) or QGuiApplication.primaryScreen()
        self.playback_window.show()
        self.playback_window.adjustSize()
        self.playback_window.activateWindow()

        geom = screen.geometry()
        x = min(cursor_pos.x(), geom.right() - self.playback_window.width())
        y = min(cursor_pos.y() + 20, geom.bottom() - self.playback_window.height() - 10)
        self.playback_window.move(max(x, geom.left()), max(y, geom.top()))

    @QtCore.Slot(str)
    def _on_playback_state(self, state):
        if state in ("done", "error", "idle") and self._read_active:
            self._read_active = False

    def _stop_and_close(self):
        if self.playback_window is not None and self.playback_window.isVisible():
            self.playback_window._on_stop()
            self.playback_window.close()
        self._read_active = False

    # --- model download (background, daemon doesn't load engine) ---------
    def _maybe_download_models(self):
        if models_present():
            return
        self._model_downloading = True
        logging.info("Models absent; downloading in background (daemon stays light)")

        import threading

        def _dl():
            try:
                download_models()
            except Exception as e:
                logging.error(f"Model download failed: {e}", exc_info=True)
            finally:
                self._model_downloading = False

        threading.Thread(target=_dl, daemon=True).start()

    # --- spam guard (mirrors WritingTool) --------------------------------
    def _check_trigger_spam(self):
        now = time.time()
        self.recent_triggers.append(now)
        self.recent_triggers = [
            t for t in self.recent_triggers if now - t <= self.TRIGGER_WINDOW
        ]
        return len(self.recent_triggers) >= self.MAX_TRIGGERS

    # --- lifecycle -------------------------------------------------------
    def _setup_sigint(self):
        signal.signal(signal.SIGINT, lambda *_: self._quit())
        self._sigint_timer = QtCore.QTimer()
        self._sigint_timer.start(100)
        self._sigint_timer.timeout.connect(lambda: None)

    def _quit(self):
        logging.debug("Exiting")
        try:
            if self.hotkey_listener is not None:
                self.hotkey_listener.stop()
        except Exception:
            pass
        for w in list(self._active_workers):
            w.kill()
        self.quit()
