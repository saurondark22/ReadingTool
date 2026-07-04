from PySide6 import QtCore, QtWidgets


class PlaybackWindow(QtWidgets.QWidget):
    """Transient foreground window shown on hotkey press. Displays the
    captured Selection text, auto-starts the Read (spawning the TTS Worker),
    and exposes transport controls (Play, Stop, Speed). The worker process
    is killed on Stop or window close — there is no Pause/Resume."""

    state_changed = QtCore.Signal(str)  # idle|loading|playing|done|error

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.selected_text = ""
        self._clean_text = None
        self._show_clean_text = bool(app.config.get("show_clean_text", False))
        self._state = "idle"
        self._speed = float(app.config.get("speed", 1.0))
        self._voice = app.config.get("voice", "af_heart")
        self._worker = None
        self.setWindowTitle("ReadingTool")
        self.setWindowFlags(
            QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setMinimumWidth(420)
        self._build_ui()
        self._apply_state_buttons()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        self.text_label = QtWidgets.QLabel("Reading…")
        self.text_label.setWordWrap(True)
        self.text_label.setMaximumHeight(120)
        self.text_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.text_label)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: gray; font-style: italic;")
        layout.addWidget(self.status_label)

        # Speed slider row
        speed_row = QtWidgets.QHBoxLayout()
        speed_row.addWidget(QtWidgets.QLabel("Speed:"))
        self.speed_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.speed_slider.setRange(50, 200)
        self.speed_slider.setValue(int(self._speed * 100))
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        speed_row.addWidget(self.speed_slider)
        self.speed_value_label = QtWidgets.QLabel(f"{self._speed:.2f}x")
        self.speed_value_label.setMinimumWidth(48)
        speed_row.addWidget(self.speed_value_label)
        layout.addLayout(speed_row)

        # Transport buttons
        btn_row = QtWidgets.QHBoxLayout()
        self.play_btn = QtWidgets.QPushButton("Play")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        btn_row.addWidget(self.play_btn)
        btn_row.addWidget(self.stop_btn)
        layout.addLayout(btn_row)

        self.play_btn.clicked.connect(self._on_play)
        self.stop_btn.clicked.connect(self._on_stop)

    # --- state -----------------------------------------------------------
    def _set_state(self, state):
        self._state = state
        self.state_changed.emit(state)
        self._apply_state_buttons()

    def _apply_state_buttons(self):
        s = self._state
        self.play_btn.setEnabled(s in ("idle", "done", "error"))
        self.stop_btn.setEnabled(s in ("loading", "playing"))

    def set_status(self, text):
        self.status_label.setText(text)

    def set_selected_text(self, text):
        self.selected_text = text
        self._clean_text = None
        self._render_preview()

    def _render_preview(self):
        """Show the Clean Text if the diagnostic toggle is on and it has
        arrived; otherwise the raw Selection, exactly as before."""
        if self._show_clean_text and self._clean_text is not None:
            source = self._clean_text
            label = "Cleaned"
        else:
            source = self.selected_text
            label = None
        preview = source if len(source) <= 500 else source[:500] + "…"
        if label:
            self.text_label.setText(f"[{label}]\n{preview}")
        else:
            self.text_label.setText(preview)
        self.setToolTip(self.selected_text)

    # --- controls --------------------------------------------------------
    def _on_speed_changed(self, val):
        self._speed = val / 100.0
        self.speed_value_label.setText(f"{self._speed:.2f}x")
        self.app.config_manager.set("speed", self._speed)

    def _on_play(self):
        if not self.selected_text.strip():
            self.set_status("No text selected.")
            return
        self._spawn_worker()

    def _on_stop(self):
        self._kill_worker()
        self._set_state("idle")
        self.set_status("Stopped")

    # --- worker lifecycle ------------------------------------------------
    def auto_start(self, text, voice, speed):
        self.set_selected_text(text)
        self._voice = voice
        self._speed = speed
        self.speed_slider.setValue(int(speed * 100))
        if not text.strip():
            self.set_status("No text was selected.")
            self._set_state("idle")
            return
        self._spawn_worker()

    def _spawn_worker(self):
        self._kill_worker()
        self._set_state("loading")
        self.set_status("Loading engine…")
        self._worker = self.app.start_read(
            self.selected_text, self._voice, self._speed, on_event=self._on_worker_event
        )

    def _kill_worker(self):
        if self._worker is not None:
            self.app.stop_read(self._worker)
            self._worker = None

    def _on_worker_event(self, evt):
        # Called from the worker's reader thread — bounce to Qt main thread.
        # The "cleaned" event carries Clean Text under the "text" key; all
        # other events use "msg".
        event = evt.get("event", "")
        if event == "cleaned":
            msg = evt.get("text", "")
        else:
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
            self.set_status("Loading engine…" if not msg else msg)
        elif event == "cleaned":
            # msg carries the Clean Text (post-Cleaner, pre-Phonemizer).
            if self._show_clean_text:
                self._clean_text = msg
                self._render_preview()
        elif event == "synthesizing":
            self.set_status("Synthesizing…")
        elif event == "playing":
            self._set_state("playing")
            self.set_status("Playing…")
        elif event == "done":
            self._set_state("done")
            self.set_status("Done")
            self._worker = None
        elif event == "error":
            self._set_state("error")
            self.set_status(f"Error: {msg}" if msg else "Error")
            self._worker = None

    def closeEvent(self, event):
        # Closing the window kills the worker (Stop) — no orphaned audio.
        self._kill_worker()
        super().closeEvent(event)
