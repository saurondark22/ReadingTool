# ReadingTool

A Linux background daemon that reads your currently-selected text aloud
via [Kokoro TTS](https://github.com/hexgrad/kokoro), triggered by a global
hotkey. English-only, fully offline, autostarts at login.

## What it does

Select any text in any app, press the hotkey (default `Ctrl+Alt+R`), and
ReadingTool reads it aloud. A small Playback Window pops near the cursor,
shows the captured text, and auto-starts playback.

- **Play / Stop** and a **Speed** slider (persisted to `config.json`). No
  Pause/Resume — Stop is the only control besides Play.
- Press the hotkey again to **Stop and close the window** — a one-key toggle
  between "read this" and "be quiet".
- Tray icon exposes **Settings** (voice, default speed, hotkey) and **Quit**.

## How it works

A lightweight daemon (hotkey listener, selection capture, tray, Playback
Window) spawns a short-lived TTS worker per Read. The worker loads Kokoro
ONNX, synthesizes the text in phoneme-bounded chunks, streams audio to the
speakers via a ringbuffer, and exits when playback finishes or is killed on
Stop. Model files (~350 MB) are downloaded on first run.

## Dependencies

- Python 3.11–3.12
- System packages: `xclip` or `xsel` (clipboard capture), `portaudio19`
  (audio output)

## Build & install (local user install)

```bash
bash build-and-install-local-linux.sh --enable-autostart
```

Creates a venv at `.venv/`, installs deps, builds, and installs with
autostart. Installs to `~/.local/share/readingtool/app`, creates the
`reading-tool` launcher in `~/.local/bin`, a desktop menu entry, and an
autostart entry at `~/.config/autostart/reading-tool.desktop`.

To rebuild after code changes without re-installing deps:

```bash
bash build-and-install-local-linux.sh --skip-build   # install existing build
.venv/bin/python3 pyinstaller-build-script.py         # rebuild only
```

## Run from source

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
