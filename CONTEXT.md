# CONTEXT.md — ReadingTool

A glossary for the ReadingTool domain. Implementation-free. Terms are
captured here as they are resolved during design.

## Glossary

- **ReadingTool** — The application as a whole: a Linux background
  daemon that reads the user's currently-selected text aloud via Kokoro
  TTS, triggered by a global hotkey.

- **Read** — The act of converting a captured `Selection` into speech
  and playing it back to the user. One `Read` is triggered by one
  hotkey press.

- **Selection** — The text the user has highlighted in whatever
  application currently has focus at the moment the hotkey is pressed.
  Captured by injecting Ctrl+C and polling the clipboard (mirrors
  WritingTool). In practice a **Selection** is *rendered plain text*:
  Unicode-rich prose as the source application emitted it to the
  clipboard, not a structured document. It carries real Unicode
  punctuation (smart quotes, em/en dashes, ellipsis, bullets),
  invisible characters (NBSP, zero-width, soft hyphen), and
  line-wrap artifacts — but generally not raw markdown syntax, since
  the user copies from rendered surfaces.

- **Read Session** — A single playback lifecycle: from hotkey press
  through capture, synthesis, playback, to natural completion or a
  user-issued Stop.

- **Stop** — User action that kills the TTS Worker process immediately,
  halting both synthesis and audio. Available as a button in the
  Playback Window. Re-pressing the hotkey during a Read acts as Stop.
  There is no Pause/Resume — Stop is the only transport control
  besides Play.

- **Speed** — Playback rate multiplier (Kokoro `speed` param), e.g.
  1.0. A runtime slider in the Playback Window whose last-used value
  is persisted to `config.json`. Speed is applied at Read start (when
  the TTS Worker is spawned); changing the slider mid-playback takes
  effect on the next Play.

- **Playback Window** — A small foreground window that appears on
  hotkey press, shows the captured Selection, auto-starts the Read
  (spawning the TTS Worker), and exposes transport controls (Play,
  Stop, Speed). Distinguished from the tray-only Settings window.

- **Voice** — A Kokoro voice id (e.g. `af_heart`) used for synthesis.
  Configurable in Settings only.

- **Hotkey** — The global keyboard shortcut that toggles a Read.
  When no Read Session is active, a press initiates a Read (capture
  → synthesize → play) and shows the Playback Window. When a Read
  Session is active OR the Playback Window is open, a press acts as
  Stop and closes the Playback Window. Thus the hotkey is a one-key
  toggle between "read this" and "be quiet." The window's own Stop
  button halts playback without closing the window.

- **Tray Icon** — The system-tray presence of the daemon. Provides
  Settings and Quit. The daemon is otherwise headless; the only
  window that surfaces at runtime is the transient Playback Window.

- **Daemon** — The lightweight long-running background process (~30MB
  resident) that owns the hotkey listener, the capture flow, and the
  tray + Playback Window UI. It does **not** hold the TTS engine or
  model in memory. Autostarted at login.

- **TTS Worker** — A short-lived subprocess spawned on demand by the
  Daemon when a Read begins. It loads the Kokoro ONNX engine +
  model, synthesizes the Selection into audio, and exits as soon as
  playback completes (or is killed by the Daemon on Stop / timeout).
  The heavy resident memory (~150MB) lives only in this process and
  is reclaimed the moment it exits.

- **Settings** — User-editable configuration (voice, default speed,
  hotkey) persisted to `config.json`, edited via the Settings window.

- **Phonemizer** — The G2P stage inside the TTS Worker that converts
  the cleaned `Selection` into IPA phonemes for the Kokoro model. It is
  espeak-ng (via the `phonemizer` library, as wrapped by kokoro-onnx
  `Tokenizer.phonemize`), which already expands numbers and basic
  abbreviations and preserves ASCII punctuation.

- **Cleaner** — The pure-function preprocessor that runs in the TTS
  Worker, between clipboard handoff and the `Phonemizer`. Its job is
  to turn a `Selection` into **Clean Text**: rendered-plain-text
  hygiene (invisible-char removal, Unicode-symbol → ASCII or spoken
  replacement, line-wrap repair) and symbol-to-prose translation
  (e.g. `→` → "to", `•` → "-") so the `Phonemizer` never sees
  glyphs it will misread or silently drop. It deliberately does
  *not* duplicate work the `Phonemizer` already does (numbers,
  contractions, ASCII punctuation) and does *not* attempt to parse
  raw markdown, since a `Selection` is rendered text.

- **Clean Text** — The output of the `Cleaner`: the `Selection` with
  invisible characters removed, Unicode glyphs replaced by ASCII or
  spoken-word equivalents, and line-wrap artifacts repaired, but
  with prose, numbers, contractions, and ASCII punctuation otherwise
  untouched. It is the exact string handed to the `Phonemizer`. In
  the current build the TTS Worker emits it back to the Daemon as the
  `cleaned` event so the `Playback Window` can display it for
  inspection.
