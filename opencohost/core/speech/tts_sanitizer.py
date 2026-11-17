"""TTS text sanitization — pure functions moved verbatim out of llm_engine.py
(Phase C2, refactor_core_api_20260802/proposal.md). No state, no locks;
MotorVocalIA keeps thin delegating staticmethods so no caller/test changes.
"""

import re

from opencohost.config.logger import get_logger
from opencohost.core.speech.pronunciation import apply_pronunciation
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

# Protocol/reasoning and unsafe HTML blocks must never be spoken.  Matching is
# stack-based below, rather than a cross-tag regex: a mismatched closer cannot
# establish a safe boundary, so the opener drops through EOF.
_TTS_CONTROL_BLOCK_TAG_RE = re.compile(
    r"</(?P<closing>think|analysis|reasoning|tool_call|tool_response|script|style|svg|template)\s*>"
    r"|<(?P<opening>think|analysis|reasoning|tool_call|tool_response|script|style|svg|template)(?=[\s/>])[^>]*>",
    re.IGNORECASE,
)
_TTS_SPECIAL_TOKENS_RE = re.compile(
    r"(?i)<\|(?:assistant|user|system|im_start|im_end|endoftext)\|>|\[/?INST\]|<<?/?SYS>>?|<s>|</s>"
)

# Comments are separately fail-closed; their `-->` delimiter has no tag name
# to balance. Unsafe HTML tags share `_TTS_CONTROL_BLOCK_TAG_RE` above.
_TTS_HTML_COMMENT_RE = re.compile(r"<!--.*?(-->|$)", re.DOTALL)

# HTML tags: <br>, presentation tags (<b>, <strong>, <i>, <em>, <p>, <span>, <div>),
# autolinks (<https://example.com/docs>), and placeholder angle brackets (<usuario>, <Importante>).
_TTS_BR_RE = re.compile(r"(?i)<br\s*/?>")
_TTS_HTML_DIV_P_CLOSE_RE = re.compile(r"(?i)</(?:p|div)>")
_TTS_HTML_PRESENTATION_TAG_RE = re.compile(
    r"(?i)</?(?:b|strong|i|em|p|span|div)(?:\s+[^>]*)?>"
)
# Structural table tags (live-session-fixes-20261005 L1): a degenerate reply of
# `<th>` x32 was spoken as the word "th" 32 times, because the placeholder rule
# below turns `<th>` into its inner word. Dropped as a space so cells never glue.
_TTS_HTML_TABLE_TAG_RE = re.compile(
    r"(?i)</?(?:table|thead|tbody|tfoot|tr|th|td)(?:\s+[^>]*)?/?>"
)
# Reasoning closers whose orphan form means "everything before me was reasoning"
# (a template-injected opening tag), mirroring `strip_think_blocks`.
_TTS_REASONING_TAGS = frozenset({"think", "analysis", "reasoning"})
# Degenerate sentence (L1): at least this many word tokens, and one token is at
# least this share of them ("th th th th th th").
_TTS_DEGENERATE_MIN_TOKENS = 6
_TTS_DEGENERATE_SHARE = 0.8
_TTS_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_TTS_WORD_TOKEN_RE = re.compile(r"\w+")
_TTS_AUTOLINK_RE = re.compile(r"<((?:https?|ftp)://[^\s>]+)>", re.IGNORECASE)
_TTS_PLACEHOLDER_RE = re.compile(
    r"<([a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9_](?:[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9_\s.\-]*?[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9_])?)>"
)

# Hashtags / lone hash signs: Piper reads "#" as "almohadilla" (live 2026-10-08,
# vision read-aloud of "#claude #ia"). A hash glued to a preceding word ("C#") stays.
_TTS_HASH_SIGN_RE = re.compile(r"(?<!\w)#")

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
# B1 candidates are checked by `_tts_is_inline_latex_body`: prose between two
# currency markers or isolated amounts ($5 y $10) are left intact, while math formulas
# ($O(N)$, $x^2$, $x + y = z$) are recognized as inline formulas.
_TTS_INLINE_LATEX_RE = re.compile(r"(?<![\w\\])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w])")
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

