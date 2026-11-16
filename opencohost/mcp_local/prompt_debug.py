"""Debug prompt visibility (mcp-local-20261015 T4).

Classification of prompt sections for the opt-in debug tools. STATIC sections
are text built from code, locale manifests or the profile persona and carry no
user, viewer or memory data: they may be returned in full. Everything else is
DYNAMIC and is returned as size + hash only. A name that is in neither set is
treated as dynamic (fail closed). A test pins this list against the sections
``prompt_assembler`` can emit.

Prompt layout (``prompt_assembler``): the system message is the system parts
joined with a blank line; the context parts (episodic_memory, memory_recall,
session_topics, digest, web, screen) are appended to the last user message. Under layout v2
(ADR-064, ``record["layout"] == "v2"``) every per-turn section is in that
message, inside the ``<turn_context>`` preamble. The trace record
lists both kinds in ``sections``; the system ones are located by walking the
system message with each section's length.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Optional

ENV_FLAG = "OPENCOHOST_MCP_DEBUG_PROMPT"

STATIC_SECTIONS = frozenset(
    {
        "persona", "grounding", "core_rules", "compact_policy", "owner_policy", "reply_length",
        "warmth", "owner_gender", "model_label", "screen_instruction", "vision_speech",
        "vision_rules", "no_look", "vision_off", "vision_capability", "web_instruction",
        "screen_rule", "web_rule", "turn_context_rule",
        "web_note",
        # owner_nickname: one stable system line per profile (v2 owner turns).
        # web_no_source: fixed locale text on turns that carry web or link context.
        "owner_nickname", "web_no_source",
    }
)
DYNAMIC_SECTIONS = frozenset(
    {
        "facts_relevance", "personalization", "already_said", "recency_line", "active_cards",
        "vision_note", "vision_read_note",
        "episodic_memory", "memory_recall", "session_topics", "digest", "web", "screen",
        # ADR-064: per-turn values. ``self_knowledge`` and ``clock`` (legacy
        # layout, system message) carry the time and gated facts, so they are
        # dynamic; ``knowledge``, ``now`` and ``turn_directive`` are their v2
        # preamble counterparts.
        "self_knowledge", "clock", "knowledge", "now", "turn_directive",
        # runtime-fixes-20261110 T2: per-turn web blocks (the typed link note and
        # today's date change from turn to turn).
        "link_note", "web_date",
    }
)
# Sections appended to the last user message, not to the system message.
CONTEXT_SECTIONS = frozenset(
    {"episodic_memory", "memory_recall", "session_topics", "digest", "web", "screen",
     "knowledge", "now", "turn_directive"}
)
# Layout v2 (ADR-064) also moves these out of the system message. They keep
# their names, so they count as context sections only for a v2 record.
V2_CONTEXT_EXTRA = frozenset(
    {"active_cards", "web_instruction", "web_note", "screen_instruction",
     "vision_read_note", "vision_note", "no_look", "vision_capability", "recency_line",
     "link_note", "web_date", "web_no_source"}
)

SEP = "\n\n"
DISABLED = {
    "error": "debug_prompt_disabled",
    "how_to_enable": (
        f"Set {ENV_FLAG}=1 in the environment of the MCP server process "
        '(for example under "env" of the opencohost-diagnostics entry in your local .mcp.json) '
        "and restart the server."
    ),
}


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip() == "1"


def kind(name: str) -> str:
    return "static" if name in STATIC_SECTIONS else "dynamic"


def short_hash(text: str) -> str:
    # Same function as prompt_trace.short_hash so hashes line up with the trace.
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:6]


def system_message(record: dict) -> Optional[str]:
    msgs = record.get("messages") or []
    if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
        c = msgs[0].get("content")
        return c if isinstance(c, str) else None
    return None


def split_sections(record: dict) -> "tuple[list[dict], list[dict]]":
    """Return ``(system_sections, context_sections)``.

    System sections carry name, kind, chars, hash, start/end offsets into the
    system message, ``verified`` (slice hash equals the traced hash) and
    ``text`` only when the section is static AND verified, so a misaligned
    slice is never trusted. Callers strip the internal keys before returning.
    """
    system = system_message(record)
    system_secs: list[dict] = []
    context_secs: list[dict] = []
    context_names = CONTEXT_SECTIONS | V2_CONTEXT_EXTRA if record.get("layout") == "v2" else CONTEXT_SECTIONS
    offset = 0
    for s in record.get("sections") or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name"))
        chars = s.get("chars") if isinstance(s.get("chars"), int) else 0
        entry: dict[str, Any] = {"name": name, "kind": kind(name), "chars": chars, "hash": s.get("hash")}
        if name in context_names:
            context_secs.append(entry)
            continue
        entry["start"], entry["end"] = offset, offset + chars
        entry["verified"] = False
        if system is not None:
            piece = system[offset:offset + chars]
            if short_hash(piece) == s.get("hash"):
                entry["verified"] = True
                if entry["kind"] == "static":
                    entry["text"] = piece
        offset += chars + len(SEP)
        system_secs.append(entry)
    return system_secs, context_secs


def message_meta(record: dict) -> list[dict]:
    out = []
    for m in record.get("messages") or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content", "")
        c = c if isinstance(c, str) else str(c)
        out.append({"role": str(m.get("role", "?"))[:16], "chars": len(c), "hash": short_hash(c)})
    return out


def common_prefix_len(a: str, b: str) -> int:
    return len(os.path.commonprefix([a, b]))
