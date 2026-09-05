"""TTS text sanitization — pure functions moved verbatim out of llm_engine.py
(Phase C2, refactor_core_api_20260802/proposal.md). No state, no locks;
MotorVocalIA keeps thin delegating staticmethods so no caller/test changes.
"""
import re

from opencohost.config.logger import get_logger
from opencohost.i18n import active as i18n_active

logger = get_logger()

_TTS_MARKDOWN_EMPHASIS_RE = re.compile(
    r"(?<![\w*])(\*{2,})\s*([^*\n]+?)\s*\1(?![\w*])|(?<![\w*])(\*)(?!\s)([^*\n]+?)(?<!\s)\3(?![\w*])"
)
_TTS_MARKDOWN_OPERATOR_CHARS = set("=+*/\\|")

# F4 (interruptible_speech_architecture_20260804) — markdown structure the TTS
# copy must not narrate verbatim. Stage A (block rules): fenced code / tables /
# display math collapse to one spoken notice each; headings/bullets/blockquotes/
# hr are reshaped line-by-line. Stage B (inline): LaTeX-ish `$...$`, links,
# inline code, `__x__`/`_x_` emphasis, residual pipes.
_TTS_FENCED_CODE_RE = re.compile(r"(```+|~~~+).*?\1", re.DOTALL)
_TTS_DISPLAY_MATH_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)

# Protocol tokens & think/reasoning blocks LLMs emit that must never be spoken.
# Fail-closed: unclosed blocks drop to EOF so streaming truncation/cancels never leak.
_TTS_THINK_BLOCK_RE = re.compile(
    r"<(?:think|analysis|reasoning|tool_call|tool_response)(?:\s+[^>]*)?>.*?(?:</(?:think|analysis|reasoning|tool_call|tool_response)>|$)",
    re.DOTALL | re.IGNORECASE,
)
_TTS_SPECIAL_TOKENS_RE = re.compile(
    r"(?i)<\|(?:assistant|user|system|im_start|im_end|endoftext)\|>|\[/?INST\]|<<?/?SYS>>?|<s>|</s>"
)

# Unsafe HTML blocks (<script>, <style>, <svg>, <template>) and comments <!-- ... -->.
# Dropped completely with content (both complete and unclosed to EOF).
_TTS_UNSAFE_HTML_BLOCK_RE = re.compile(
    r"<!--.*?(-->|$)|<(?:script|style|svg|template)(?:\s+[^>]*)?>.*?(?:</(?:script|style|svg|template)>|$)",
    re.DOTALL | re.IGNORECASE,
)

# HTML tags: <br>, presentation tags (<b>, <strong>, <i>, <em>, <p>, <span>, <div>),
# autolinks (<https://example.com/docs>), and placeholder angle brackets (<usuario>, <Importante>).
_TTS_BR_RE = re.compile(r"(?i)<br\s*/?>")
_TTS_HTML_DIV_P_CLOSE_RE = re.compile(r"(?i)</(?:p|div)>")
_TTS_HTML_PRESENTATION_TAG_RE = re.compile(r"(?i)</?(?:b|strong|i|em|p|span|div)(?:\s+[^>]*)?>")
_TTS_AUTOLINK_RE = re.compile(r"<((?:https?|ftp)://[^\s>]+)>", re.IGNORECASE)
_TTS_PLACEHOLDER_RE = re.compile(
    r"<([a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9_](?:[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9_\s.\-]*?[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9_])?)>"
)

# Strikethrough ~~texto~~ -> texto
_TTS_STRIKETHROUGH_RE = re.compile(r"(?<!~)~~(?!\s)([^~\n]+?)(?<!\s)~~(?!~)")
_TTS_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
# The `|---|---|` / `|:---:|:---:|` alignment row: only pipes, dashes, colons,
# whitespace. Used to exclude that row (and the header row) from the spoken
# row count -- see _tts_stage_a_line_rules.
_TTS_TABLE_SEPARATOR_RE = re.compile(r"^[\s|:-]+$")
_TTS_ATX_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_TTS_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
# Capped at 1-3 digits (real list markers) so 4+ digit numbers -- years
# ("1999. Fue un buen año...") and quantities -- aren't mistaken for a list
# marker and silently lose their digits (reviewer BLOCKER).
_TTS_NUMBERED_RE = re.compile(r"^\s*\d{1,3}[.)]\s+(.*)$")
_TTS_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_TTS_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
# B1: requires a TeX-ish char inside the $...$ span so dollar amounts ("$5 y
# $10") are never touched -- only the display ($$...$$) form is unconditional.
_TTS_INLINE_LATEX_RE = re.compile(r"\$([^$\n]*[\\_^{][^$\n]*)\$")
_TTS_LINK_RE = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
_TTS_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# B4: `**x**`/`*x*` are deliberately NOT handled here -- the pre-existing
# _TTS_MARKDOWN_EMPHASIS_RE + keep-guard below already converts them correctly
# (and is pinned by test_tts_sanitizer_keeps_math_and_code_like_asterisks); this
# stage only adds the underscore styles nothing currently handles. Double-
# underscore must run before single so "__bold__" isn't half-eaten by the
# single-underscore pattern first. `(?<!\w)`/`(?!\w)` (word class includes "_")
# is what keeps snake_case ("mi_variable_nombre") untouched.
_TTS_DOUBLE_UNDERSCORE_RE = re.compile(r"(?<!\w)__(?!\s)([^_\n]+?)(?<!\s)__(?!\w)")
_TTS_SINGLE_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")
_TTS_PIPE_RE = re.compile(r"\|")

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
    text = re.sub(r"\.{3,}", ",", text)              # ellipsis → comma pause (Piper reads "..." as "punto")
    text = text.replace("\u2026", ",")                    # Unicode ellipsis (…) → same comma pause
    text = re.sub(r"\.{2}(?!\.)", ".", text)          # exactly two dots -> one; ellipsis already handled above
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