# Sequence/flow arrows and LaTeX text wrappers
_TTS_LATEX_TEXT_WRAPPER_RE = re.compile(
    r"\\(?:text|mathrm|mathbf|mathit)\{([^}]+)\}"
)
_TTS_ARROW_IMPLIES_RE = re.compile(
    r"(?:\$?\s*\\(?:Longrightarrow|Rightarrow|implies)\b\s*\$?)"
    r"|(?<=\s)(?:==>|=>)(?=\s)"
    r"|[⇒]"
)
_TTS_ARROW_STEP_RE = re.compile(
    r"(?:\$?\s*\\(?:longrightarrow|rightarrow|to|mapsto)\b\s*\$?)"
    r"|(?<=\s)(?:-->|->)(?=\s)"
    r"|[→➔➜]"
)
_TTS_ARROW_LEFT_RE = re.compile(
    r"(?:\$?\s*\\(?:longleftarrow|leftarrow)\b\s*\$?)"
    r"|(?<=\s)(?:<--|<-)(?=\s)"
    r"|[←]"
)

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
_TTS_MATH_SYMBOL_VERBALIZATION_ES = {
    "±": " más menos ",
    "×": " por ",
    "÷": " entre ",
    "→": " pasa a ",
    "➔": " pasa a ",
    "➜": " pasa a ",
    "⇒": " implica ",
    "←": " viene de ",
}

_TTS_MATH_SYMBOL_VERBALIZATION_EN = {
    "±": " plus or minus ",
    "×": " times ",
    "÷": " divided by ",
    "→": " leads to ",
    "➔": " leads to ",
    "➜": " leads to ",
    "⇒": " implies ",
    "←": " comes from ",
}

_TTS_MATH_SYMBOL_VERBALIZATIONS = {
    "es": _TTS_MATH_SYMBOL_VERBALIZATION_ES,
    "en": _TTS_MATH_SYMBOL_VERBALIZATION_EN,
}

# Backward compatibility alias
_TTS_MATH_SYMBOL_VERBALIZATION = _TTS_MATH_SYMBOL_VERBALIZATION_ES


def _resolve_verbalization_map(lang: str | None = None) -> dict[str, str]:
    """Return localized symbol verbalization map with fallback to Spanish."""
    target_lang = lang
    if not target_lang:
        try:
            bundle = i18n_active.get_active_bundle()
            if bundle and getattr(bundle, "code", None):
                target_lang = bundle.code
        except Exception:
            target_lang = "es"

    if target_lang:
        try:
            from opencohost.i18n.coherence import primary_subtag
            clean_lang = primary_subtag(target_lang)
        except Exception:
            clean_lang = str(target_lang).split("-", 1)[0].strip().lower()
    else:
        clean_lang = "es"

    return _TTS_MATH_SYMBOL_VERBALIZATIONS.get(clean_lang, _TTS_MATH_SYMBOL_VERBALIZATION_ES)

# Non-Latin script ranges to strip, by Unicode block — NOT a glyph enumeration.
# `contains_emoji_or_symbol` (kira_agenda_controller.py) cannot be reused here:
# it only tests `ord > 0xFFFF` or 0x2600-0x27BF, so CJK/kana/hangul/Arabic/
# Hebrew/Greek/Cyrillic (all BMP, all below 0x2600) would sail through, and it
# raises rather than returning cleaned text.
_TTS_NON_LATIN_RANGES = (
    (0x0300, 0x036F, None),  # combining diacritics — handled as "keep" below, not here
    (0x0370, 0x03FF, "greek"),
    (0x1F00, 0x1FFF, "greek"),
    (0x0400, 0x052F, "cyrillic"),
    (0x0590, 0x05FF, "hebrew"),
    (0x0600, 0x06FF, "arabic"),
    (0x0750, 0x077F, "arabic"),
    (0xFB50, 0xFDFF, "arabic"),
    (0xFE70, 0xFEFF, "arabic"),
    (0x1100, 0x11FF, "hangul"),
    (0x3130, 0x318F, "hangul"),
    (0xAC00, 0xD7A3, "hangul"),
    (0x3040, 0x309F, "kana"),
    (0x30A0, 0x30FF, "kana"),
    (0x2E80, 0x2EFF, "cjk"),
    (0x3000, 0x303F, "cjk"),
    (0x3400, 0x4DBF, "cjk"),
    (0x4E00, 0x9FFF, "cjk"),
    (0xF900, 0xFAFF, "cjk"),
    (0x20000, 0x2FFFF, "cjk"),
    (0x2600, 0x27BF, "emoji"),
    (0x1F000, 0x1FFFF, "emoji"),
    (0x1F1E6, 0x1F1FF, "emoji"),
    (0x2500, 0x259F, "symbol"),
)
# Smart punctuation LLMs commonly emit that is not ASCII but is speakable/
# harmless to keep as-is (dashes, curly quotes, ellipsis).
_TTS_KEEP_PUNCT_CODEPOINTS = frozenset(
    {0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026}
)


