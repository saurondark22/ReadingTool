"""TTS Worker process manager (daemon side).

The Daemon never loads Kokoro or onnxruntime. Instead it spawns a short-
lived `tts_worker` subprocess that owns the heavy engine + model for the
duration of one Read, then exits. This keeps the Daemon's resident memory
~30MB and reclaims the ~150MB worker memory the moment playback ends.

This module provides:
  - Model file management (download + path resolution) — shared with the
    worker since both live in the same app directory.
  - WorkerProcess: spawns the worker, monitors its stdout events, kills it
    on Stop/timeout.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import urllib.request

MODEL_FILENAME = "kokoro-v1.0.onnx"
VOICES_FILENAME = "voices-v1.0.bin"
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

# Hard ceiling on a worker process. If it hasn't exited by this point the
# Daemon kills it (safety net for a hung synth/playback with no audio).
WORKER_TIMEOUT_S = 600


def _app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.dirname(os.path.abspath(__file__))


def models_dir():
    d = os.path.join(_app_base_dir(), "models")
    os.makedirs(d, exist_ok=True)
    return d


def model_path():
    return os.path.join(models_dir(), MODEL_FILENAME)


def voices_path():
    return os.path.join(models_dir(), VOICES_FILENAME)


def models_present():
    return os.path.exists(model_path()) and os.path.exists(voices_path())


def download_models(on_progress=None):
    """Download model + voices files. on_progress receives (label, pct)."""
    for label, url, dest in [
        ("Model", MODEL_URL, model_path()),
        ("Voices", VOICES_URL, voices_path()),
    ]:
        if os.path.exists(dest):
            if on_progress:
                on_progress(label, 100)
            continue
        logging.info(f"Downloading {label}: {url}")
        tmp = dest + ".part"
        urllib.request.urlretrieve(url, tmp)
        os.rename(tmp, dest)
        if on_progress:
            on_progress(label, 100)
    logging.info("All model files present")


def _worker_script():
    """Path to the tts_worker script (or the frozen worker binary)."""
    if getattr(sys, "frozen", False):
        candidate = os.path.join(_app_base_dir(), "tts_worker")
        if os.path.exists(candidate):
            return candidate
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "tts_worker.py")


class WorkerProcess:
    """Spawns and monitors a single TTS Worker subprocess for one Read.

    Calls on_event(event_dict) for each JSON line the worker emits on stdout.
    """

    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda evt: None)
        self._proc = None
        self._reader_thread = None
        self._watcher_thread = None
        self._killed = False

    @property
    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self, text, voice, speed):
        """Spawn the worker, feed it params on stdin, start reading stdout."""
        script = _worker_script()
        is_python = script.endswith(".py")

        if is_python:
            cmd = [sys.executable, script]
        else:
            cmd = [script]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            logging.error(f"Failed to spawn TTS worker: {e}", exc_info=True)
            self.on_event({"event": "error", "msg": f"Spawn failed: {e}"})
            return

        params = json.dumps({"text": text, "voice": voice, "speed": speed})
        try:
            self._proc.stdin.write(params + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            logging.error(f"Failed to write to worker stdin: {e}")
            self.on_event({"event": "error", "msg": f"stdin write failed: {e}"})
            return

        self._reader_thread = threading.Thread(target=self._read_events, daemon=True)
        self._reader_thread.start()

        self._watcher_thread = threading.Thread(target=self._watch_timeout, daemon=True)
        self._watcher_thread.start()

    def _read_events(self):
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    self.on_event(evt)
                except json.JSONDecodeError:
                    logging.debug(f"Worker non-JSON line: {line}")
        except Exception as e:
            logging.debug(f"Worker stdout read ended: {e}")

    def _watch_timeout(self):
        """Safety net: if the worker hasn't exited after WORKER_TIMEOUT_S,
        kill it. This catches a hung synth/playback that produces no audio
        and never calls sd.wait() to completion."""
        try:
            self._proc.wait(timeout=WORKER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            if not self._killed:
                logging.warning(f"Worker timed out after {WORKER_TIMEOUT_S}s; killing")
                self.on_event({"event": "error", "msg": "Worker timed out"})
                self.kill()

    def kill(self):
        """Kill the worker process immediately (Stop / close window)."""
        if self._proc is None:
            return
        self._killed = True
        try:
            self._proc.kill()
            self._proc.wait(timeout=3.0)
        except Exception:
            pass
        logging.debug("Worker killed")

    def wait(self, timeout=None):
        if self._proc is None:
            return
        try:
            self._proc.wait(timeout=timeout)
        except Exception:
            pass
