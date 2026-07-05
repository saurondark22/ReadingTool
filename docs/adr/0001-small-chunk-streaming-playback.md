# Small-chunk streaming playback via phoneme-aware splitting and a ringbuffer output stream

Long selections sat in "Synthesizing…" for seconds before any audio because
kokoro-onnx's `_split_phonemes` targets `MAX_PHONEME_LENGTH = 510` phonemes per
batch — roughly a full paragraph — so the entire text became one ONNX inference
call that had to finish before sound started. We replace the library's batch
splitter with ReadingTool's own and switch playback from discrete
`sd.play`/`sd.wait` per chunk to a continuous `sd.OutputStream` fed by a
synthesis thread, so the first sentence plays within ~1 s and the rest streams
behind it gaplessly.

## Splitting rule (replaces `Kokoro._split_phonemes`)

Operate on the **phoneme** string (post-G2P), with the **word** as the atomic
unit (a space-delimited run of phonemes; never cut mid-word).

1. Don't end a batch before **MIN_CAP (~10 phonemes)** — prevents micro-batches
   from sequences of lone punctuation (`. . . .`).
2. A word ending in **any punctuation** (`. ! ? , ; :`) past `min_cap` → batch
   ends there.  Breaking at any punctuation (not just sentence terminators)
   makes the first batch as small as possible so audio starts fast.
3. No punctuation within the **soft cap (~120 phonemes)** → cut at the next
   word boundary past it.
4. No word boundary within the **hard cap (~200 phonemes)** (a run-on
   sentence) → cut at a word boundary anyway — never mid-word.

Budget is measured in phonemes (latency proxy), unit is the word (prosody).

## Playback (replaces the `sd.play`/`sd.wait` loop)

A single `sd.OutputStream` is opened at Read start with a PortAudio callback.
A synthesis thread synthesizes batches into a thread-safe ringbuffer; the
callback drains it sample-by-sample. First batch primes the buffer (small →
fast first audio); subsequent batches fill ahead while earlier audio plays.
**Prefetch depth is 2**: the synth thread blocks once 2 chunks are queued, so
only ~2 batches of audio are resident at once and played clips are released
from memory as soon as the callback pops them. Stop closes the stream and
drains the buffer. Under-runs degrade to brief silence, not hard stops.

The synth thread audio arrays are flattened from Kokoro's native `(1, N)` 2-D
output to 1-D `(N,)` before queuing — the callback indexes sample-by-sample
and requires 1-D arrays.

## Rejected alternatives

- **Persist the worker across Reads (warm worker).** Would eliminate the engine
  cold-start wall on every Read after the first. Rejected: adds a stateful
  multi-line stdin protocol and continuous ~150 MB resident memory, for a
  benefit that only helps repeat Reads. The cold start is a separate problem
  to tackle if it becomes the dominant latency; chunking is the priority.
- **Split the text into sentences before phonemizing.** Re-runs espeak per
  chunk and needs a sentence-segmentation step; splitting phonemes in place
  keeps one fast G2P pass and reuses the punctuation espeak already preserves.