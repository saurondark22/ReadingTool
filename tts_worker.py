"""TTS Worker subprocess.

Spawned on demand by the Daemon when a Read begins. Loads the Kokoro ONNX
engine + model, synthesizes the text into audio chunks, and plays each
chunk via sounddevice as it arrives (no disk writes). Exits as soon as
playback completes naturally, or is killed by the Daemon (SIGTERM/ SIGKILL)
on Stop / timeout.

Protocol:
  - stdin:  one JSON line: {"text": "...", "voice": "...", "speed": 1.0}
  - stdout: one JSON line per event:
      {"event": "loading"}            — engine loading started
      {"event": "playing"}           — first audio chunk is playing
      {"event": "done"}              — playback finished naturally
      {"event": "error", "msg": "..."} — fatal error
  - exit code 0 on done, non-zero on error/killed.

The worker does NOT depend on PySide6 — it is a pure CLI script so the
heavy audio + ML libs live only in this process, never in the Daemon.
"""

import json
import logging
import signal
import sys
import threading

import numpy as np

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - WORKER - %(levelname)s - %(message)s",
    stream=sys.stderr,
)

_stop_flag = threading.Event()
_sd = None


def _set_debug(enabled):
    """Raise logging to INFO when debug_mode is on; WARNING otherwise."""
    logging.getLogger().setLevel(logging.DEBUG if enabled else logging.WARNING)


def _handle_stop(signum, frame):
    logging.info("Stop signal received; halting playback")
    _stop_flag.set()
    try:
        if _sd is not None:
            _sd.stop()
    except Exception as e:
        logging.debug(f"sd.stop() on signal failed: {e}")


signal.signal(signal.SIGTERM, _handle_stop)


def _emit(event, **extra):
    payload = {"event": event}
    payload.update(extra)
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _ensure_models():
    from tts_engine import models_present, download_models, model_path, voices_path

    if not models_present():
        _emit("loading", phase="downloading")
        download_models()
    return model_path(), voices_path()


class _StreamPlayer:
    """Gapless ringbuffer playback for chunked TTS audio.

    A synthesis thread calls :meth:`put` with ``(samples, sample_rate)``
    tuples; a single ``sd.OutputStream`` callback drains the internal
    buffer sample-by-sample.  The first chunk primes the buffer so audio
    starts as soon as it arrives; subsequent chunks fill ahead while
    earlier audio plays.  Under-runs degrade to brief silence rather than
    hard stops.
    """

    PREFETCH = 2

    def __init__(self, stop_flag: threading.Event):
        self._stop_flag = stop_flag
        self._queue: list[tuple] = []
        self._queue_lock = threading.Lock()
        self._cv = threading.Condition(self._queue_lock)
        self._eos = False
        self._stream = None
        self._sr = None
        self._offset = 0
        self._current = None
        self._playing = False
        self._done = False
        self._started_first_chunk = False
        self._chunk_count = 0

    def put(self, chunk):
        """Add a (samples, sample_rate) chunk to the buffer.

        Flattens the array to 1-D: Kokoro's ONNX output is shape (1, N)
        (batch dimension + samples), but the OutputStream callback indexes
        sample-by-sample and needs a 1-D array.

        Blocks if PREFETCH chunks are already queued so the synth thread
        stays only ~2 batches ahead of playback, releasing memory for
        played clips instead of synthesizing the whole text up front.
        """
        samples, sr = chunk
        samples = np.asarray(samples, dtype=np.float32).ravel()
        idx = self._chunk_count
        self._chunk_count += 1
        dur = len(samples) / sr
        logging.info(f"Chunk {idx} queued: {len(samples)} samples, {dur:.2f}s")
        with self._cv:
            while len(self._queue) >= self.PREFETCH and not self._stop_flag.is_set():
                self._cv.wait(timeout=0.5)
            if self._stop_flag.is_set():
                return
            self._queue.append((samples, sr))
            self._cv.notify_all()

    def end_of_stream(self):
        """Signal that no more chunks will arrive."""
        with self._cv:
            self._eos = True
            logging.info("End of stream: all chunks synthesized")
            self._cv.notify_all()

    def start(self) -> bool:
        """Open the OutputStream.  Returns False on failure.

        Waits briefly for the first chunk so we know the sample rate
        (Kokoro always outputs 24000 Hz), then opens a mono float32
        OutputStream at that rate.
        """
        with self._cv:
            if not self._queue and not self._eos:
                self._cv.wait(timeout=2.0)
            if self._queue:
                self._sr = self._queue[0][1]
        if self._sr is None:
            self._sr = 24000

        try:
            self._stream = _sd.OutputStream(
                samplerate=self._sr,
                channels=1,
                dtype="float32",
                callback=self._callback,
                finished_callback=self._finished,
            )
            self._stream.start()
            logging.info(f"Audio stream opened: {self._sr}Hz, mono, float32")
            return True
        except Exception as e:
            logging.error(f"Failed to open OutputStream: {e}", exc_info=True)
            return False

    def _callback(self, outdata: np.ndarray, frames: int, *_):
        """PortAudio callback — fill outdata from the chunk buffer.

        MUST NOT block.  If the buffer is empty (under-run), output silence
        and return immediately.  The synthesis thread keeps the buffer primed
        ahead of playback so under-runs are rare.
        """
        try:
            outdata[:] = 0
            written = 0

            while written < frames and not self._stop_flag.is_set():
                if self._current is None or self._offset >= len(self._current):
                    with self._cv:
                        if not self._queue:
                            if self._eos:
                                self._done = True
                                self._cv.notify_all()
                                logging.info("Playback complete: EOS, buffer drained")
                                return
                            return
                        self._current, self._sr = self._queue.pop(0)
                        self._offset = 0
                        self._started_first_chunk = True
                        self._cv.notify_all()
                        logging.info(
                            f"Chunk playing: {len(self._current)} samples, "
                            f"{len(self._queue)} remaining in queue"
                        )

                avail = len(self._current) - self._offset
                need = frames - written
                n = min(avail, need)

                outdata[written : written + n, 0] = self._current[
                    self._offset : self._offset + n
                ]

                self._offset += n
                written += n
                self._playing = True
        except Exception as e:
            logging.error(f"EXCEPTION in audio callback: {e}", exc_info=True)
            raise

    def _finished(self, _=None):
        logging.info("Audio stream finished")

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def is_done(self) -> bool:
        return self._done

    def wait_step(self, timeout):
        """Sleep briefly so the main loop can poll without busy-waiting."""
        with self._cv:
            if not self._done and not self._stop_flag.is_set():
                self._cv.wait(timeout=timeout)

    def stop(self):
        """Close the stream and discard any buffered audio."""
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception as e:
            logging.debug(f"Stream stop error: {e}")
        with self._cv:
            self._queue.clear()
            self._cv.notify_all()


