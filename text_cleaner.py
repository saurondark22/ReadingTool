"""Rendered-text normalizer for TTS.

A Selection is rendered plain text (Unicode-rich prose as the source
application emitted it to the clipboard), not raw markdown. This module
turns it into Clean Text: invisible chars removed, Unicode glyphs
replaced by ASCII or spoken-word equivalents, and line-wrap artifacts
repaired, so the Phonemizer (espeak-ng by default) never sees glyphs it
will misread or silently drop.

It deliberately does NOT duplicate work the Phonemizer already does
(numbers, contractions, ASCII punctuation) and does NOT attempt to parse
raw markdown. No AI / no LLM — just regex and string ops.
"""

import re
import unicodedata

# --- invisible / format characters ----------------------------------------

# Map of invisible / format chars -> their replacement. Everything not
# listed here is kept. NBSP becomes a normal space; zero-width and soft
# hyphen disappear; BOM and most Unicode format (Cf) chars disappear.
_INVISIBLE_MAP = {
    "\u00a0": " ",  # NBSP
    "\u200b": "",  # zero-width space
    "\u200c": "",  # zero-width non-joiner
    "\u200d": "",  # zero-width joiner
    "\u00ad": "",  # soft hyphen
    "\ufeff": "",  # BOM / zero-width no-break space
    "\u2060": "",  # word joiner
    "\u200e": "",  # left-to-right mark
    "\u200f": "",  # right-to-left mark
    "\u202a": "",  # LRE
    "\u202b": "",  # RLE
    "\u202c": "",  # PDF
    "\u202d": "",  # LRO
    "\u202e": "",  # RLO
    "\u2061": "",  # function application
    "\u2062": "",  # invisible times
    "\u2063": "",  # invisible separator
    "\u2064": "",  # invisible plus
}

# --- quote normalization ---------------------------------------------------

_RE_SMART_DOUBLE = re.compile(
    r"[\u201C\u201D\u201E\u201F\u00AB\u00BB\u2018\u2019\u201A\u201B]"
)
_QUOTE_REPLACEMENTS = {
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u00ab": '"',
    "\u00bb": '"',
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
}

# --- dashes ----------------------------------------------------------------

# Em dash -> comma pause. Handles both spaced ("a — b") and unspaced
# ("a—b") forms; rendered text often drops the spaces.
_RE_EMDASH = re.compile(r"\s*\u2014\s*")
# En dash between digits -> " to " (range: 1990–2000).
_RE_ENDASH_RANGE = re.compile(r"(\d)\s*\u2013\s*(\d)")
# Standalone en dash -> ASCII hyphen.
_RE_ENDASH = re.compile(r"\u2013")
# Figure dash / horizontal bar -> comma pause.
_RE_FIGDASH = re.compile(r"\s*[\u2012\u2015]\s*")

# --- ellipsis --------------------------------------------------------------

_RE_ELLIPSIS = re.compile(r"\u2026")

# --- bullets / middle dots -------------------------------------------------

_RE_BULLET = re.compile(r"[\u2022\u2023\u25E6\u2043\u204C\u204D\u00B7]")
# Lone bullet on its own line -> "- "; inline middle dot -> "-".

# --- arrows ----------------------------------------------------------------

_RE_ARROW = re.compile(
    r"\s*(?:\u2192|\u21D2|\u27F6|\u27A1|\u2196|\u2197|\u2198|\u2199)\s*"
)
_RE_ARROW_LEFT = re.compile(r"\s*(?:\u2190|\u21D0|\u27F5|\u2B05)\s*")
_RE_ARROW_UP = re.compile(r"\s*(?:\u2191|\u21D1|\u27F7)\s*")
_RE_ARROW_DOWN = re.compile(r"\s*(?:\u2193|\u21D3|\u27F8)\s*")

# --- math / symbol -> prose -------------------------------------------------

_SYMBOL_MAP = {
    "\u2248": " about ",  # ≈
    "\u2260": " not equal to ",  # ≠
    "\u2264": " less than or equal to ",  # ≤
    "\u2265": " greater than or equal to ",  # ≥
    "\u00d7": " times ",  # ×
    "\u00f7": " divided by ",  # ÷
    "\u00b1": " plus or minus ",  # ±
    "\u221e": " infinity ",  # ∞
    "\u221a": " square root of ",  # √
    "\u00b0": " degrees ",  # °
    "\u2192": " to ",  # → (fallback if arrow pass missed)
}

# --- ligatures -------------------------------------------------------------

_LIGATURE_MAP = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
}

# --- CJK punctuation -> ASCII ----------------------------------------------

_CJK_PUNCT_MAP = {
    "\u3001": ", ",  # 、 ideographic comma
    "\u3002": ". ",  # 。
    "\uff01": "! ",  # ！
    "\uff0c": ", ",  # ，
    "\uff1a": ": ",  # ：
    "\uff1b": "; ",  # ；
    "\uff1f": "? ",  # ？
}

# --- file_path:line_number -------------------------------------------------

