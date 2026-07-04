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
import sys
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - WORKER - %(levelname)s - %(message)s",
    stream=sys.stderr,
)


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
    except Exception as e:
        _emit("error", msg=f"Bad input: {e}")
        return 1

    if not text.strip():
        _emit("error", msg="Empty text")
        return 1

    try:
        import sounddevice as sd
    except OSError as e:
        _emit("error", msg=f"PortAudio not found: {e}. Install libportaudio2.")
        return 1

    _emit("loading", phase="engine")

    try:
        from tts_engine import TTSEngine

        engine = TTSEngine()
        if not engine.ensure_loaded():
            _emit("error", msg="Failed to load Kokoro engine")
            return 1
    except Exception as e:
        _emit("error", msg=f"Engine load failed: {e}")
        return 1

    _emit("synthesizing")

    # Playback loop: synthesize chunk-by-chunk, play each immediately.
    # A prefetch thread synthesizes the next chunk while the current one
    # plays, so audio stays continuous with minimal gaps.
    stop_flag = threading.Event()

    def _synth_iter():
        for chunk in engine.synth_stream(text, voice, speed):
            if stop_flag.is_set():
                break
            yield chunk

    first = True
    next_chunk = None

    synth_gen = _synth_iter()
    try:
        next_chunk = next(synth_gen)
    except StopIteration:
        _emit("error", msg="No audio produced")
        return 1

    while next_chunk is not None:
        if stop_flag.is_set():
            break

        # Prefetch the next chunk in a thread while this one plays.
        current = next_chunk
        prefetch_result = {}

        def _prefetch():
            try:
                prefetch_result["chunk"] = next(synth_gen)
            except StopIteration:
                prefetch_result["chunk"] = None
            except Exception as e:
                prefetch_result["error"] = e

        prefetch_thread = threading.Thread(target=_prefetch, daemon=True)
        prefetch_thread.start()

        if first:
            _emit("playing")
            first = False

        samples, sr = current
        try:
            sd.play(samples, sr)
            sd.wait()
        except Exception as e:
            _emit("error", msg=f"Playback error: {e}")
            return 1

        prefetch_thread.join(timeout=30.0)
        if "error" in prefetch_result:
            _emit("error", msg=f"Synthesis error: {prefetch_result['error']}")
            return 1
        next_chunk = prefetch_result.get("chunk")

    _emit("done")
    return 0


if __name__ == "__main__":
    sys.exit(_run())