def _run():
    raw = sys.stdin.readline()
    if not raw:
        _emit("error", msg="No input received")
        return 1
    try:
        params = json.loads(raw)
        text = params.get("text", "")
        voice = params.get("voice", "af_heart")
        speed = float(params.get("speed", 1.0))
        debug_mode = bool(params.get("debug_mode", False))
    except Exception as e:
        logging.error(f"Bad input parse: {e}", exc_info=True)
        _emit("error", msg=f"Bad input: {e}")
        return 1

    _set_debug(debug_mode)

    if not text.strip():
        _emit("error", msg="Empty text")
        return 1

    try:
        from text_cleaner import clean_for_tts

        raw_len = len(text)
        text = clean_for_tts(text)
        logging.info(f"Text cleaned: {raw_len} -> {len(text)} chars")
        _emit("cleaned", text=text)
    except Exception as e:
        logging.debug(f"Text cleaner skipped: {e}")
        _emit("cleaned", text=text)
    if not text.strip():
        _emit("error", msg="Empty text after cleaning")
        return 1

    try:
        import sounddevice as sd
    except OSError as e:
        logging.error(f"PortAudio/sounddevice import failed: {e}", exc_info=True)
        _emit("error", msg=f"PortAudio not found: {e}. Install libportaudio2.")
        return 1
    except Exception as e:
        logging.error(f"Unexpected sounddevice import error: {e}", exc_info=True)
        _emit("error", msg=f"Audio backend error: {e}")
        return 1

    global _sd
    _sd = sd

    _emit("loading", phase="engine")

    try:
        # Defense in depth: ensure model files exist before loading. The
        # daemon gates on this too, but a worker spawned standalone (or a
        # race the daemon misses) must not crash on a missing file.
        _ensure_models()

        from tts_engine import TTSEngine

        engine = TTSEngine()
        if not engine.ensure_load():
            _emit("error", msg="Failed to load Kokoro engine")
            return 1
    except ImportError as e:
        logging.error(f"TTSEngine import failed: {e}", exc_info=True)
        _emit("error", msg=f"Engine import failed: {e}")
        return 1
    except Exception as e:
        logging.error(f"Engine load failed: {e}", exc_info=True)
        _emit("error", msg=f"Engine load failed: {e}")
        return 1

    _emit("synthesizing")

    stop_flag = _stop_flag
    synth_error = [None]

    def _synth_thread():
        """Synthesize all chunks and push them to the playback queue."""
        try:
            logging.info(f"Synthesis started: {len(text)} chars")
            for chunk in engine.synth_stream(text, voice, speed):
                if stop_flag.is_set():
                    logging.info("Synthesis stopped by user")
                    break
                _player.put(chunk)
            logging.info("Synthesis complete: all batches processed")
        except Exception as e:
            logging.error(f"Synthesis error: {e}", exc_info=True)
            synth_error[0] = e
        finally:
            _player.end_of_stream()

    _player = _StreamPlayer(stop_flag)

    synth_t = threading.Thread(target=_synth_thread, daemon=True)
    synth_t.start()

    if not _player.start():
        _emit("error", msg="Failed to open audio output stream")
        return 1

    first = True

    while True:
        if stop_flag.is_set():
            break
        if synth_error[0] is not None:
            _emit("error", msg=f"Synthesis failed: {synth_error[0]}")
            _player.stop()
            return 1
        if _player.is_done:
            break
        if first and _player.is_playing:
            _emit("playing")
            first = False
        _player.wait_step(0.05)

    _player.stop()
    if synth_error[0] is not None:
        _emit("error", msg=f"Synthesis failed: {synth_error[0]}")
        return 1
    if stop_flag.is_set():
        return 0

    if not _player._started_first_chunk:
        _emit("error", msg="No audio produced")
        return 1

    _emit("done")
    return 0


if __name__ == "__main__":
    sys.exit(_run())
