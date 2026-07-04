import logging
import threading
import time

import pyperclip
from pynput import keyboard as pykeyboard


class SelectionCapture:
    """Captures the user's currently-selected text by injecting Ctrl+C and
    polling the clipboard, mirroring WritingTool. The capture runs in a
    background thread so the caller is never blocked.

    Usage:
        holder = SelectionCapture().capture_async()
        ... do UI work ...
        if holder.ready.wait(timeout=3.0):
            text = holder.text
    """

    def __init__(self):
        self._capture_lock = threading.Lock()

    @staticmethod
    def _clear_clipboard():
        try:
            pyperclip.copy("")
        except Exception as e:
            logging.error(f"Error clearing clipboard: {e}")

    def capture_async(self, holder):
        """Inject Ctrl+C now, then poll the clipboard in a background thread.

        Returns immediately. `holder` is a SelectionHolder whose `text` and
        `ready` will be populated by the background thread.
        """
        try:
            clipboard_backup = pyperclip.paste()
        except Exception:
            clipboard_backup = ""

        self._clear_clipboard()

        kbrd = pykeyboard.Controller()
        try:
            kbrd.press(pykeyboard.Key.ctrl.value)
            kbrd.press("c")
            kbrd.release("c")
            kbrd.release(pykeyboard.Key.ctrl.value)
        except Exception as e:
            logging.error(f"Error simulating Ctrl+C: {e}")

        def _poll():
            with self._capture_lock:
                text = ""
                try:
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        try:
                            text = pyperclip.paste() or ""
                        except Exception as e:
                            logging.error(f"Error reading clipboard during poll: {e}")
                            text = ""
                        if text:
                            break
                        time.sleep(0.05)
                    holder.text = text
                    logging.debug(f"Captured selected text (len={len(text)})")
                finally:
                    try:
                        pyperclip.copy(clipboard_backup)
                    except Exception as e:
                        logging.error(f"Error restoring clipboard: {e}")
                    holder.ready.set()

        threading.Thread(target=_poll, daemon=True).start()
        return holder


class SelectionHolder:
    __slots__ = ("text", "ready")

    def __init__(self):
        self.text = ""
        self.ready = threading.Event()