_RE_FILELINE = re.compile(
    r"([\w./-]+\.(?:py|js|ts|tsx|jsx|rs|go|c|cpp|h|hpp|java|rb|sh|yml|yaml|json|toml|md)):(\d+)"
)

# --- line-wrap repair ------------------------------------------------------

# A hard newline inside a sentence (PDF / terminal copy artifacts). Join
# when the previous line ends in a lowercase letter or comma and the next
# line starts in a lowercase letter — a clear prose continuation. This
# preserves paragraph breaks (blank line) and breaks after sentence
# terminators (. ! ? : ").
_RE_SOFT_NEWLINE = re.compile(r"([a-z,])\n([a-z])")

# --- whitespace / line breaks ---------------------------------------------

# Pure ASCII control chars that carry no speech and no line-break meaning.
# Everything below 0x20 except HT(0x09)/LF(0x0A) — plus DEL(0x7F) — is dropped.
# CR(0x0D) is normalized to LF before this runs, so it's safe to strip here.
_RE_CONTROL_STRIP = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Normalize all line-break flavors to \n: CRLF -> \n, lone CR -> \n.
_RE_LB_NORMALIZE = re.compile(r"\r\n?")

# Any run of line breaks (and surrounding whitespace) becomes a single
# sentence terminator. Runs after line-wrap repair, so only real line/
# paragraph breaks reach it — every surviving break marks a statement
# boundary, even lines that ended without punctuation.
_RE_NEWLINE = re.compile(r"[ \t]*\n+[ \t]*")

# Structural brackets carry no prosody and no speakable content; replace
# with a space (collapsed later) so "foo (bar) baz" -> "foo bar baz".
_RE_BRACKETS = re.compile(r"[\(\)\[\]\{\}]")

_RE_LINE_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_RE_MULTISPACE = re.compile(r"[ \t]{2,}")


def clean_for_tts(text: str) -> str:
    """Preprocess rendered plain text into TTS-friendly Clean Text."""
    if not text or not text.strip():
        return text

    # 1. Invisible / format characters.
    for ch, repl in _INVISIBLE_MAP.items():
        if ch in text:
            text = text.replace(ch, repl)

    # 2. Normalize all line-break flavors to \n, then strip ASCII control
    #    chars (< 0x20 except \t \n, plus DEL) — silent drop. Done early so
    #    control chars can't pollute the line-break pass below.
    text = _RE_LB_NORMALIZE.sub("\n", text)
    text = _RE_CONTROL_STRIP.sub("", text)

    # 3. Quotes.
    for ch, repl in _QUOTE_REPLACEMENTS.items():
        if ch in text:
            text = text.replace(ch, repl)

    # 4. Dashes.
    text = _RE_EMDASH.sub(", ", text)
    text = _RE_ENDASH_RANGE.sub(r"\1 to \2", text)
    text = _RE_ENDASH.sub("-", text)
    text = _RE_FIGDASH.sub(", ", text)

    # 5. Ellipsis -> three dots (espeak reads "..." as a pause).
    text = _RE_ELLIPSIS.sub("...", text)

    # 6. Bullets / middle dots.
    text = _RE_BULLET.sub("- ", text)

    # 7. Arrows.
    text = _RE_ARROW.sub(" to ", text)
    text = _RE_ARROW_LEFT.sub(" from ", text)
    text = _RE_ARROW_UP.sub(" up ", text)
    text = _RE_ARROW_DOWN.sub(" down ", text)

    # 8. Math / symbol -> prose.
    for ch, repl in _SYMBOL_MAP.items():
        if ch in text:
            text = text.replace(ch, repl)

    # 9. Ligatures.
    for ch, repl in _LIGATURE_MAP.items():
        if ch in text:
            text = text.replace(ch, repl)

    # 10. CJK punctuation.
    for ch, repl in _CJK_PUNCT_MAP.items():
        if ch in text:
            text = text.replace(ch, repl)

    # 11. file.py:123 -> file.py, line 123
    text = _RE_FILELINE.sub(r"\1, line \2", text)

    # 12. Line-wrap repair (conservative: only clear prose continuation).
    text = _RE_SOFT_NEWLINE.sub(r"\1 \2", text)

    # 13. Structural brackets -> space (no prosody, no speech).
    text = _RE_BRACKETS.sub(" ", text)

    # 14. Surviving line breaks -> sentence terminator ". ". Runs after
    #     line-wrap repair, so only real statement boundaries reach it.
    text = _RE_NEWLINE.sub(". ", text)

    # 15. Whitespace normalization.
    text = _RE_LINE_WS.sub("", text)
    text = _RE_MULTISPACE.sub(" ", text)

    # 16. Collapse stray ". ." runs (from break -> period adjacent to
    #     existing punctuation) and tidy spacing before periods.
    text = re.sub(r"(?:\s*\.\s*){2,}", ". ", text)
    text = re.sub(r"\s+\.", ".", text)

    # 17. Strip outer whitespace.
    text = text.strip()

    # 18. Strip any remaining Cf-format chars that weren't in the map.
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return text
