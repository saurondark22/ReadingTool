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

import numpy as np

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


class TTSEngine:
    """Thin wrapper around the Kokoro ONNX engine used by the TTS Worker.

    Lives only in the worker process (the Daemon never instantiates this).
    Lazily loads the model + voices, then exposes a chunked `synth_stream`
    generator that yields (samples, sample_rate) tuples — one per phoneme
    batch — so the worker can play audio chunk-by-chunk as it synthesizes.
    """

    def __init__(self):
        self._kokoro = None
        self._model_path = model_path()
        self._voices_path = voices_path()

    def ensure_load(self):
        """Lazily load the Kokoro engine + model. Returns True on success."""
        if self._kokoro is not None:
            return True
        try:
            from kokoro_onnx import Kokoro

            logging.debug(
                f"Loading Kokoro engine: model={self._model_path} voices={self._voices_path}"
            )
            self._kokoro = Kokoro(self._model_path, self._voices_path)
            logging.debug("Kokoro engine loaded")
            return True
        except Exception as e:
            logging.error(f"Failed to load Kokoro engine: {e}", exc_info=True)
            self._kokoro = None
            return False

    # --- chunking parameters --------------------------------------------
    # One Chunk is a batch of phonemes synthesized in one ONNX inference
    # call.  The word is the atomic unit (never cut mid-word).  We prefer
    # sentence terminators, fall back to clause punctuation, and at the
    # hard cap we cut at a word boundary so a run-on sentence never
    # produces a catastrophically long batch.  MIN_CAP prevents micro-
    # batches from sequences like ". . . ." so each batch carries at
    # least one real word of speech.
    MIN_CAP = 10  # phonemes — don't end a batch before at least this many
    SOFT_CAP = 120  # phonemes — prefer to cut at or before this
    HARD_CAP = 200  # phonemes — never exceed this

    @staticmethod
    def _split_phonemes(
        phonemes, min_cap=MIN_CAP, soft_cap=SOFT_CAP, hard_cap=HARD_CAP
    ):
        """Split a phoneme string into word-bounded batches.

        Walks the phoneme string word-by-word (space-delimited) and ends a
        batch when:
          1. A word ending in any punctuation (. ! ? , ; :) is seen past
             ``min_cap`` — preferred (natural prosody break).  This makes
             the first batch as small as possible so audio starts fast.
          2. No punctuation appears within ``soft_cap`` phonemes — cut at
             the next word boundary past ``soft_cap``.
          3. No word boundary within ``hard_cap`` phonemes (a run-on
             sentence) — cut at a word boundary anyway so the batch is
             never catastrophically long.

        Never splits mid-word.  Never ends a batch before ``min_cap``
        phonemes so sequences of lone punctuation (". . . .") don't
        produce one-batch-per-period.
        """
        words = phonemes.split(" ")
        batches = []
        current_words: list[str] = []
        current_len = 0

        def _end_batch():
            nonlocal current_words, current_len
            if current_words:
                batches.append(" ".join(current_words).strip())
                current_words = []
                current_len = 0

        for word in words:
            word = word.strip()
            if not word:
                continue
            word_len = len(word) + (1 if current_words else 0)

            current_words.append(word)
            current_len += word_len

            # Don't end a batch before min_cap (prevents micro-batches).
            if current_len < min_cap:
                continue

            # Any punctuation past min_cap — preferred break (smallest
            # possible batches for fast first-audio).
            if word and word[-1] in ".!?,;:":
                _end_batch()
                continue

            # Soft cap — cut at word boundary if past it.
            if current_len >= soft_cap:
                _end_batch()
                continue

            # Hard cap — force a break (run-on sentence).
            if current_len >= hard_cap:
                _end_batch()

        _end_batch()
        return [b for b in batches if b]

    def synth_stream(self, text, voice, speed):
        """Yield (samples_ndarray, sample_rate) chunks for the given text.

        Phonemizes the whole text in one G2P pass (fast, preserves cross-
        sentence continuity), then splits the phoneme string into small
        word-bounded batches via :meth:`_split_phonemes`.  Each batch is
        synthesized separately so playback can start after the first
        sentence rather than waiting for the whole text.

        The first chunk is NOT trimmed — librosa's default trim (top_db=60)
        is aggressive enough to clip soft word onsets, so the first words
        get cut. Subsequent chunks get a gentler top_db=30 trim that only
        removes the long leading silence Kokoro emits (~2s) without eating
        into speech.
        """
        if self._kokoro is None:
            raise RuntimeError("Engine not loaded; call ensure_load() first")
        speed = max(0.5, min(2.0, float(speed)))
        voice_style = self._kokoro.get_voice_style(voice)
        lang = "en-us"
        phonemes = self._kokoro.tokenizer.phonemize(text, lang)
        batches = self._split_phonemes(phonemes)
        logging.info(f"Phonemize: {len(phonemes)} phonemes -> {len(batches)} batches")
        from kokoro_onnx.trim import trim as trim_audio

        first = True
        for i, batch in enumerate(batches):
            if not batch.strip():
                continue
            logging.info(f"Batch {i}/{len(batches)}: {batch}")
            audio, sr = self._kokoro._create_audio(batch, voice_style, speed)
            dur = np.asarray(audio).ravel().shape[0] / sr
            logging.info(f"Batch {i}: done, {dur:.2f}s audio")
            if first:
                first = False
            else:
                audio, _ = trim_audio(audio, top_db=30)
            yield audio, sr


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
        self._stderr_thread = None
        self._watcher_thread = None
        self._killed = False
        self._debug_mode = False

    @property
    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self, text, voice, speed, debug_mode=False):
        """Spawn the worker, feed it params on stdin, start reading stdout."""
        self._debug_mode = debug_mode
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

        params = json.dumps(
            {
                "text": text,
                "voice": voice,
                "speed": speed,
                "debug_mode": debug_mode,
            }
        )
        try:
            self._proc.stdin.write(params + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            logging.error(f"Failed to write to worker stdin: {e}")
            self.on_event({"event": "error", "msg": f"stdin write failed: {e}"})
            return

        self._reader_thread = threading.Thread(target=self._read_events, daemon=True)
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

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

    def _read_stderr(self):
        """Forward worker stderr to the daemon's logging.

        Always forwards WARNING+ (errors, timeouts).  INFO/DEBUG lines
        (lifecycle events) are forwarded only when debug_mode is on.
        """
        try:
            for line in self._proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                # Worker log lines look like:
                #   "2026-... - WORKER - LEVEL - message"
                # Detect the level to decide whether to forward.
                if " - WORKER - " in line:
                    level_part = line.split(" - WORKER - ")[1].split(" - ")[0]
                    if level_part in ("WARNING", "ERROR", "CRITICAL"):
                        logging.warning(f"[WORKER] {line}")
                    elif self._debug_mode:
                        logging.info(f"[WORKER] {line}")
                else:
                    # Non-worker-format line (e.g. Python traceback) — always log.
                    logging.warning(f"[WORKER] {line}")
        except Exception as e:
            logging.debug(f"Worker stderr read ended: {e}")

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
        """Stop the worker: SIGTERM first (lets it call sd.stop() to cut
        buffered audio), then SIGKILL if it doesn't exit promptly."""
        if self._proc is None:
            return
        self._killed = True
        try:
            self._proc.terminate()  # SIGTERM — graceful stop
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                logging.debug("Worker didn't exit on SIGTERM; SIGKILL")
                self._proc.kill()
                self._proc.wait(timeout=3.0)
        except Exception as e:
            logging.debug(f"Worker kill error: {e}")
        logging.debug("Worker killed")

    def wait(self, timeout=None):
        if self._proc is None:
            return
        try:
            self._proc.wait(timeout=timeout)
        except Exception:
            pass
