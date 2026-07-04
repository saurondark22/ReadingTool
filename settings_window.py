from PySide6 import QtCore, QtWidgets

from config_manager import AVAILABLE_VOICES


class SettingsWindow(QtWidgets.QDialog):
    """Tray-launched settings: voice, default speed, hotkey. Persisted to
    config.json on apply."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("ReadingTool Settings")
        self.setMinimumWidth(360)
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # Voice
        layout.addWidget(QtWidgets.QLabel("Voice:"))
        self.voice_combo = QtWidgets.QComboBox()
        self.voice_combo.addItems(AVAILABLE_VOICES)
        layout.addWidget(self.voice_combo)

        # Default speed
        layout.addWidget(QtWidgets.QLabel("Default speed:"))
        speed_row = QtWidgets.QHBoxLayout()
        self.speed_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.speed_slider.setRange(50, 200)
        self.speed_value_label = QtWidgets.QLabel("1.00x")
        self.speed_value_label.setMinimumWidth(48)
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        speed_row.addWidget(self.speed_slider)
        speed_row.addWidget(self.speed_value_label)
        layout.addLayout(speed_row)

        # Hotkey
        layout.addWidget(QtWidgets.QLabel("Hotkey (e.g. ctrl+alt+r):"))
        self.hotkey_edit = QtWidgets.QLineEdit()
        self.hotkey_edit.setPlaceholderText("ctrl+alt+r")
        layout.addWidget(self.hotkey_edit)

        # Show clean text (diagnostic toggle)
        self.show_clean_check = QtWidgets.QCheckBox(
            "Show cleaned text in playback window"
        )
        layout.addWidget(self.show_clean_check)

        # Buttons
        btn_row = QtWidgets.QHBoxLayout()
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.close_btn = QtWidgets.QPushButton("Close")
        btn_row.addStretch()
        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

        self.apply_btn.clicked.connect(self._on_apply)
        self.close_btn.clicked.connect(self.accept)

    def _load_values(self):
        cfg = self.app.config
        voice = cfg.get("voice", "af_heart")
        idx = AVAILABLE_VOICES.index(voice) if voice in AVAILABLE_VOICES else 0
        self.voice_combo.setCurrentIndex(idx)
        speed = float(cfg.get("speed", 1.0))
        self.speed_slider.setValue(int(speed * 100))
        self._on_speed_changed(int(speed * 100))
        self.hotkey_edit.setText(cfg.get("shortcut", "ctrl+alt+r"))
        self.show_clean_check.setChecked(bool(cfg.get("show_clean_text", False)))

    def _on_speed_changed(self, val):
        self.speed_value_label.setText(f"{val / 100.0:.2f}x")

    def _on_apply(self):
        voice = self.voice_combo.currentText()
        speed = self.speed_slider.value() / 100.0
        hotkey = self.hotkey_edit.text().strip() or "ctrl+alt+r"
        show_clean = self.show_clean_check.isChecked()
        old_hotkey = self.app.config.get("shortcut")  # capture BEFORE mutating
        self.app.config_manager.set("voice", voice)
        self.app.config_manager.set("speed", speed)
        self.app.config_manager.set("shortcut", hotkey)
        self.app.config_manager.set("show_clean_text", show_clean)
        self.app.config = self.app.config_manager.config
        if hotkey != old_hotkey:
            self.app.reregister_hotkey()
        QtWidgets.QMessageBox.information(self, "Settings", "Settings saved.")
