"""Inline ``<think>...</think>`` reasoning split, shared by every LLM transport.

Pure module, no engine imports. Moved out of ``cloud_llm_client`` (where it
was written for nim-cloud-reasoning-off T3 and live_session_fixes_20260925
T8) so the LOCAL Ollama path applies the same split (live-session-fixes-
20261005 L1): a community fine-tune can emit its reasoning, or an orphan
``</think>``, inside ``content`` exactly like a cloud reasoning model.
"""

import re
from typing import Optional

# Case-insensitive; DOTALL so the reasoning body can span multiple lines.
THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL)
THINK_OPEN_TAG = "<think>"
THINK_CLOSE_TAG = "</think>"


def strip_think_blocks(content: str, thinking: str) -> tuple[str, str]:
    """Move inline ``<think>...</think>`` reasoning out of ``content`` into
    ``thinking`` (nim-cloud-reasoning-off, T3).

    Case-insensitive. Also handles a leading, never-closed ``<think>`` (a
    stream cut mid-reasoning): the whole remainder is reasoning and content
    becomes ``""``. Content with no ``<think`` tag at all is returned
    unchanged -- no stripping, no whitespace trim.
    """
    if not content or "think>" not in content.lower():
        return content, thinking

    extracted: list[str] = []

    # Template-injected opening tag (DeepSeek-R1 style): the model only emits
    # the closing tag, so everything before the first one is reasoning.
    lowered = content.lower()
    close_at = lowered.find(THINK_CLOSE_TAG)
    if close_at != -1 and THINK_OPEN_TAG not in lowered[:close_at]:
        extracted.append(content[:close_at].strip())
        content = content[close_at + len(THINK_CLOSE_TAG):]

    def _take(match: re.Match) -> str:
        extracted.append(match.group(1).strip())
        return ""

    remaining = THINK_BLOCK_RE.sub(_take, content)

    lstripped = remaining.lstrip()
    if lstripped.lower().startswith(THINK_OPEN_TAG):
        extracted.append(lstripped[len(THINK_OPEN_TAG):].strip())
        remaining = ""
    else:
        remaining = remaining.strip()

    combined = "\n".join(part for part in [(thinking or "").strip(), *extracted] if part)
    return remaining, combined


class ThinkTagStreamSplitter:
    """Routes inline ``<think>...</think>`` text out of streamed content deltas
    (live_session_fixes_20260925 T8), the streaming analogue of
    ``strip_think_blocks`` above. A tag can split across SSE chunks, so this
    holds back up to ``len("</think>") - 1`` unclassified trailing chars
    between ``feed`` calls instead of assuming a tag never crosses a chunk
    boundary.

    Also handles an orphan ``</think>`` with no preceding ``<think>`` (a
    template-injected opening, DeepSeek-R1 style): only the VERY FIRST tag
    seen in the whole stream gets this treatment, matching
    ``strip_think_blocks``'s non-streaming behavior for the same case. That
    requires a SECOND, larger holdback before the first tag has ever been
    seen (``_PRE_FIRST_TAG_HOLDBACK`` below): once ordinary-looking text has
    been handed out as a content chunk it cannot be recalled, so if a bare
    reasoning preamble turns out to end in an orphan close, only the part of
    it still held back at that moment can be reclassified. This is a
    documented, BOUNDED residual (an orphan preamble longer than the holdback
    streams its excess as content), not a silent full guarantee -- accepted
    because the cost is a fixed, tiny addition to TTFA (the downstream
    SentenceSplitter withholds everything until a full sentence closes,
    ~25 tokens, or, with TTS_FIRST_CHUNK_BOOST on and think off, until the
    first soft mark after >= 4 words) and the alternative is holding back the entire first
    tag-free stream indefinitely.
    """

    _MAX_TAG_LEN = len(THINK_CLOSE_TAG)
    _PRE_FIRST_TAG_HOLDBACK = 64

    def __init__(self, pre_first_tag_holdback: Optional[int] = None) -> None:
        # The local Ollama path passes 0: its sentence splitter holds the
        # unspoken head (a full sentence, or up to the first soft mark when
        # the first-chunk boost is on and think is off), and its consumer
        # drops that pending text on an orphan close (`orphan_close_seen`).
        # A soft chunk already spoken is NOT recoverable; that leak is counted
        # by `[THINK_RESCUE] spoken_before=yes`. None keeps the class default.
        if pre_first_tag_holdback is not None:
            self._PRE_FIRST_TAG_HOLDBACK = max(0, int(pre_first_tag_holdback))
        self._buf = ""
        self.in_think = False
        self._seen_any_tag = False
        # True once the stream's first tag turned out to be an orphan close:
        # everything before it was reasoning. A consumer that already handed
        # earlier content downstream (a sentence splitter still holding it)
        # reads this to discard that pending text.
        self.orphan_close_seen = False

    @staticmethod
    def _partial_tag_suffix_len(buf: str) -> int:
        """Longest trailing slice of *buf* that is a proper prefix of either
        think tag -- i.e. chars a future feed() call could still turn into a
        complete tag. Zero when *buf* ends in ordinary text with no tag
        risk at all (e.g. "uno ", "dos ") -- such text is safe to emit now."""
        max_len = 0
        for tag in (THINK_OPEN_TAG, THINK_CLOSE_TAG):
            limit = min(len(buf), len(tag) - 1)
            for length in range(limit, 0, -1):
                if buf[-length:] == tag[:length]:
                    max_len = max(max_len, length)
                    break
        return max_len

    def feed(self, delta: str) -> tuple[str, str]:
        self._buf += delta or ""
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        while True:
            open_idx = self._buf.find(THINK_OPEN_TAG)
            close_idx = self._buf.find(THINK_CLOSE_TAG)
            if open_idx == -1 and close_idx == -1:
                break
            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
                idx, tag_len, is_open = close_idx, len(THINK_CLOSE_TAG), False
            else:
                idx, tag_len, is_open = open_idx, len(THINK_OPEN_TAG), True
            before = self._buf[:idx]
            orphan_close = (not is_open) and (not self.in_think) and (not self._seen_any_tag)
            if orphan_close:
                self.orphan_close_seen = True
            if self.in_think or orphan_close:
                thinking_parts.append(before)
            else:
                content_parts.append(before)
            self._buf = self._buf[idx + tag_len:]
            self.in_think = is_open
            self._seen_any_tag = True
        # No more complete tags in the buffer. Hold back a trailing slice:
        # at minimum whatever could still be a partial tag opener; MORE than
        # that, pre-first-tag, to bound the orphan-close residual above.
        tag_risk = self._partial_tag_suffix_len(self._buf)
        if self._seen_any_tag:
            holdback = tag_risk
        else:
            holdback = max(tag_risk, min(len(self._buf), self._PRE_FIRST_TAG_HOLDBACK))
        safe_len = max(0, len(self._buf) - holdback)
        emit, self._buf = self._buf[:safe_len], self._buf[safe_len:]
        if self.in_think:
            thinking_parts.append(emit)
        else:
            content_parts.append(emit)
        return "".join(content_parts), "".join(thinking_parts)

    def flush(self) -> tuple[str, str]:
        """At stream end: no more data can complete a pending partial tag, so
        whatever remains buffered is emitted as-is under the current state."""
        remaining, self._buf = self._buf, ""
        if self.in_think:
            return "", remaining
        return remaining, ""
