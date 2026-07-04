# ReadingTool

A Linux background daemon that reads your currently-selected text aloud
via [Kokoro TTS](https://github.com/hexgrad/kokoro), triggered by a global
hotkey. English-only, fully offline, autostarts at login.

Mirrors the architecture of [WritingTools](../WritingTools): PySide6 tray
daemon + pynput global hotkey + Ctrl+C/clipboard selection capture.

## Architecture

ReadingTool is split into two binaries:

- **ReadingTool (daemon, ~66MB)** — the lightweight autostarted background
  process. Owns the hotkey listener, selection capture, tray icon, and
  Playback Window UI. Resident memory ~30MB. Never loads the TTS engine.
- **tts_worker (~63MB)** — a short-lived subprocess spawned on demand for
  each Read. Loads Kokoro ONNX + model (~150MB RAM), synthesizes audio
  chunk-by-chunk, plays via sounddevice, and exits when done (or is killed
  by the daemon on Stop/timeout). No disk writes — audio streams directly
  to the speakers.

Model files (~350MB) are downloaded on first run into the app's `models/`
directory and shared between the worker and the download manager.

## How it works

- Press the hotkey (default `Ctrl+Alt+R`) with text selected in any app.
- A small Playback Window pops near the cursor, shows the captured text,
  and **auto-starts reading** via Kokoro ONNX TTS.
- Transport controls: **Play / Pause / Resume / Stop** and a **Speed**
  slider (persisted to `config.json`).
- Press the hotkey again (while reading or while the window is open) to
  **Stop and close the window** — a one-key toggle between "read this"
  and "be quiet". The window's own Stop halts audio without closing.
- Tray icon exposes **Settings** (voice, default speed, hotkey) and
  **Quit**.

## Dependencies

- Python 3.11–3.12 (kokoro-onnx requirement)
- System packages: `xclip` or `xsel` (clipboard capture), `portaudio19`
  (sounddevice audio output).
- Kokoro model files (`kokoro-v1.0.onnx`, `voices-v1.0.bin`, ~350MB) are
  downloaded automatically on first run into the app's `models/` dir.

## Build & install (local user install)

One command — creates a venv, installs deps, builds, and installs with
autostart:

```bash
bash build-and-install-local-linux.sh --enable-autostart
```

This avoids the PEP 668 "externally managed environment" error by using a
project-local venv at `.venv/`. It installs to
`~/.local/share/readingtool/app`, creates the `reading-tool` launcher in
`~/.local/bin`, a desktop menu entry, and an autostart entry at
`~/.config/autostart/reading-tool.desktop`.

To rebuild after code changes without re-installing deps:

```bash
bash build-and-install-local-linux.sh --skip-build   # install existing build
.venv/bin/python3 pyinstaller-build-script.py         # rebuild only
```

## Run from source (without building)

```bash
bash build-and-install-local-linux.sh --skip-build   # sets up venv + deps
.venv/bin/python3 main.py
```

## Configuration

`config.json` (in the app directory):

```json
{
    "voice": "af_heart",
    "speed": 1.0,
    "shortcut": "ctrl+alt+r",
    "is_config_file_updated_for_v1": true
}
```

Available voices: `af_heart`, `af_nicole`, `af_bella`, `af_sky`,
`am_adam`, `am_michael`, `bf_emma`, `bf_isabella`, `bm_george`.

## Notes

- **Wayland**: global hotkey and focus behavior may be limited; X11 is
  the primary target (same caveat as WritingTools).
- First launch downloads the model (~350MB); subsequent launches preload
  the engine in the background for instant first Read.