def _tts_is_keep_char(ch: str) -> bool:
    """True for Latin script (incl. accents), digits, whitespace, common punctuation."""
    if ch.isspace():
        return True
    cp = ord(ch)
    if cp < 0x80:  # Basic Latin: ASCII letters/digits/punctuation
        return True
    if (
        0x00A1 <= cp <= 0x00FF
    ):  # Latin-1 Supplement: á é í ó ú ñ ü ¿ ¡ « » ° ... (× ÷ verbalized earlier)
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
    # 1. Clean curly braces (preserving format placeholders like {count}) and empty bracket pairs
    def _preserve_placeholders(m: re.Match) -> str:
        inner = m.group(1)
        if re.fullmatch(r"[a-zA-Z0-9_]+", inner):
            return m.group(0)
        return f" {inner} "

    text = re.sub(r"\{([^{}]*)\}", _preserve_placeholders, text)
    text = re.sub(r"\(\s*\)", "", text)

    # 2. Conversational parentheses to commas, preserving math/identifier calls like O(N), f(x)
    text = re.sub(r"(?<![A-Za-z0-9_])\(([^()]+)\)", r", \1, ", text)
    text = re.sub(r"(?<![A-Za-z0-9_])[()]", " ", text)

    # 3. Collapse whitespace and spaces before punctuation
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)

    # 4. Standardize ellipsis to '...' (preserved for reflective speech pauses)
    text = text.replace("\u2026", "...")
    text = re.sub(r"\.{3,}", "...", text)
    text = re.sub(
        r"(?<!\.)\.{2}(?!\.)", ".", text
    )  # exactly two dots -> one; preserve three-dot ellipsis

    # 5. Collapse duplicate punctuation and cleanup comma preceding terminal punctuation
    text = re.sub(r"([,;:!?])\1+", r"\1", text)
    text = re.sub(r",\s*([.!?…])", r"\1", text)
    text = re.sub(r"\\([a-zA-ZáéíóúÁÉÍÓÚñÑüÜ]+)", r"\1", text)
    text = re.sub(r"\s*\\\s*", " ", text)
    text = re.sub(r"^[\s,;:]+", "", text)
    res = text.strip()
    return res


def _tts_drop_control_blocks(text: str) -> tuple[str, bool]:
    """Drop recognized private/unsafe blocks with nested-tag balancing.

    An unmatched opener or a closer for a different tag fails closed through
    EOF.  Orphaned recognized closers are removed without discarding following
    public text.  When the FIRST recognized tag is an orphan reasoning closer
    (``</think>``, ``</analysis>``, ``</reasoning>`` with no opener: the model's
    template injected the opening tag), the text before it is reasoning and is
    dropped too (live-session-fixes-20261005 L1).
    """
    safe_parts: list[str] = []
    cursor = 0
    stack: list[str] = []
    seen_tag = False

    for match in _TTS_CONTROL_BLOCK_TAG_RE.finditer(text):
        opening = match.group("opening")
        tag = (opening or match.group("closing")).lower()
        first_tag = not seen_tag
        seen_tag = True

        if stack:
            if opening:
                stack.append(tag)
                continue
            if tag != stack[-1]:
                return "".join(safe_parts), True
            stack.pop()
            if not stack:
                cursor = match.end()
            continue

        if not opening and first_tag and tag in _TTS_REASONING_TAGS:
            safe_parts = []
        else:
            safe_parts.append(text[cursor : match.start()])
        cursor = match.end()
        if opening:
            stack.append(tag)

    if stack:
        return "".join(safe_parts), True

    result = "".join(safe_parts) + text[cursor:]
    return result, result != text


