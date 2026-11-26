"""Lightweight contextual web query rewriter (Option B).

Rewrites a user's web search request into a short, self-contained query using
ONLY recent owner-sourced conversation turns (never viewer chat, never agenda,
never pasted-image OCR blocks). Pure helpers here perform zero I/O and never
log user text or queries.
"""
from __future__ import annotations

import re
from typing import Any, Sequence

from opencohost.core.web.trigger import QUERY_MAX_CHARS

HOST_SOURCES = frozenset({"direct", "ptt", "owner-bundle"})
REWRITE_MAX_MESSAGES = 4
REWRITE_MAX_CHARS_PER_MSG = 180
REWRITE_MAX_WORDS = 16
REWRITE_MAX_OUTPUT_CHARS = 140

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK = re.compile(r"<think>.*$", re.IGNORECASE | re.DOTALL)
_MENTION = re.compile(r"@\w+")
_LEADING_LABEL = re.compile(
    r"^(?:query|search\s*query|search|busqueda|búsqueda|consulta)\s*:\s*",
    re.IGNORECASE,
)
_LEADING_BULLET = re.compile(r"^[\-\*•\d.)\s]+")
_URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_REFUSAL_RE = re.compile(
    r"^(?:lo\s+siento|no\s+puedo|no\s+tengo\s+acceso|claro[,!:]|por\s+supuesto[,!:]|"
    r"i(?:'m|\s+am)?\s+sorry|i\s+can(?:'t|not)|sure[,!:]|here\s+is\b|here's\b)",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You rewrite a user's web search request into ONE short, self-contained web search query "
    "(2 to 10 words) in the same language as the request. "
    "Use the recent conversation ONLY when the request depends on or refers to a previous topic "
    "(such as 'what is it about', 'the bosses', 'who made it', 'how many seasons'). "
    "If the request clearly introduces a new unrelated topic, ignore the previous conversation. "
    "Output ONLY the final search query on one line. No quotes, no explanations, no URLs."
)


def _strip_wrapper_and_images(text: str) -> str:
    cleaned = text
    try:
        from opencohost.core.memory.memoria_store import strip_history_wrapper

        cleaned = strip_history_wrapper(cleaned) or cleaned
    except Exception:
        pass
    try:
        from opencohost.core.engine.llm_engine_memorias import strip_pasted_image_block

        cleaned = strip_pasted_image_block(cleaned)
    except Exception:
        pass
    cleaned = _MENTION.sub("", cleaned)
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned


def recent_host_context(
    history: Sequence[dict[str, Any]],
    *,
    max_messages: int = REWRITE_MAX_MESSAGES,
    max_chars: int = REWRITE_MAX_CHARS_PER_MSG,
) -> list[tuple[str, str]]:
    """Extract up to *max_messages* recent owner-thread turns (never viewer chat)."""
    if not history or max_messages <= 0:
        return []
    host_only: list[tuple[str, str]] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        source = str(msg.get("source") or "").strip().lower()
        if source not in HOST_SOURCES:
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        raw = msg.get("content")
        if not isinstance(raw, str) or not raw.strip():
            continue
        cleaned = _strip_wrapper_and_images(raw)
        if not cleaned:
            continue
        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars].rstrip() + "..."
        speaker = "User" if role == "user" else "Assistant"
        host_only.append((speaker, cleaned))
    return host_only[-max_messages:]


def build_rewrite_messages(
    request_text: str,
    recent_lines: Sequence[tuple[str, str]],
) -> list[dict[str, str]]:
    """Build a minimal ~150-token chat payload for contextual query rewriting."""
    convo = "\n".join(f"{speaker}: {text}" for speaker, text in recent_lines)
    clean_req = " ".join(str(request_text or "").split()).strip()[:QUERY_MAX_CHARS]
    user_block = (
        f"Recent conversation:\n{convo}\n\n"
        f"Search request: {clean_req}\n"
        f"Standalone search query:"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_block},
    ]


def parse_rewritten_query(raw: str, *, fallback: str) -> str:
    """Validate and clean the LLM's single-line query output, falling back safely."""
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    text = _THINK_BLOCK.sub("", raw)
    text = _UNCLOSED_THINK.sub("", text).strip()
    if not text:
        return fallback
    line = ""
    for candidate in text.splitlines():
        candidate = candidate.strip()
        if candidate:
            line = candidate
            break
    if not line:
        return fallback
    line = _LEADING_BULLET.sub("", line).strip()
    line = _LEADING_LABEL.sub("", line).strip()
    line = line.strip(" \t\"'`«»“”")
    line = line.rstrip(".!?¿¡:;").strip()
    line = " ".join(line.split())
    if not line:
        return fallback
    if len(line) > min(REWRITE_MAX_OUTPUT_CHARS, QUERY_MAX_CHARS):
        return fallback
    if len(line.split()) > REWRITE_MAX_WORDS:
        return fallback
    if _URL_RE.search(line) or _REFUSAL_RE.search(line):
        return fallback
    return line
