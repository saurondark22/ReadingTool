import logging
import sys

from reading_tool_app import ReadingToolApp


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    app = ReadingToolApp(sys.argv)
    app.hotkey_triggered_signal.connect(app.on_hotkey_pressed)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