def _tts_is_inline_latex_body(body: str) -> bool:
    """Accept explicit TeX syntax, subscripts, Big-O notation, and math formulas.

    A dollar-delimited span with prose or bare currency amounts ($5 y $10) is
    left intact unless it carries a clear mathematical signal.
    """
    body_clean = body.strip()
    if not body_clean:
        return False
    if any(marker in body_clean for marker in ("\\", "^", "{", "}")):
        return True
    if re.search(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+", body_clean):
        return True
    if re.search(r"\bO\s*\([^$]+\)", body_clean):
        return True
    if re.search(r"[A-Za-z0-9]\s*[=+\-*/<>≤≥≠≈±×÷]\s*[A-Za-z0-9]", body_clean):
        return True
    if re.search(r"\b[A-Za-z]\s*\([A-Za-z0-9,\s+\-*/]+\)", body_clean):
        return True
    if re.fullmatch(r"[A-Za-z](?:[0-9]+)?", body_clean):
        return True
    return False


def _tts_strip_non_latin(text: str, lang: str | None = None) -> str:
    """Verbalize a tiny symbol allowlist, strip everything non-Latin, clean up.

    Returns the SAME object when nothing needed changing (preserves the
    identity fast-path the markdown stage below relies on).
    """
    working = text
    verbalization_map = _resolve_verbalization_map(lang)
    for symbol, replacement in verbalization_map.items():
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
            sum(removed_counts.values()),
            ",".join(sorted(removed_counts)),
        )
    return cleaned


def _first_sentence(text: str) -> str:
    """Return the first sentence of text (split on . ! ?)."""
    # Split on sentence-ending punctuation followed by whitespace or end-of-string
    match = re.search(r"[.!?](?:\s|$)", text)
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
                non_separator = [
                    ln for ln in block if not _TTS_TABLE_SEPARATOR_RE.match(ln)
                ]
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

    # Leading asterisks attached to a word, digit, or opening paren (e.g. "*hola", "¿*seguro", "**100", "**(algo)")
    working = re.sub(r"(^|[\s¿¡\"'(\[])\*+([a-zA-Z0-9áéíóúÁÉÍÓÚñÑüÜ(\[])", r"\1\2", working)

    # Trailing asterisks attached to a word, digit, or closing paren (e.g. "hola*", "seguro*?", "100**", "algo)**")
    working = re.sub(
        r"([a-zA-Z0-9áéíóúÁÉÍÓÚñÑüÜ)\]])\*+($|[\s.,;:!?)\"'\]])",
        r"\1\2",
        working,
    )

    # Clean any residual double-or-more asterisks not part of digit exponentiation (e.g. '2 ** 8')
    # If '**' is not surrounded by digits on both sides, strip it so Piper never says "asterisco asterisco"
    working = re.sub(r"(?<!\d\s)\*{2,}(?!\s*\d)", "", working)

    if working != text:
        working = re.sub(r" +", " ", working).strip()
    return working


def _tts_normalize_markdown(text: str, lang: str | None = None) -> str:
    """Convert markdown block/inline structure into plain spoken sentences.

    Must run BEFORE `_tts_strip_non_latin` (ordering is load-bearing):
    `_tts_cleanup_punctuation` collapses all whitespace, including newlines,
    to a single space, so block detection here needs the raw, still-lined
    text. Returns the SAME OBJECT when no rule fired (identity fast-path the
    rest of `_sanitize_tts_text_for_playback`'s chain depends on).
    """
    working = text
    changed = False

    # Private protocol/reasoning and unsafe HTML blocks are balanced by name;
    # an unclosed or mismatched block drops to EOF.
    working, block_changed = _tts_drop_control_blocks(working)
    changed = changed or block_changed

    working, n = _TTS_SPECIAL_TOKENS_RE.subn("", working)
    changed = changed or n > 0

    # Comments have no named close tag, but are also fail-closed to EOF.
    working, n = _TTS_HTML_COMMENT_RE.subn("", working)
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

    working, n = _TTS_HTML_TABLE_TAG_RE.subn(" ", working)
    changed = changed or n > 0

    # Presentation-tag stripping can expose a previously split control tag
    # (for example `<thi<b></b>nk>`). Re-check before placeholder cleanup can
    # remove its angle brackets and make the private text look innocuous.
    working, block_changed = _tts_drop_control_blocks(working)
    changed = changed or block_changed

    # Autolinks: <https://example.com/docs> -> https://example.com/docs
    working, n = _TTS_AUTOLINK_RE.subn(r"\1", working)
    changed = changed or n > 0

    # Placeholder angle brackets (<usuario>, <Importante>) -> inner word
    working, n = _TTS_PLACEHOLDER_RE.subn(r"\1", working)
    changed = changed or n > 0

    working, block_changed = _tts_stage_a_line_rules(working)
    changed = changed or block_changed

    # Stage B0: Normalize LaTeX text wrappers and sequence/flow arrows before inline math
    working, n = _TTS_LATEX_TEXT_WRAPPER_RE.subn(r"\1", working)
    changed = changed or n > 0

    target_lang = lang
    if not target_lang:
        try:
            bundle = i18n_active.get_active_bundle()
            target_lang = bundle.code if bundle else "es"
        except Exception:
            target_lang = "es"

    try:
        from opencohost.i18n.coherence import primary_subtag
        clean_lang = primary_subtag(target_lang)
    except Exception:
        clean_lang = str(target_lang).split("-", 1)[0].strip().lower()

    if clean_lang == "en":
        arrow_implies = "implies"
        arrow_step = "leads to"
        arrow_left = "comes from"
    else:
        arrow_implies = i18n_active.tts_markdown_arrow_implies()
        arrow_step = i18n_active.tts_markdown_arrow_step()
        arrow_left = "viene de"

    working, n = _TTS_ARROW_IMPLIES_RE.subn(
        lambda _m: f", {arrow_implies}, ", working
    )
    changed = changed or n > 0

    working, n = _TTS_ARROW_STEP_RE.subn(
        lambda _m: f", {arrow_step}, ", working
    )
    changed = changed or n > 0

    working, n = _TTS_ARROW_LEFT_RE.subn(f", {arrow_left}, ", working)
    changed = changed or n > 0

    def _replace_inline_latex(match: re.Match) -> str:
        body = match.group(1)
        if _tts_is_inline_latex_body(body):
            return i18n_active.tts_markdown_formula_inline()
        if body and (body[0].isdigit() or body.strip().startswith("$")):
            return match.group(0)
        return body

    before_latex = working
    working = _TTS_INLINE_LATEX_RE.sub(_replace_inline_latex, working)
    changed = changed or working != before_latex

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

    working, n = _TTS_HASH_SIGN_RE.subn("", working)
    changed = changed or n > 0

    if not changed:
        return text
    return _tts_cleanup_punctuation(working)


