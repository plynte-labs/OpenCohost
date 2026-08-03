"""TTS text sanitization — pure functions moved verbatim out of llm_engine.py
(Phase C2, refactor_core_api_20260802/proposal.md). No state, no locks;
MotorVocalIA keeps thin delegating staticmethods so no caller/test changes.
"""
import re

from opencohost.config.logger import get_logger

logger = get_logger()

_TTS_MARKDOWN_EMPHASIS_RE = re.compile(r"(?<![\w])(\*{1,3})(?!\s)([^*\n]+?)(?<!\s)\1(?![\w])")
_TTS_MARKDOWN_OPERATOR_CHARS = set("=+*/<>\\|")

# Unit 1.2 (runtime_findings_batch_20260731) — a cloud model can emit non-Latin
# glyphs (CJK/Arabic/Cyrillic/emoji/...). The SCREEN keeps them (owner ruling);
# espeak-ng would otherwise generate a spoken *description* of the glyph from
# the character itself — that description is never in our text, so there is no
# marker to filter, only characters to strip before espeak ever sees them.
#
# Small, conservative verbalization map: symbols outside ASCII that are common
# enough in dialogue to spell out instead of silently dropping. Anything not
# on this list falls through to the strip pass below. `=`/`%`/`$` are ASCII
# and already reach espeak untouched (see test_tts_sanitizer_keeps_math_and_
# code_like_asterisks) — do not add them here, that would change behavior a
# passing test already pins.
_TTS_MATH_SYMBOL_VERBALIZATION = {
    "±": " más menos ",
    "×": " por ",
    "÷": " entre ",
}

# Non-Latin script ranges to strip, by Unicode block — NOT a glyph enumeration.
# `contains_emoji_or_symbol` (kira_agenda_controller.py) cannot be reused here:
# it only tests `ord > 0xFFFF` or 0x2600-0x27BF, so CJK/kana/hangul/Arabic/
# Hebrew/Greek/Cyrillic (all BMP, all below 0x2600) would sail through, and it
# raises rather than returning cleaned text.
_TTS_NON_LATIN_RANGES = (
    (0x0300, 0x036F, None),        # combining diacritics — handled as "keep" below, not here
    (0x0370, 0x03FF, "greek"), (0x1F00, 0x1FFF, "greek"),
    (0x0400, 0x052F, "cyrillic"),
    (0x0590, 0x05FF, "hebrew"),
    (0x0600, 0x06FF, "arabic"), (0x0750, 0x077F, "arabic"),
    (0xFB50, 0xFDFF, "arabic"), (0xFE70, 0xFEFF, "arabic"),
    (0x1100, 0x11FF, "hangul"), (0x3130, 0x318F, "hangul"), (0xAC00, 0xD7A3, "hangul"),
    (0x3040, 0x309F, "kana"), (0x30A0, 0x30FF, "kana"),
    (0x2E80, 0x2EFF, "cjk"), (0x3000, 0x303F, "cjk"), (0x3400, 0x4DBF, "cjk"),
    (0x4E00, 0x9FFF, "cjk"), (0xF900, 0xFAFF, "cjk"), (0x20000, 0x2FFFF, "cjk"),
    (0x2600, 0x27BF, "emoji"), (0x1F000, 0x1FFFF, "emoji"), (0x1F1E6, 0x1F1FF, "emoji"),
    (0x2500, 0x259F, "symbol"),
)
# Smart punctuation LLMs commonly emit that is not ASCII but is speakable/
# harmless to keep as-is (dashes, curly quotes, ellipsis).
_TTS_KEEP_PUNCT_CODEPOINTS = frozenset({0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026})


def _tts_is_keep_char(ch: str) -> bool:
    """True for Latin script (incl. accents), digits, whitespace, common punctuation."""
    if ch.isspace():
        return True
    cp = ord(ch)
    if cp < 0x80:  # Basic Latin: ASCII letters/digits/punctuation
        return True
    if 0x00A1 <= cp <= 0x00FF:  # Latin-1 Supplement: á é í ó ú ñ ü ¿ ¡ « » ° ... (× ÷ verbalized earlier)
        return True
    if 0x0100 <= cp <= 0x024F:  # Latin Extended-A/B
        return True
    if 0x0300 <= cp <= 0x036F:  # combining diacritics (NFD-decomposed accents)
        return True
    if cp in _TTS_KEEP_PUNCT_CODEPOINTS:
        return True
    return False


def _tts_classify_non_latin_char(ch: str) -> str:
    cp = ord(ch)
    for start, end, label in _TTS_NON_LATIN_RANGES:
        if label and start <= cp <= end:
            return label
    return "other"


def _tts_cleanup_punctuation(text: str) -> str:
    """Collapse whitespace/punctuation artifacts left by stripping characters.

    e.g. "dijo  , y" -> "dijo, y"; a clause reduced to bare punctuation
    (".", ".") collapses into a single "."; no leading commas.
    """
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\.{2}(?!\.)", ".", text)  # exactly two dots -> one; keep "..." ellipsis
    text = re.sub(r"([,;:!?])\1+", r"\1", text)
    text = re.sub(r"^[\s,;:]+", "", text)
    return text.strip()


def _tts_strip_non_latin(text: str) -> str:
    """Verbalize a tiny symbol allowlist, strip everything non-Latin, clean up.

    Returns the SAME object when nothing needed changing (preserves the
    identity fast-path the markdown stage below relies on).
    """
    working = text
    for symbol, replacement in _TTS_MATH_SYMBOL_VERBALIZATION.items():
        if symbol in working:
            working = working.replace(symbol, replacement)

    changed = working is not text
    removed_counts: dict[str, int] = {}
    kept_chars = []
    for ch in working:
        if _tts_is_keep_char(ch):
            kept_chars.append(ch)
            continue
        changed = True
        category = _tts_classify_non_latin_char(ch)
        removed_counts[category] = removed_counts.get(category, 0) + 1

    if not changed:
        return text

    cleaned = _tts_cleanup_punctuation("".join(kept_chars))
    if removed_counts:
        # Metadata only — counts and category names, never the removed text.
        logger.debug(
            "[TTS_SANITIZE] non_latin_stripped chars=%d categories=%s",
            sum(removed_counts.values()), ",".join(sorted(removed_counts)),
        )
    return cleaned


def _first_sentence(text: str) -> str:
    """Return the first sentence of text (split on . ! ?)."""
    # Split on sentence-ending punctuation followed by whitespace or end-of-string
    match = re.search(r'[.!?](?:\s|$)', text)
    if match:
        return text[: match.start() + 1].strip()
    return text.strip()


def _sanitize_tts_text_for_playback(text: str) -> str:
    """Strip Markdown emphasis markers and non-Latin script glyphs, without
    deleting otherwise-speakable text.

    Screen/speech split: this runs inside _hablar_impl, AFTER _emit_dialogue
    already forwarded the original (unfiltered) string to the screen sink —
    the screen keeps CJK/etc glyphs, only the TTS-bound copy is filtered.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = _tts_strip_non_latin(text)
    if "*" not in text:
        return text

    def replace_emphasis(match: re.Match) -> str:
        inner = match.group(2)
        if not any(ch.isalpha() for ch in inner):
            return match.group(0)
        if any(ch in _TTS_MARKDOWN_OPERATOR_CHARS for ch in inner):
            return match.group(0)
        return inner

    return _TTS_MARKDOWN_EMPHASIS_RE.sub(replace_emphasis, text)