def _tts_ensure_terminal_punctuation(text: str) -> str:
    """Strip and add a trailing '.' unless the text already ends in . ! ?"""
    text = text.strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _tts_process_line_structural_prefixes(line: str) -> tuple[str | None, bool]:
    """Consume structural markdown prefixes iteratively on a single line:
    nested blockquotes (>>), blockquote + bullet (> -), blockquote + heading (> #),
    bullets/numbered items, and horizontal rules."""
    curr = line
    line_changed = False
    needs_terminal_punct = False

    while True:
        if _TTS_HR_RE.match(curr):
            return None, True

        m = _TTS_BLOCKQUOTE_RE.match(curr)
        if m:
            curr = m.group(1)
            line_changed = True
            continue

        m = _TTS_ATX_HEADING_RE.match(curr)
        if m:
            curr = m.group(1)
            line_changed = True
            needs_terminal_punct = True
            continue

        m = _TTS_BULLET_RE.match(curr) or _TTS_NUMBERED_RE.match(curr)
        if m:
            curr = m.group(1)
            line_changed = True
            needs_terminal_punct = True
            continue

        break

    if not line_changed:
        return line, False

    if needs_terminal_punct:
        curr = _tts_ensure_terminal_punctuation(curr)

    return curr, True


def _tts_stage_a_line_rules(text: str) -> tuple[str, bool]:
    """Line-based Stage A rules: A2 tables, A4 headings, A5 bullets/numbered,
    A6 blockquotes/horizontal rules. A1 (fenced code) and A3 (display math)
    run separately before this, as whole-text DOTALL passes, since they can
    span multiple lines."""
    lines = text.split("\n")
    out: list[str] = []
    changed = False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        if _TTS_TABLE_LINE_RE.match(line):
            j = i
            while j < n and _TTS_TABLE_LINE_RE.match(lines[j]):
                j += 1
            block = lines[i:j]
            if len(block) >= 2:
                # n = data rows only: the alignment separator and the header
                # row are structure, not rows Kira should claim exist.
                non_separator = [ln for ln in block if not _TTS_TABLE_SEPARATOR_RE.match(ln)]
                data_row_count = max(len(non_separator) - 1, 0)
                notice_template = i18n_active.tts_markdown_table_notice()
                try:
                    notice = notice_template.format(n=data_row_count)
                except Exception:
                    # Malformed/incompatible locale template must never crash
                    # the TTS path -- fall back to the raw notice string.
                    notice = notice_template
                out.append(notice)
                changed = True
                i = j
                continue

        res, line_changed = _tts_process_line_structural_prefixes(line)
        if line_changed:
            changed = True
        if res is not None:
            out.append(res)
        i += 1

    if not changed:
        return text, False
    return "\n".join(out), changed


def _tts_clean_residual_asterisks(text: str) -> str:
    """Clean lone residual formatting asterisks on word boundaries that are NOT
    math expressions (preserve 5*10=50, a*b, 2 ** 8, foo * bar, width * height)."""
    if "*" not in text:
        return text

    working = text

    # Bullet asterisks at start of string or line (e.g. "* hola" -> "hola")
    working = re.sub(r"(^|\n)\s*\*+\s+", r"\1", working)

    # Bullet asterisks after sentence punctuation (e.g. "Puntos: * uno" -> "Puntos: uno")
    working = re.sub(r"([.,;:!?])\s*\*+\s+", r"\1 ", working)

    # Lone formatting markers with 2+ asterisks (e.g. "uno ** dos" -> "uno dos")
    # Single '*' surrounded by spaces is preserved for multiplication and code.
    # Exponentiation like '2 ** 8' with digits is preserved.
    working = re.sub(r"(?<!\d)\s+\*{2,}\s+(?!\d)", " ", working)

    # Leading asterisks attached to a word (e.g. "*hola", "¿*seguro")
    working = re.sub(r"(^|[\s¿¡\"'(\[])\*+([a-zA-ZáéíóúÁÉÍÓÚñÑüÜ])", r"\1\2", working)

    # Trailing asterisks attached to a word (e.g. "hola*", "seguro*?")
    working = re.sub(
        r"([a-zA-ZáéíóúÁÉÍÓÚñÑüÜ])\*+($|[\s.,;:!?)\"'\]])",
        r"\1\2",
        working,
    )

    if working != text:
        working = re.sub(r" +", " ", working).strip()
    return working