_TTS_SPEAKER_PREFIX_RE = re.compile(
    r"^\s*(?:\*{1,2})?(?:"
    r"(?:\[|\()?(?:kira|akira|assistant|asistente|co-?host|host|bot|ai|ia)(?:\]|\))?\s*(?:\*{1,2})?\s*[:：]"
    r"|\[(?:kira|akira|assistant|asistente|co-?host|host|bot|ai|ia)\]\s*[-–—:]?"
    r"|\b(?:assistant|asistente|co-?host|bot)\b\s*[-–—:]"
    r")\s*(?:\*{1,2})?\s*",
    re.IGNORECASE,
)


def _tts_strip_speaker_prefix(text: str) -> str:
    """Remove leading character/role prefixes like 'Kira:', 'Assistant:', '[Kira]:'
    that an LLM might emit due to conversational script pattern completion."""
    if not text:
        return text
    prev = None
    curr = text
    while curr != prev:
        prev = curr
        curr = _TTS_SPEAKER_PREFIX_RE.sub("", curr)
    return curr.strip()


def _sanitize_tts_text_for_playback(text: str, lang: str | None = None) -> str:
    """Strip Markdown emphasis markers and non-Latin script glyphs, without
    deleting otherwise-speakable text, then apply the owner pronunciation
    dictionary as the final text stage.

    Screen/speech split: this runs inside _hablar_impl, AFTER _emit_dialogue
    already forwarded the original (unfiltered) string to the screen sink —
    the screen keeps CJK/etc glyphs, only the TTS-bound copy is filtered.

    This is the SPOKEN form of a whole text: the output guard judges it, so
    the dictionary can never be used to evade a rule. Fragmentation and every
    display-facing record use `_sanitize_tts_text_for_display` instead, and the
    synthesizer applies `_spoken_text_for_fragment` per fragment
    (follow-reading-pronunciation R1). Both orders yield the same spoken text.
    """
    return _spoken_text_for_fragment(_sanitize_tts_text_core(text, lang=lang), lang=lang)


def _sanitize_tts_text_for_display(text: str, lang: str | None = None) -> str:
    """Every safety stage of the playback sanitizer, WITHOUT the pronunciation
    dictionary. Speech fragments are cut from this text, so speech progress and
    the reply sink carry words the UI can find in the displayed reply."""
    return _sanitize_tts_text_core(text, lang=lang)


def sanitize_for_tts(text: str, lang: str | None = None) -> str:
    """Sanitize text for TTS speech playback, verbalizing math/symbols for the target locale.

    If ``lang`` is not provided, falls back to the active bundle locale (or Spanish 'es').
    """
    return _sanitize_tts_text_for_playback(text, lang=lang)


