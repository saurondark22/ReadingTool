import json
import logging
import os
import sys

CURRENT_CONFIG_VERSION = 1

DEFAULT_CONFIG = {
    "voice": "af_heart",
    "speed": 1.0,
    "shortcut": "ctrl+alt+r",
    "show_clean_text": False,
    "debug_mode": False,
}

AVAILABLE_VOICES = [
    "af_heart",
    "af_nicole",
    "af_bella",
    "af_sky",
    "am_adam",
    "am_michael",
    "bf_emma",
    "bf_isabella",
    "bm_george",
]


def app_dir():
    return (
        os.path.dirname(sys.argv[0])
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__))
    )


class ConfigManager:
    def __init__(self):
        self.config_path = os.path.join(app_dir(), "config.json")
        self.config = None
        self.load()

    def load(self):
        logging.debug(f"Loading config from {self.config_path}")
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    self.config = json.load(f)
                self._migrate()
            except Exception as e:
                logging.error(f"Error loading config: {e}; resetting to defaults")
                self.config = dict(DEFAULT_CONFIG)
                self.save()
        else:
            logging.debug("Config file not found; creating default")
            self.config = dict(DEFAULT_CONFIG)
            self.config["is_config_file_updated_for_v1"] = True
            self.save()

    def _migrate(self):
        needs_v1 = not self.config.get("is_config_file_updated_for_v1", False)
        if not needs_v1:
            return
        logging.info("Running config migration to v1")
        changed = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in self.config:
                self.config[k] = v
                changed = True
        self.config["is_config_file_updated_for_v1"] = True
        self.save()
        if changed:
            logging.info("Config migration complete")

    def save(self):
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=4)
        logging.debug("Config saved")

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.save()
