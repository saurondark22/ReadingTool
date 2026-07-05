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
  halting both synthesis and audio. Available as an item in the Tray
  Icon's right-click menu. Re-pressing the hotkey during a Read acts as
  Stop. There is no Pause/Resume — Stop is the only transport control.

- **Speed** — Playback rate multiplier (Kokoro `speed` param), e.g.
  1.0. A value in `config.json`, applied at Read start (when the TTS
  Worker is spawned). The last-used value is persisted to `config.json`.
  There is no runtime Speed control; Speed is set by editing
  `config.json` (via the Tray Icon's Settings item, which opens the
  file in the system's default editor).

- **Tray Icon** — The system-tray presence of the daemon. Its
  right-click menu exposes Stop (kills any active Read), Settings
  (opens `config.json` in the system's default editor), and Quit.
  The daemon is otherwise headless; no window surfaces at runtime.
  Status during a Read (Loading / Playing / Done / Error) is shown as
  a native tray notification (balloon) emitted by the Daemon when the
  TTS Worker reports the corresponding event.

- **Voice** — A Kokoro voice id (e.g. `af_heart`) used for synthesis.
  Configurable in `config.json` only (edited via the Tray Icon's
  Settings item).

- **Hotkey** — The global keyboard shortcut that toggles a Read.
  When no Read Session is active, a press initiates a Read (capture
  → synthesize → play). When a Read Session is active, a press acts as
  Stop. Thus the hotkey is a one-key toggle between "read this" and
  "be quiet."

- **TTS Worker** — A short-lived subprocess spawned on demand by the
  Daemon when a Read begins. It loads the Kokoro ONNX engine +
  model, synthesizes the Selection into audio, and exits as soon as
  playback completes (or is killed by the Daemon on Stop / timeout).
  The heavy resident memory lives only in this process and is
  reclaimed the moment it exits. It runs the fp16 Kokoro model
  (`kokoro-v1.0.fp16.onnx`, ~170MB) rather than the fp32 original
  (~311MB) or the int8 quantization (~88MB). The model choice is
  constrained by a hard realtime requirement: synthesis must be
  faster than playback (RTF < 1.0) so the ringbuffer stays primed
  and audio is gapless. fp16 measures RTF ~0.8 and ~600MB RSS; int8
  measures RTF ~2.6 (dequantize-on-CPU overhead) and breaks streaming
  despite lower RSS, so it is rejected even though it is the smallest.
  onnxruntime's CPU memory arena is left enabled (default), because
  this workload makes many small `sess.run()` calls (one per `Chunk`)
  and the arena reuses memory across them.

- **Settings** — User-editable configuration (voice, default speed,
  hotkey) persisted to `config.json`, edited via the Tray Icon's
  Settings item (opens the file in the system's default editor).

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
  raw markdown, since a `Selection` is rendered text. It also drops
  **noise**: ASCII control chars (everything below 0x20 except `\t`
  and `\n`, plus DEL) and **structural punctuation** (`() [] {}`)
  that carry no speech and no prosody, leaving **prosody
  punctuation** (`. , ! ? ; : - … " '` and significant symbols like
  `$ @ ~ % + =`) intact. Every surviving line break (paragraph or
  line) is converted to a sentence terminator (`". "`) so a line
  that ended without punctuation still marks a `Chunk` boundary.

- **Clean Text** — The output of the `Cleaner`: the `Selection` with
  invisible characters removed, Unicode glyphs replaced by ASCII or
  spoken-word equivalents, line-wrap artifacts repaired, structural
  brackets dropped, ASCII control chars dropped, and every surviving
  line break converted to a sentence terminator (`". "`), but with
  prose, numbers, contractions, and prosody punctuation otherwise
  untouched.   It is the exact string handed to the `Phonemizer`. In the
  current build the TTS Worker emits it back to the Daemon as the
  `cleaned` event (kept for diagnostics; no window displays it).

- **Chunk** — One batch of phonemes that the TTS Worker synthesizes
  into one audio segment and feeds to the ringbuffer playback stream.
  A `Chunk` is cut from the phoneme string (post-`Phonemizer`) with the
  **word** as the atomic unit: a boundary never lands mid-word.
  Splitting breaks at any punctuation (`. ! ? , ; :`) past a minimum
  cap (~10 phonemes), so the first chunk is as small as possible for
  fast first-audio. Because the `Cleaner` already turned every
  surviving line break into a `"."`, line and paragraph boundaries
  act as `Chunk` boundaries without the splitter knowing about `\n`.
  If no punctuation appears within the soft cap (~120 phonemes), it
  cuts at a word boundary; a hard cap (~200 phonemes) ensures a run-on
  sentence never produces a catastrophically long batch. The minimum
  cap prevents micro-batches from sequences of lone punctuation
  (`. . . .`). The soft/hard caps are measured in phonemes (a latency
  proxy), but the unit is the word (a prosody boundary). One `Chunk`
  is the granularity at which audio starts (the first `Chunk` primes
  the ringbuffer) and at which the synthesis thread stays ahead of
  playback. The TTS Worker keeps at most ~2 `Chunk`s buffered ahead of
  playback (prefetch depth) so played clips are released from memory
  as soon as they finish.
