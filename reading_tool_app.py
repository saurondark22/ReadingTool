import logging
import os
import signal
import subprocess
import sys
import threading
import time

from pynput import keyboard as pykeyboard
from PySide6 import QtCore, QtGui, QtWidgets

from config_manager import ConfigManager
from selection import SelectionCapture, SelectionHolder
from tts_engine import WorkerProcess, download_models, models_present


class ReadingToolApp(QtWidgets.QApplication):
    hotkey_triggered_signal = QtCore.Signal()

    def __init__(self, argv):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)
        logging.debug("Initializing ReadingToolApp (tray-only daemon)")

        self.config_manager = ConfigManager()
        self.config = self.config_manager.config

        self.capture = SelectionCapture()
        self.tray_icon = None
        self.tray_menu = None
        self.registered_hotkey = None
        self.hotkey_listener = None
        self.current_text_holder = None
        self._read_active = False
        self._model_downloading = False
        self._active_workers = []
        self._stop_action = None

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
        """Kill a worker immediately (Stop)."""
        if worker in self._active_workers:
            self._active_workers.remove(worker)
        worker.kill()

    def _stop_active_read(self):
        """Stop any active worker (tray Stop item / hotkey toggle)."""
        for w in list(self._active_workers):
            self.stop_read(w)
        self._read_active = False
        self._update_stop_enabled()

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
        self._stop_action = self.tray_menu.addAction("Stop")
        self._stop_action.triggered.connect(self._stop_active_read)
        self._stop_action.setEnabled(False)
        settings_action = self.tray_menu.addAction("Settings")
        settings_action.triggered.connect(self._open_settings_file)
        quit_action = self.tray_menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)

    def _update_stop_enabled(self):
        if self._stop_action is not None:
            self._stop_action.setEnabled(self._read_active)

    def _open_settings_file(self):
        """Open config.json in the system's default editor."""
        path = self.config_manager.config_path
        try:
            # ponytail: xdg-open is the native Linux "open with default
            # app" — no dependency, no GUI toolkit needed.
            subprocess.Popen(
                ["xdg-open", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.tray_icon.showMessage(
                "ReadingTool",
                "xdg-open not found; edit config.json manually.",
                QtWidgets.QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
        except Exception as e:
            logging.error(f"Failed to open settings: {e}", exc_info=True)

    def _notify(self, title, message, ms=2500):
        """Native tray balloon — status surfacing with no window."""
        if self.tray_icon is not None:
            self.tray_icon.showMessage(
                title,
                message,
                QtWidgets.QSystemTrayIcon.MessageIcon.Information,
                ms,
            )

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

        # Toggle: if a Read is active -> stop. Otherwise begin a Read.
        if self._read_active:
            self._stop_active_read()
            self._notify("ReadingTool", "Stopped")
            return

        self._begin_read()

    # --- read lifecycle --------------------------------------------------
    def _begin_read(self):
        self._read_active = True
        self._update_stop_enabled()
        self.current_text_holder = SelectionHolder()
        self.capture.capture_async(self.current_text_holder)
        self._notify("ReadingTool", "Loading…")

        def _wait_and_play():
            if not self.current_text_holder.ready.wait(timeout=3.0):
                logging.warning("Timed out waiting for selected text capture")
            text = (
                self.current_text_holder.text if self.current_text_holder else ""
            ) or ""
            # ponytail: block until models are present rather than spawning a
            # worker that crashes on a missing file. The background download
            # flips _model_downloading to False when done; poll until then.
            if not models_present():
                self._notify(
                    "ReadingTool",
                    "Model still downloading — please wait…",
                    ms=4000,
                )
                deadline = time.time() + 300
                while not models_present() and time.time() < deadline:
                    if not self._model_downloading:
                        # Download ended but file still absent — kick it again.
                        self._model_downloading = True
                        try:
                            download_models()
                        except Exception as e:
                            logging.error(f"Model download failed: {e}", exc_info=True)
                            self._notify(
                                "ReadingTool",
                                "Model download failed — check logs.",
                                ms=5000,
                            )
                            self._read_active = False
                            self._update_stop_enabled()
                            return
                        finally:
                            self._model_downloading = False
                    time.sleep(1.0)
                if not models_present():
                    self._notify(
                        "ReadingTool", "Model not available — timed out.", ms=4000
                    )
                    self._read_active = False
                    self._update_stop_enabled()
                    return
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

        threading.Thread(target=_wait_and_play, daemon=True).start()

    @QtCore.Slot(str, str, float)
    def _auto_start_playback(self, text, voice, speed):
        if not text.strip():
            self._notify("ReadingTool", "No text was selected.")
            self._read_active = False
            self._update_stop_enabled()
            return
        self.start_read(text, voice, speed, on_event=self._on_worker_event)

    def _on_worker_event(self, evt):
        # Called from the worker's reader thread — bounce to Qt main thread.
        event = evt.get("event", "")
        msg = evt.get("msg", "")
        QtCore.QMetaObject.invokeMethod(
            self,
            "_handle_worker_event",
            QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(str, event),
            QtCore.Q_ARG(str, msg),
        )

    @QtCore.Slot(str, str)
    def _handle_worker_event(self, event, msg):
        if event == "loading":
            self._notify("ReadingTool", "Loading engine…")
        elif event == "synthesizing":
            self._notify("ReadingTool", "Synthesizing…")
        elif event == "playing":
            self._notify("ReadingTool", "Playing…")
        elif event == "done":
            self._notify("ReadingTool", "Done")
            self._read_active = False
            self._update_stop_enabled()
            self._clear_finished_workers()
        elif event == "error":
            self._notify("ReadingTool", f"Error: {msg}" if msg else "Error", ms=4000)
            self._read_active = False
            self._update_stop_enabled()
            self._clear_finished_workers()

    def _clear_finished_workers(self):
        """Drop references to workers that have exited so the process
        table and any buffered stderr drain fully."""
        self._active_workers = [w for w in self._active_workers if w.is_running]

    # --- model download (background, daemon doesn't load engine) ---------
    def _maybe_download_models(self):
        if models_present():
            return
        self._model_downloading = True
        logging.info("Models absent; downloading in background (daemon stays light)")
        self._notify("ReadingTool", "Downloading model (~88MB) in background…", ms=4000)

        def _dl():
            try:
                download_models()
                self._notify("ReadingTool", "Model download complete.")
            except Exception as e:
                logging.error(f"Model download failed: {e}", exc_info=True)
                self._notify(
                    "ReadingTool", "Model download failed — check logs.", ms=5000
                )
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