def _strip_control_markup_for_display(text: str) -> str:
    """Display copy of a streamed sentence (live-session-fixes-20261005 L1).

    Markdown is kept (the UI renders it, and the follow-reading match already
    works against it); only the private/structural markup the speech copy also
    drops is removed: control blocks (including an orphan reasoning closer and
    the reasoning before it), special tokens, HTML comments and table tags.
    Returns `text` itself when none was present.
    """
    if not text:
        return text or ""
    working, changed = _tts_drop_control_blocks(text)
    for pattern, replacement in (
        (_TTS_SPECIAL_TOKENS_RE, ""),
        (_TTS_HTML_COMMENT_RE, ""),
        (_TTS_HTML_TABLE_TAG_RE, " "),
    ):
        working, n = pattern.subn(replacement, working)
        changed = changed or n > 0
    if not changed:
        return text
    return re.sub(r" {2,}", " ", working).strip()


def _spoken_text_for_fragment(fragment: str, lang: str | None = None) -> str:
    """Apply the owner pronunciation dictionary to one already-sanitized
    fragment, at synthesis time only. Returns `fragment` itself when nothing
    matched. A replacement can join with neighbouring text or carry a special
    token, so every safety stage (without the dictionary) re-runs over a
    changed result: the dictionary can never reintroduce control blocks or
    tokens. The result never feeds back into guardrails or display records.
    """
    replaced = apply_pronunciation(fragment, locale=lang)
    if replaced == fragment:
        return fragment
    return _sanitize_tts_text_core(replaced, lang=lang)


def _tts_is_degenerate_sentence(sentence: str) -> bool:
    tokens = _TTS_WORD_TOKEN_RE.findall(sentence.lower())
    if len(tokens) < _TTS_DEGENERATE_MIN_TOKENS:
        return False
    top = max(tokens.count(token) for token in set(tokens))
    return top / len(tokens) >= _TTS_DEGENERATE_SHARE


def _tts_drop_degenerate_sentences(text: str) -> str:
    """Drop sentences that are one token repeated (L1, live log 13:40:08).
    Returns the SAME object when nothing was dropped."""
    if not text:
        return text
    sentences = _TTS_SENTENCE_SPLIT_RE.split(text)
    kept = [s for s in sentences if not _tts_is_degenerate_sentence(s)]
    if len(kept) == len(sentences):
        return text
    return " ".join(s.strip() for s in kept if s.strip())


def _sanitize_tts_text_core(text: str, lang: str | None = None) -> str:
    return _tts_drop_degenerate_sentences(_sanitize_tts_text_stages(text, lang=lang))


def _sanitize_tts_text_stages(text: str, lang: str | None = None) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    # 1. Drop control blocks first (<think>, HTML tags, etc.)
    text, block_changed = _tts_drop_control_blocks(text)
    if block_changed:
        text = _tts_cleanup_punctuation(text)
    # 2. Normalize markdown (headings, bullets, tables, **emphasis**, code, links)
    text = _tts_normalize_markdown(text, lang=lang)
    # 3. Strip speaker prefixes (now that control blocks and markdown formatting are removed)
    text = _tts_strip_speaker_prefix(text)
    # 4. Strip non-Latin glyphs
    text = _tts_strip_non_latin(text, lang=lang)
    # Non-Latin stripping can remove zero-width obfuscators and reconstruct a
    # control tag. Re-check the final speech-bound text in the same call.
    text, block_changed = _tts_drop_control_blocks(text)
    if block_changed:
        text = _tts_cleanup_punctuation(text)
    if "*" not in text:
        cleaned = _tts_cleanup_punctuation(_tts_strip_speaker_prefix(text))
        return text if cleaned == text else cleaned

    def replace_emphasis(match: re.Match) -> str:
        is_double = match.group(1) is not None
        inner = match.group(2) if is_double else match.group(4)
        inner = inner.strip()
        if not any(ch.isalnum() for ch in inner):
            return match.group(0)
        if not is_double and any(ch in _TTS_MARKDOWN_OPERATOR_CHARS for ch in inner):
            return match.group(0)
        return inner

    text = _TTS_MARKDOWN_EMPHASIS_RE.sub(replace_emphasis, text)
    text = _tts_clean_residual_asterisks(text)
    # Emphasis cleanup can remove the characters splitting a control tag
    # (for example `<**think**>`). This is the final text-producing stage.
    text, block_changed = _tts_drop_control_blocks(text)
    text = _tts_cleanup_punctuation(text)
    return _tts_strip_speaker_prefix(text)