def _tts_normalize_markdown(text: str) -> str:
    """Convert markdown block/inline structure into plain spoken sentences.

    Must run BEFORE `_tts_strip_non_latin` (ordering is load-bearing):
    `_tts_cleanup_punctuation` collapses all whitespace, including newlines,
    to a single space, so block detection here needs the raw, still-lined
    text. Returns the SAME OBJECT when no rule fired (identity fast-path the
    rest of `_sanitize_tts_text_for_playback`'s chain depends on).
    """
    working = text
    changed = False

    # Protocol tokens & think/reasoning blocks: dropped completely (fail-closed to EOF)
    working, n = _TTS_THINK_BLOCK_RE.subn("", working)
    changed = changed or n > 0

    working, n = _TTS_SPECIAL_TOKENS_RE.subn("", working)
    changed = changed or n > 0

    # Unsafe HTML blocks (<script>, <style>, <svg>, <template>) and comments <!-- ... -->
    working, n = _TTS_UNSAFE_HTML_BLOCK_RE.subn("", working)
    changed = changed or n > 0

    # Alternative & standard fenced code: ``` and ~~~ collapse to spoken notice
    working, n = _TTS_FENCED_CODE_RE.subn(
        lambda _m: i18n_active.tts_markdown_code_notice(), working
    )
    changed = changed or n > 0

    working, n = _TTS_DISPLAY_MATH_RE.subn(
        lambda _m: i18n_active.tts_markdown_formula_notice(), working
    )
    changed = changed or n > 0

    # HTML breaks (<br>) and presentation tags (<b>, <i>, <p>, <span>, <div>)
    working, n = _TTS_BR_RE.subn("\n", working)
    changed = changed or n > 0

    working, n = _TTS_HTML_DIV_P_CLOSE_RE.subn("\n", working)
    changed = changed or n > 0

    working, n = _TTS_HTML_PRESENTATION_TAG_RE.subn("", working)
    changed = changed or n > 0

    # Autolinks: <https://example.com/docs> -> https://example.com/docs
    working, n = _TTS_AUTOLINK_RE.subn(r"\1", working)
    changed = changed or n > 0

    # Placeholder angle brackets (<usuario>, <Importante>) -> inner word
    working, n = _TTS_PLACEHOLDER_RE.subn(r"\1", working)
    changed = changed or n > 0

    working, block_changed = _tts_stage_a_line_rules(working)
    changed = changed or block_changed

    working, n = _TTS_INLINE_LATEX_RE.subn(
        lambda _m: i18n_active.tts_markdown_formula_inline(), working
    )
    changed = changed or n > 0

    working, n = _TTS_LINK_RE.subn(r"\1", working)
    changed = changed or n > 0

    working, n = _TTS_INLINE_CODE_RE.subn(r"\1", working)
    changed = changed or n > 0

    # Strikethrough ~~texto~~ -> texto
    working, n = _TTS_STRIKETHROUGH_RE.subn(r"\1", working)
    changed = changed or n > 0

    working, n = _TTS_DOUBLE_UNDERSCORE_RE.subn(r"\1", working)
    changed = changed or n > 0

    working, n = _TTS_SINGLE_UNDERSCORE_RE.subn(r"\1", working)
    changed = changed or n > 0

    working, n = _TTS_PIPE_RE.subn(" ", working)
    changed = changed or n > 0

    if not changed:
        return text
    return _tts_cleanup_punctuation(working)


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
    text = _tts_normalize_markdown(text)
    text = _tts_strip_non_latin(text)
    if "*" not in text:
        return text

    def replace_emphasis(match: re.Match) -> str:
        inner = match.group(2) if match.group(2) is not None else match.group(4)
        inner = inner.strip()
        if not any(ch.isalpha() for ch in inner):
            return match.group(0)
        if any(ch in _TTS_MARKDOWN_OPERATOR_CHARS for ch in inner):
            return match.group(0)
        return inner

    text = _TTS_MARKDOWN_EMPHASIS_RE.sub(replace_emphasis, text)
    return _tts_clean_residual_asterisks(text)
