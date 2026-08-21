"""MemoriaStore — SQLite-backed store for Kira's auto-captured + curated memorias.

Owner-approved design (engram sdd/kira-memory-persistence-20260701/design v2.1):
  - Own unshared SQLite file (memorias.db), PRAGMA user_version=1.
  - Single guarded upsert: INSERT .. ON CONFLICT(profile_id, stable_key) DO
    UPDATE .. WHERE status='draft' — curated rows are immune by construction
    (the conflict resolves to a no-op when the WHERE clause is false, so no
    write happens and no exception is raised). The uniqueness is a composite
    UNIQUE(profile_id, stable_key), not a global UNIQUE(stable_key) — this is
    defense-in-depth for R7 (cross-profile isolation): even a future
    stable_key derivation bug that emits a colliding key across two
    profiles can never make one profile's write silently overwrite another
    profile's row content.
  - Unified curation rule (F5): editing, pinning, OR marking private all
    promote status to 'curated' in the SAME statement as the action — the
    engine never rewrites an operator-touched row. `inactive` is the ONLY
    pure visibility flag (soft mute, not a content judgment) and never
    changes status.
  - Provenance tiers (memoria_recall_20260718 + memoria_import_20260718 +
    memory_promotion_20260725): rows carry FIVE statuses —
      'draft'    — auto-captured, expendable (the prunable pool);
      'curated'  — operator-touched (F5 above), durable;
      'summary'  — machine-authored session summaries (insert_summary), durable;
      'imported' — machine-parsed external-AI export rows (insert_imported),
                   durable;
      'promoted' — machine-JUDGED drafts an LLM archivist rewrote and kept
                   (llm_engine.promote_pending_drafts), durable.
    'summary', 'imported' and 'promoted' are deliberately DISTINCT from
    'curated' so a machine-authored/parsed/judged row is never misreported as
    operator intent on any surface; all three inherit curated's prune- and
    upsert-immunity for free via the same status!='draft' guards
    (_prune_profile / upsert_draft). 'summary' is capped separately by
    _prune_summaries; 'imported' has NO pruner (its cap is enforced at the route
    via count_imported — silent deletion of deliberate imports is dishonest);
    'promoted' has no pruner either (a judged keeper is the durable output the
    whole sweep exists to produce).
  - stable_key namespace (CANONICAL — the marker constants below point back
    here): a 'draft'/'curated' row has a PLAIN key
    ("{profile_id}|{sorted-tokens}", no ':' after the '|'); a 'summary' row
    embeds _SESSION_SUMMARY_MARKER ("{profile_id}|session_summary:{utc-ts}");
    an 'imported' row embeds _IMPORT_MARKER ("{profile_id}|import:{sorted-
    tokens}"). The three namespaces never collide, so a plain
    `MARKER in stable_key` substring test classifies any row unambiguously
    (build_recency_lines and the UI rely on exactly this).
  - stable_key / title derivation applies a small domain-stopword list on
    top of opencohost.core.editorial.editorial_matching.normalize_tokens (which stays
    untouched) — see _MEMORIA_DOMAIN_STOPWORDS. Capture requires >=3
    significant tokens (RC-1); fewer -> derive_stable_key returns None and
    is_capturable() returns False.
  - Bounded timeouts (agenda_persistence.py precedent): READ 0.5s / WRITE
    1.0s, fail-open with a one-time warning per failure episode.
  - Per-profile growth cap: prune oldest unpinned drafts beyond
    MEMORIAS_PROFILE_CAP after each successful INSERT (not UPDATE).
    Curated/pinned rows are never pruned.
  - Log hygiene (RC-8, OBLIGATORY): failure logs and exception messages
    carry only ids/metadata/exception type, NEVER row title or content.

MEMORIAS_ENABLED is True as of slice 8 (flip+disclosure) — the
capture/retrieval/UI slices are wired in; see opencohost/core/llm_engine.py
and opencohost/ui/inspector_memory.py.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from opencohost.config.settings import (
    MEMORIAS_MAX_INJECT_CHARS,
    MEMORIAS_MAX_PINNED_INJECT,
    MEMORIAS_META_RECALL_K,
    MEMORIAS_PINNED_CLIP_CHARS,
    MEMORIAS_PROFILE_CAP,
    MEMORIAS_SUMMARY_CAP,
)
# Same accent folding as the editorial matcher — the meta-recall detector must
# normalize exactly like the rest of the pipeline that feeds it. Single source
# of truth on purpose (editorial_matching owns the normalizer; do not add a
# second copy here).
from opencohost.core.editorial.editorial_matching import _strip_accents, normalize_tokens
# History-wrapper templates (see strip_history_wrapper below). Import-safe:
# i18n.active resolves its bundle lazily on first accessor call, never at import.
from opencohost.i18n import active as i18n_active

logger = logging.getLogger(__name__)

# Saves/reads can fire from multiple threads; never wait longer than these
# (sqlite default is 5s — a visible freeze). Mirrors agenda_persistence.py.
READ_TIMEOUT_SECONDS: float = 0.5
WRITE_TIMEOUT_SECONDS: float = 1.0

_MIN_SIGNIFICANT_TOKENS = 3
_STABLE_KEY_TOKEN_COUNT = 6
_TITLE_TOKEN_COUNT = 3
_SIGNATURE_TOKEN_COUNT = 12  # within the design's 8-16 window
_MIN_SHARED_TOKENS = 2

# Closed list (design v2.1 residual risk #3): near-ubiquitous tokens in
# captured co-host turns — Kira's own name, streaming tooling, and
# meta-conversation words about profiles/topics/remembering/chat — that
# survive normalize_tokens's generic stopwords but never discriminate
# between memories. Calibrated against the RC-1/RC-7 test fixtures below.
# Do not expand/shrink without updating those tests in lockstep.
_MEMORIA_DOMAIN_STOPWORDS: frozenset[str] = frozenset({
    "kira", "obs", "tema", "perfil", "usuario", "recuerda", "dice", "streamer", "chat",
    # A2 (memoria_quality_20260717): PTT history-wrapper + legacy ledger-label
    # boilerplate that leaks into derivation ("El streamer dijo (PTT): ...",
    # "El streamer acaba de decir (PTT): ...", "contexto: ..."). LOAD-BEARING,
    # not belt-and-braces: the honest ptt_history_wrapper still carries
    # dijo/ptt. Also retroactively neutralizes the stored "contexto"-polluted
    # signatures at scoring time, since select_top_k stopword-filters the topic
    # side. Do not expand/shrink without updating the RC-1/RC-7 tests in lockstep.
    "acaba", "decir", "dijo", "ptt", "contexto",
    # NOTE (F2): the typed-turn wrapper's verbs ("escribio"/"typed") were briefly
    # added here and then removed — blacklisting content words to cancel template
    # boilerplate is the wrong layer. It shifted stable_key for turns using those
    # words as real content (dupe rows), shaved the >=3 capture minimum, and
    # still missed "said" under the `en` bundle. strip_history_wrapper below
    # removes the frame STRUCTURALLY instead. Do not re-add wrapper words here;
    # if a new wrapper appears, give it an i18n slot with a "{text}" placeholder
    # and it is covered for free, in every locale.
})


# ---------------------------------------------------------------------------
# History-wrapper strip (F2) — the derivation boundary's only frame remover
# ---------------------------------------------------------------------------

# Every history wrapper is an i18n template that frames an operator turn with
# its provenance, with the turn itself in a "{text}" placeholder:
#   "El streamer dijo (PTT): {text}" / "The streamer typed: {text}" / ...
# So the prefix is derivable from the template at runtime — no per-locale,
# per-wrapper word list. accumulation_ptt() is deliberately absent: it uses a
# "{messages}" placeholder and its "accumulated" source is not in the engine's
# capture allowlist, so it never reaches derivation.
_HISTORY_WRAPPER_ACCESSORS = (
    i18n_active.ptt_wrapper,
    i18n_active.ptt_history_wrapper,
    i18n_active.typed_history_wrapper,
    i18n_active.live_voice_wrapper,
)
_WRAPPER_TEXT_PLACEHOLDER = "{text}"


def strip_history_wrapper(text: str) -> str:
    """Remove a leading history-wrapper frame from *text*, if it carries one.

    The frame is provenance METADATA, not content: leaving it in shifts the
    tokens stable_key is built from (so the upsert stops recognising the same
    exchange and inserts a competing row), consumes one of the >=3 significant
    tokens a capture needs, and keeps the filler gate's remainder non-empty so a
    wrapped pure greeting survives as a memoria.

    Resolves the prefixes from the ACTIVE locale's templates — the same
    accessors that produced the text — and strips the longest match. Text with
    no wrapper (chat, agenda, a bare typed turn) is returned unchanged.
    """
    raw = text or ""
    longest = ""
    for accessor in _HISTORY_WRAPPER_ACCESSORS:
        # _slot-backed accessors swallow every failure and fall back to their
        # legacy es literal, so this cannot raise.
        prefix = accessor().split(_WRAPPER_TEXT_PLACEHOLDER)[0]
        if prefix and len(prefix) > len(longest) and raw.startswith(prefix):
            longest = prefix
    return raw[len(longest):].lstrip() if longest else raw


class MemoriaValidationError(ValueError):
    """Raised when a memoria write is missing required content.

    The message is a static, content-free string by design (RC-8): it must
    never interpolate row title/content, since callers may log it.
    """


# ---------------------------------------------------------------------------
# Pure helpers — stable_key / title derivation (RC-1, RC-7)
# ---------------------------------------------------------------------------

# memoria_quality gate fix (2026-07): tracks.md's design intent for C1 states
# the "user-side >=2-token capture gate kills greetings" -- it did not,
# because significant_token_count was a bare COUNT, not a salience judgement.
# "Buenas, como vamos el dia de hoy" reduces to 5 distinct tokens (buenas,
# como, vamos, dia, hoy), none of which are in _MEMORIA_DOMAIN_STOPWORDS or
# editorial_matching's generic stopwords, so it cleared both the >=2 gate
# (llm_engine.py) and the >=3 is_capturable/derive_stable_key gate here.
#
# Widening the stopword lists was rejected: "como", "dia", "hoy" etc. are too
# generic to blacklist token-by-token (they appear in real content too --
# "que dia juega el equipo?"). Instead this is a closed set of
# greeting/farewell/acknowledgement PHRASE SHAPES (Spanish + English, the
# project is bilingual). A turn is filler-only when substituting every shape
# out of it leaves nothing significant behind -- an ALL-OR-NOTHING turn
# classification, not a per-token stopword: a turn that opens with a
# greeting but goes on to say something real is untouched (RC-1 "greeting +
# content" case), because the leftover real tokens keep the check honest.
_FILLER_PHRASE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in (
        # Spanish greetings / "how's it going" openers
        r"\bbuen(?:as|os|a)(?:\s+(?:dias|tardes|noches))?\b",
        r"\bholas?\b",
        r"\bque\s+tal\b",
        r"\bque\s+onda\b",
        # "vas" escaped the original alternation within a day of it shipping
        # ("como vas?" still persisted as a memoria). BAND-AID: a closed filler
        # list does not scale — the real fix is LLM-judged promotion at session
        # close, tracked separately.
        r"\bcomo\s+(?:vamos|vas|estas|estan|va|andas)\b",
        r"\bel\s+dia\s+de\s+hoy\b",
        r"\bhoy\b",
        # Spanish farewells / acknowledgements
        r"\bnos\s+vemos\b",
        r"\bhasta\s+(?:luego|pronto|manana)\b",
        r"\badios\b",
        r"\bchau\b",
        r"\bgracias\b",
        r"\bvale\b",
        r"\bdale\b",
        r"\bperfecto\b",
        # English greetings
        r"\bhi\b",
        r"\bhello\b",
        r"\bhey\b",
        r"\bgood\s+(?:morning|afternoon|evening)\b",
        r"\bhow(?:'s|\s+is)\s+it\s+going\b",
        r"\bhow\s+are\s+you\b",
        r"\bwhat'?s\s+up\b",
        r"\btoday\b",
        # English farewells / acknowledgements
        r"\bbye\b",
        r"\bgoodbye\b",
        r"\bsee\s+you\b",
        # "thank you" must be tried before the bare "thanks?" below, or the
        # bare pattern eats "thank" first and strands a lone "you".
        r"\bthank\s+you\b",
        r"\bthanks?\b",
        r"\bok(?:ay)?\b",
        r"\balright\b",
        r"\bsounds?\s+good\b",
    )
)


def _raw_significant_tokens(text: str) -> list[str]:
    """normalize_tokens(text) minus domain stopwords, deduped, first-occurrence order.

    The pre-filler-gate helper: no greeting/farewell shape check. Kept
    separate so the filler check below can test its OWN leftover text
    without recursing into itself.
    """
    seen: set[str] = set()
    result: list[str] = []
    for token in normalize_tokens(text or ""):
        if token in _MEMORIA_DOMAIN_STOPWORDS or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _is_pure_filler(text: str) -> bool:
    """True when *text*, with every greeting/farewell/ack phrase shape
    substituted out, has no significant tokens left over."""
    remainder = _strip_accents((text or "").lower())
    for pattern in _FILLER_PHRASE_PATTERNS:
        remainder = pattern.sub(" ", remainder)
    return not _raw_significant_tokens(remainder)


def _significant_tokens(text: str) -> list[str]:
    """normalize_tokens(text) minus domain stopwords, deduped, first-occurrence
    order -- then, IF there are 2+ tokens AND the whole text is nothing but
    greeting/farewell/ack phrase shapes, reports none. A single bare token
    (e.g. "hola" alone) is left alone: the existing >=2/>=3 count gates
    already reject it on their own, and zeroing single tokens here would
    conflate this shape-check with a second stopword list.

    F2: the history-wrapper frame comes off FIRST, once, so both the token list
    and the filler check below see the operator's actual words. This is the
    single derivation boundary — is_capturable, derive_stable_key,
    derive_import_key, build_title, build_signature and significant_token_count
    all route through here, so the strip cannot be bypassed by a caller.
    """
    text = strip_history_wrapper(text)
    tokens = _raw_significant_tokens(text)
    if len(tokens) >= 2 and _is_pure_filler(text):
        return []
    return tokens


def significant_token_count(text: str) -> int:
    """Public count of DISTINCT significant tokens in *text* (domain + generic
    stopwords filtered).

    C1 (memoria_quality_20260717): the engine's content-shaping gates reuse
    this without reaching into the private _significant_tokens — the user-side
    >=2-token capture gate and the Kira-side "first sentence with >=3
    significant tokens" pick both count the same way capture already does.
    """
    return len(_significant_tokens(text))


def is_capturable(text: str) -> bool:
    """True when *text* has enough significant tokens to be worth capturing.

    RC-1 capture minimum: fewer than 3 significant tokens (post domain-
    stopword filtering) means the pair is low-signal / collision-prone and
    must not be captured. Consumed by the slice-3 capture gate.
    """
    return len(_significant_tokens(text)) >= _MIN_SIGNIFICANT_TOKENS


def derive_stable_key(profile_id: str, text: str) -> str | None:
    """Deterministic dedup key for *text* within *profile_id*.

    Returns None when *text* has fewer than 3 significant tokens (RC-1) —
    callers MUST treat None as "skip, do not capture". Key = first 6
    distinct significant tokens, sorted (order-independent), prefixed with
    `{profile_id}|`.
    """
    tokens = _significant_tokens(text)
    if len(tokens) < _MIN_SIGNIFICANT_TOKENS:
        return None
    key_tokens = sorted(tokens[:_STABLE_KEY_TOKEN_COUNT])
    return f"{profile_id}|{'-'.join(key_tokens)}"


def build_title(text: str) -> str:
    """First 3 distinct significant tokens, in appearance order (RC-7)."""
    return " ".join(_significant_tokens(text)[:_TITLE_TOKEN_COUNT])


def build_signature(text: str) -> str:
    """First 12 distinct significant tokens of *text*, space-joined.

    Candidate 2 (memoria_rag_followups_20260716): the retrieval signature is
    derived from the FULL user+assistant pair at capture time (vs the 3-token
    title), giving select_top_k a wider, still-distinctive token set to score
    against. Reuses _significant_tokens — no new normalizer.
    """
    return " ".join(_significant_tokens(text)[:_SIGNATURE_TOKEN_COUNT])


# memoria_import_20260718 (D3/D6) — the stable_key infix that marks an imported
# row (status='imported'). See the CANONICAL stable_key namespace note in the
# module docstring for how the three namespaces stay collision-free. Embedded as
# f"{profile_id}|import:{sorted-6-token-key}" so build_recency_lines and the UI
# can detect imports with a plain `_IMPORT_MARKER in stable_key` substring test,
# and so INSERT ON CONFLICT dedups a re-imported identical claim.
_IMPORT_MARKER = "|import:"


def derive_import_key(profile_id: str, claim: str) -> str | None:
    """Deterministic dedup key for an imported *claim* within *profile_id* (D6).

    Same 6-distinct-significant-token, order-independent shape as
    derive_stable_key, but with the _IMPORT_MARKER infix so imports occupy their
    own key namespace: a re-import of the identical claim collides (skipped, zero
    dupes), while an operator's later curated edit keeps its own draft-derived
    key and is never overwritten. Returns None when *claim* has fewer than 3
    significant tokens — the route treats None as skipped_too_short (same bucket
    as is_capturable), never as a captured row.
    """
    tokens = _significant_tokens(claim)
    if len(tokens) < _MIN_SIGNIFICANT_TOKENS:
        return None
    key_tokens = sorted(tokens[:_STABLE_KEY_TOKEN_COUNT])
    return f"{profile_id}{_IMPORT_MARKER}{'-'.join(key_tokens)}"


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Retrieval + injection (slice 5, design v2.1 §7, F6) — pure, no I/O
# ---------------------------------------------------------------------------

# Minimum remaining injection budget worth clipping a non-pinned row into —
# a fragment shorter than this is noise, not context (candidate 2).
_MIN_CLIP_REMAINDER_CHARS = 40

# W3 (memoria_recall_20260718): high-frequency Spanish function words that
# survive normalize_tokens AND _MEMORIA_DOMAIN_STOPWORDS but never discriminate
# between memories — they only inflate the shared-token score in select_top_k,
# letting an incidental function-word overlap cross the >=2-shared-token gate.
# SCORING-ONLY: subtracted from both the topic and signature sets inside
# select_top_k. Deliberately NOT added to _MEMORIA_DOMAIN_STOPWORDS — that would
# shift derive_stable_key / is_capturable (design rejected: re-capture dupes,
# shaves user-side capture gates). Also retroactively neutralizes already-stored
# polluted signatures at scoring time.
_SCORING_STOPWORDS: frozenset[str] = frozenset({
    "que", "mi", "se", "esa", "este", "como", "para",
})


def _compute_candidate_idf(rows) -> dict[str, float]:
    """Compute smoothed inverse document frequency in RAM across candidate rows.

    df(t) is the count of candidate documents whose filtered signature
    (title fallback) contains token t. Each document contributes at most 1
    to df(t). Smooth formula: log((N + 1) / (df + 1)) + 1.0.
    """
    n_docs = len(rows)
    if n_docs == 0:
        return {}
    df: dict[str, int] = {}
    for row in rows:
        sig = set((row["signature"] or row["title"]).split()) - _SCORING_STOPWORDS
        for token in sig:
            df[token] = df.get(token, 0) + 1
    return {
        token: math.log((n_docs + 1.0) / (count + 1.0)) + 1.0
        for token, count in df.items()
    }


def select_top_k(topic_text: str, rows, k: int = 3) -> list:
    """Lexical top-k of *rows* against *topic_text* by Raw IDF Sum.

    Candidate A (2026-08-21): each row is scored by the sum of inverse document
    frequency (IDF) weights of the shared significant tokens between topic and
    the candidate's signature (title fallback). Requires >= _MIN_SHARED_TOKENS
    shared tokens. Preserves input order on score ties (Timsort stability).
    Returns up to *k* rows.
    """
    topic = set(_significant_tokens(topic_text)) - _SCORING_STOPWORDS
    if not topic or not rows:
        return []
    idf_map = _compute_candidate_idf(rows)
    scored = []
    for row in rows:
        sig = set((row["signature"] or row["title"]).split()) - _SCORING_STOPWORDS
        shared = topic & sig
        if len(shared) >= _MIN_SHARED_TOKENS:
            score = sum(idf_map[token] for token in shared)
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored[:k]]


def _clip_for_injection(text: str, limit: int) -> str:
    """Hard-cut *text* to at most *limit* chars total, ellipsis included.

    Never mutates the caller's row — operates on a plain string copy.
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _append_within_budget(
    lines: list[str], used: int, text: str, max_chars: int, *,
    clip_to_remaining: bool = False,
) -> int:
    """Shared budget bookkeeping for injection-line assembly (4R WU4, D-hygiene).

    Append *text* to *lines* when it fits the remaining *max_chars* budget
    (accounting for the 1-char "\\n" join separator) and return the new *used*
    count. An over-budget text is rejected, or — with *clip_to_remaining* —
    clipped into the remainder unless that remainder is below
    _MIN_CLIP_REMAINDER_CHARS (a fragment that small is noise, not context).
    Single source of truth for build_injection_lines AND build_recency_lines.
    """
    sep = 1 if lines else 0
    remaining = max_chars - used - sep
    if len(text) > remaining:
        if not clip_to_remaining or remaining < _MIN_CLIP_REMAINDER_CHARS:
            return used
        text = _clip_for_injection(text, remaining)
    lines.append(text)
    return used + sep + len(text)


def pinned_injection_counter(rows, max_pinned: int = MEMORIAS_MAX_PINNED_INJECT) -> tuple[int, int]:
    """Honest pin/injection counter value (F6) — (total_pinned, injected).

    Unconditional: the pinned clip bounds pinned rows to at most
    max_pinned * MEMORIAS_PINNED_CLIP_CHARS chars, so the policy count never
    depends on whether the char budget happens to fit. Value only — rendered
    by the slice-7 management UI.
    """
    total_pinned = sum(1 for row in rows if row["pinned"])
    return total_pinned, min(total_pinned, max_pinned)


def build_injection_lines(
    rows,
    topic_text: str,
    *,
    max_chars: int = MEMORIAS_MAX_INJECT_CHARS,
    max_pinned: int = MEMORIAS_MAX_PINNED_INJECT,
    pinned_clip_chars: int = MEMORIAS_PINNED_CLIP_CHARS,
    top_k: int = 3,
) -> list[str]:
    """Assemble the injected memorias lines under pinned policy A (F6).

    *rows* MUST already be the eligible candidate set (private=0,
    inactive=0, active profile, capped — see
    MemoriaStore.list_injection_candidates). Pure, no I/O, never mutates a
    row's stored content/title.

    Order: up to *max_pinned* OLDEST-pinned rows first (each clipped to
    ~*pinned_clip_chars*), then automatic top-k matches from the remaining
    rows fill the rest of the budget. Because pinned inclusion is capped at
    max_pinned * pinned_clip_chars, automatic top-k always keeps a floor of
    at least (max_chars - max_pinned * pinned_clip_chars) chars, regardless
    of how many rows are pinned.
    """
    pinned_rows = sorted(
        (row for row in rows if row["pinned"]),
        key=lambda row: (row["created_at"], row["id"]),
    )[:max_pinned]
    non_pinned_rows = [row for row in rows if not row["pinned"]]

    lines: list[str] = []
    used = 0

    # Candidate 2: selected non-pinned rows are clipped into whatever budget
    # remains instead of being silently dropped (clip_to_remaining). Pinned
    # rows keep the original reject-when-over-budget behavior (already
    # pre-clipped to pinned_clip_chars).
    for row in pinned_rows:
        used = _append_within_budget(
            lines, used, _clip_for_injection(row["content"], pinned_clip_chars), max_chars,
        )

    for row in select_top_k(topic_text, non_pinned_rows, k=top_k):
        used = _append_within_budget(lines, used, row["content"], max_chars, clip_to_remaining=True)

    return lines


# W2a (memoria_recall_20260718): the stable_key infix that marks a
# machine-authored session-summary row (status='summary'). See the CANONICAL
# stable_key namespace note in the module docstring for the collision-free
# namespace layout. Embedded between the profile_id and a UTC timestamp
# (f"{profile_id}{_SESSION_SUMMARY_MARKER}{ts}") so a fresh timestamp is
# upsert-immune by construction. The W2b meta-router
# discovers summaries with a plain `_SESSION_SUMMARY_MARKER in stable_key` test;
# _prune_summaries caps the tier by the status='summary' filter (not the marker),
# so a summary an operator later edits/pins (promoted to 'curated') drops out of
# the cap and becomes permanent.
_SESSION_SUMMARY_MARKER = "|session_summary:"


# ---------------------------------------------------------------------------
# W2b/W4 (memoria_recall_20260718) — meta/temporal recall router
# ---------------------------------------------------------------------------

# Closed, accent-tolerant regex list that marks a META/temporal RECALL query —
# one asking Kira what she remembers or what happened in a PAST session, as
# opposed to a topical question. On a hit _build_memorias_injection_block
# retrieves by RECENCY (build_recency_lines) instead of lexical select_top_k,
# which structurally CANNOT answer a temporal query (no recency term, no
# "session" concept in the schema; the two production-failing owner queries —
# "¿qué recordás de mí?" / "de qué se habló la sesión pasada" — are exactly
# this class). Matched with re.search against ACCENT-STRIPPED, lowercased text,
# so a PTT-wrapped query ("El streamer acaba de decir (PTT): ¿qué recordás?")
# still matches through the wrapper prefix (substring search).
#
# 4R correction WU4 (finding A): ANCHORED PHRASES, not bare stems. A bare stem
# misroutes topical turns to the recency dump, bypassing the W4 owner-gated
# fallback ("récord" the gaming noun, "acorde"/"acordeón", indicative
# "hablamos de X", resemblance "me recordás al profe"). Every pattern anchors
# on an interrogative/recall bigram that a topical turn does not carry. All
# patterns are linear-time (plain alternation + \w*, no nested quantifiers).
# Closed list: extend only in lockstep with the positive/negative tables in
# tests/test_memoria_recall_routing.py.
_META_RECALL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in (
    # "¿qué recordás/recuerdas (de mí)?", "lo que recordabas" — interrogative
    # recall. The leading "que" excludes the gaming noun "récord" ("hice un
    # nuevo récord") and resemblance uses ("me recordás al profe").
    r"\bque\s+(record|recuerd)\w*",
    # "recordás/recuerdas de mí" without the interrogative — verb-only stems
    # (recorda- / recuerd-+suffix), so the bare noun "récord de ..." can never
    # match.
    r"\b(recorda\w*|recuerd\w+)\s+de\s+mi\b",
    # "¿qué sabés/sabes/sabías de mí?" — knowledge-recall about the speaker.
    r"\bsab(es|e|ias)\s+de\s+mi\b",
    # "de qué hablamos / de qué se habló (la sesión pasada)" — interrogative
    # word order; topical "hablamos de X" lacks the leading "de que".
    r"\bde\s+que\s+(se\s+)?habl\w+",
    # "qué hablamos ayer / la otra vez" — interrogative; "hablamos de Dark
    # Souls" and "hablamos mañana" lack the leading "que".
    r"\bque\s+hablamos\b",
    # "qué se dijo / se habló / se contó / se comentó" — impersonal recall of
    # a past conversation, anchored on the interrogative "que".
    r"\bque\s+se\s+(hablo|dijo|conto|comento)\b",
    # "te acordás (de ...)" — acordarse-as-recall, second person. The "te " +
    # verb-stem anchor never matches "acorde"/"acordeón".
    r"\bte\s+acorda\w*",
    # "¿se acuerdan (de ...)?" / "se acuerda de aquella vez" — acordarse-as-
    # recall, third person (irregular acuerd- stem the v1 list missed).
    r"\bse\s+acuerda\w*",
    # "no me acuerdo(, ¿vos sí?)" — the speaker asking Kira to fill a memory
    # gap (irregular first-person acuerdo, missed by v1's acord- stem).
    r"\bno\s+me\s+acuerdo\b",
    # "sesión/charla/stream/conversación pasada|anterior|previa", both orders.
    r"\b(sesion|charla|stream|conversacion)\w*\s+(pasad|anterior|previ)\w*",
    r"\b(pasad|anterior|previ)\w*\s+(sesion|charla|stream|conversacion)\w*",
    # "en base a mis/tus memorias" — explicit plural reference to the memoria
    # store (D6, model_switch_memory_continuity_20260723).
    r"\b(mis|tus)\s+memorias\b",
    # "en base a mi/tu memoria" (singular) — but ONLY phrase-final (end of
    # string or followed by punctuation), never followed by another noun.
    # jd-fix-agent correction (finding 1): the plain `(mi|tu)\s+memorias?`
    # shape over-triggered on topical hardware mentions ("mi memoria RAM",
    # "tu memoria USB se llenó", "...mi memoria del teléfono"), wrongly
    # routing them to recency. The negative lookahead excludes "memoria"
    # immediately followed by another word, while still matching phrase-final
    # forms like "lo último de tu memoria" / "¿qué hay en tu memoria?".
    r"\b(mi|tu)\s+memoria\b(?!\s+\w)",
))


def is_meta_recall_query(text: str) -> bool:
    """True when *text* is a meta/temporal RECALL query (W2b/W4).

    See _META_RECALL_PATTERNS. Accent-insensitive (reuses the editorial
    normalizer's accent stripper) and wrapper-tolerant (substring re.search),
    so both the direct and PTT-wrapped forms of a query resolve identically.
    Pure, no I/O. Empty/None text is never meta.
    """
    if not text:
        return False
    normalized = _strip_accents(text.lower())
    return any(pattern.search(normalized) for pattern in _META_RECALL_PATTERNS)


# 4R correction WU4 (finding C): summaries' share of the k recency slots.
# MEMORIAS_META_RECALL_K=5 slots under a 700-char budget: 2 summaries answer
# the "la sesión pasada" class (the last session, plus the one before) while
# leaving the majority of the slots — and the residual char budget — to live
# regular rows; without this bound, >=k accumulated summaries starve every
# live-session row out of (summaries + regular)[:k]. Mirrors the
# MEMORIAS_MAX_PINNED_INJECT=2 policy shape.
_META_RECALL_MAX_SUMMARIES = 2

# memoria_import_20260718 (D5): imported rows are a third recency partition with
# EXACTLY one slot — placed AFTER summaries, BEFORE regular. A bulk import has
# the newest updated_at of everything the day it lands; without this bound 50
# fresh imports would drown live session context on a "¿qué sabés de mí?" recall.
# One slot surfaces the freshest import; the owner pins key imports into the
# pinned carve-out for more. Lexical path is unchanged (imports score normally
# via signatures). Mirrors the _META_RECALL_MAX_SUMMARIES policy shape.
_META_RECALL_MAX_IMPORTED = 1


def build_recency_lines(
    rows,
    k: int = MEMORIAS_META_RECALL_K,
    *,
    max_chars: int = MEMORIAS_MAX_INJECT_CHARS,
    max_pinned: int = MEMORIAS_MAX_PINNED_INJECT,
    pinned_clip_chars: int = MEMORIAS_PINNED_CLIP_CHARS,
) -> list[str]:
    """RECENCY-ordered injection lines for a meta/temporal recall query (W2b).

    Unlike select_top_k (lexical), this ignores topic overlap — a meta query has
    no topical anchor to score against. Order (4R WU4, findings B+C): up to
    *max_pinned* OLDEST-pinned rows FIRST, each clipped to ~*pinned_clip_chars*
    (the same F6 carve-out as build_injection_lines — "¿qué recordás de mí?" is
    exactly the class the pinned guarantee exists for), then machine-authored
    session SUMMARIES (found by _SESSION_SUMMARY_MARKER in stable_key — the
    durable tier) newest first but capped at _META_RECALL_MAX_SUMMARIES so they
    never starve live rows, then at most _META_RECALL_MAX_IMPORTED newest
    IMPORTED rows (found by _IMPORT_MARKER in stable_key — memoria_import D5, so
    a fresh bulk import never drowns live session context), then the regular
    rows, VOUCHED ones first (promoted/curated/judged — see
    `_vouched_then_recency`) and newest-first within each group — *k* rows
    total,
    assembled under the SAME *max_chars* budget (_append_within_budget; a
    non-pinned over-budget row is clipped into the remainder, or skipped when
    the remainder is below _MIN_CLIP_REMAINDER_CHARS). Pure, no I/O, never
    mutates a row. The summary marker lives ONLY in stable_key, so
    row["content"] is already clean display text.

    *rows* MUST be the eligible candidate set (private=0, inactive=0, capped —
    MemoriaStore.list_injection_candidates), same contract as
    build_injection_lines.
    """
    pinned_rows = sorted(
        (row for row in rows if row["pinned"]),
        key=lambda row: (row["created_at"], row["id"]),
    )[:max_pinned]
    summaries: list = []
    imported: list = []
    regular: list = []
    for row in rows:
        if row["pinned"]:
            continue  # pinned rows (even a pinned summary/import) ride the carve-out
        stable_key = row["stable_key"] or ""
        if _SESSION_SUMMARY_MARKER in stable_key:
            summaries.append(row)
        elif _IMPORT_MARKER in stable_key:
            imported.append(row)
        else:
            regular.append(row)

    def _recency(row):
        return (row["updated_at"] or "", row["id"] or "")

    def _field(row, name):
        """Tolerant read — sqlite3.Row raises IndexError on a missing column,
        and this function is documented as pure over any row-like mapping."""
        try:
            return row[name]
        except (IndexError, KeyError):
            return None

    def _vouched_then_recency(row):
        """Vouched rows outrank raw drafts; recency still orders within each.

        A raw draft is a row NOTHING has vouched for. A promoted row survived
        the six promotion criteria and was rewritten; a curated one carries an
        explicit operator action; an uncertain keep stays `draft` but has
        `judged_at` set and its text rewritten, so it counts too.

        This exists because the sweep deliberately does NOT bump `updated_at`
        on a keep (pinned by test_confident_keep_leaves_updated_at_untouched:
        bumping it would stamp three-week-old drafts with launch time and make
        "de qué hablamos la sesión pasada" answer with the wrong session). The
        consequence is that a kept memory always LOOKS older than tonight's raw
        captures, so a pure recency sort buried it every single time —
        "¿qué recordás de mí?" recited trivia while the judged memories sat
        below the k-cap. Found by adversarial review 2026-08-14.

        Judge-REJECTED drafts never reach here: they carry inactive=1 and
        `list_injection_candidates` already filters them out.
        """
        status = _field(row, "status") or ""
        judged_at = _field(row, "judged_at") or ""
        vouched = 1 if (status != "draft" or judged_at) else 0
        return (vouched,) + _recency(row)

    summaries.sort(key=_recency, reverse=True)
    imported.sort(key=_recency, reverse=True)
    regular.sort(key=_vouched_then_recency, reverse=True)

    lines: list[str] = []
    used = 0
    for row in pinned_rows:
        used = _append_within_budget(
            lines, used, _clip_for_injection(row["content"], pinned_clip_chars), max_chars,
        )
    ordered = (
        summaries[:_META_RECALL_MAX_SUMMARIES]
        + imported[:_META_RECALL_MAX_IMPORTED]
        + regular
    )[: max(k - len(lines), 0)]
    for row in ordered:
        used = _append_within_budget(lines, used, row["content"], max_chars, clip_to_remaining=True)
    return lines


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class MemoriaStore:
    """SQLite-backed store for Kira memorias (own unshared DB, schema v1)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._warn_lock = threading.Lock()
        self._write_failure_warned = False
        self._init_db()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert_draft(
        self, profile_id: str, stable_key: str, title: str, content: str, *,
        signature: str = "", return_created: bool = False,
    ) -> "str | None | tuple[str | None, bool]":
        """Insert a new draft or refresh an existing draft sharing stable_key.

        Curated rows are upsert-immune (the WHERE clause makes the conflict
        resolve to a no-op — no write, no exception). Returns the row id on
        success, or None when the row was curated-immune or the write
        failed open (locked db). Raises MemoriaValidationError for missing
        required inputs. *signature* (candidate 2) is the optional retrieval
        signature (build_signature of the full pair); "" is valid — scoring
        falls back to the title.

        *return_created* (E1, memoria_quality_20260717) is an OPT-IN widening:
        when True, returns ``(row_id, created)`` where ``created`` is True only
        for a genuine fresh INSERT (revision==1), False for a refresh/no-op.
        Off by default so every caller keeps the bare-id contract.

        It currently has NO production caller. Its one user was the engine's
        per-capture `memoria_captured` notice, which moved to the promotion
        sweep on 2026-08-14 (a capture is an unjudged draft, so announcing it
        was announcing a decision nothing had made yet). Kept because the
        distinction is real and cheap, but do not assume it is exercised.

        D3b (memory_promotion_20260725): the DO UPDATE clause also CLEARS
        ``judged_at`` on a revision bump — a judgment is about a SPECIFIC
        capture, so changed content earns a re-judge at the next sweep. The
        ``inactive`` CASE un-hides ONLY rows the JUDGE hid (``judged_at != ''``);
        a row the OPERATOR muted via set_flags (which never touches status, so
        it still matches this draft-only conflict clause) stays hidden. Both
        expressions read the PRE-update row, so the ``judged_at = ''`` assignment
        above does not affect the CASE.
        """
        profile_id = (profile_id or "").strip()
        stable_key = (stable_key or "").strip()
        title = " ".join((title or "").split())
        content = " ".join((content or "").split())
        signature = " ".join((signature or "").split())
        if not profile_id or not stable_key or not title or not content:
            raise MemoriaValidationError(
                "memoria upsert requires profile_id, stable_key, title, and content"
            )

        now = _now_text()
        row_id = f"mem_{uuid4().hex}"
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                cur = conn.execute(
                    """
                    INSERT INTO memorias (
                        id, profile_id, stable_key, revision, title, content, status,
                        pinned, private, inactive, created_at, updated_at, signature
                    ) VALUES (?, ?, ?, 1, ?, ?, 'draft', 0, 0, 0, ?, ?, ?)
                    ON CONFLICT(profile_id, stable_key) DO UPDATE SET
                        content = excluded.content,
                        title = excluded.title,
                        signature = excluded.signature,
                        revision = memorias.revision + 1,
                        updated_at = excluded.updated_at,
                        judged_at = '',
                        inactive = CASE WHEN memorias.judged_at != ''
                                        THEN 0 ELSE memorias.inactive END
                    WHERE memorias.status = 'draft'
                    RETURNING id, revision
                    """,
                    (row_id, profile_id, stable_key, title, content, now, now, signature),
                )
                result = cur.fetchone()
        except sqlite3.Error as exc:
            self._warn_once(f"memoria store write failed (fail-open): {type(exc).__name__}")
            return (None, False) if return_created else None

        self._clear_warn()
        if result is None:
            # curated row: upsert-immune, no write happened
            return (None, False) if return_created else None

        written_id, revision = result["id"], result["revision"]
        if revision == 1:
            self._prune_profile(profile_id)
        else:
            # RC-8: the row id, never the stable_key. `derive_stable_key` builds
            # the key from the exchange's own significant tokens, so logging it
            # puts memory CONTENT in a log line — the one thing the store's
            # logging contract forbids. The id identifies the row just as well.
            logger.debug("memoria upsert conflict id=%s revision=%s", written_id, revision)
        return (written_id, revision == 1) if return_created else written_id

    def insert_summary(
        self, profile_id: str, title: str, content: str, *, signature: str = "",
    ) -> str | None:
        """W2a (memoria_recall_20260718): insert ONE machine-authored
        session-summary row (status='summary').

        Unlike upsert_draft this is a plain INSERT of a status='summary' row
        whose stable_key embeds a UTC timestamp
        (f"{profile_id}{_SESSION_SUMMARY_MARKER}{ts}"). The 'summary' status is
        deliberately DISTINCT from 'curated' (F5 = operator-touched): these rows
        are machine-authored, so labelling them curated would misreport
        provenance on every surface. They still inherit curated's durability for
        free — prune-immune (status!='draft' rows are excluded from
        _prune_profile's draft-only WHERE clause) AND upsert-immune (upsert_draft
        only overwrites status='draft'; a fresh timestamp also never collides).
        The summary is the DURABLE half of the 200-row tiering: per-turn drafts
        are the expendable pool, these summaries the durable tier the W2b
        meta-router surfaces on recall queries (found by _SESSION_SUMMARY_MARKER
        in stable_key), capped independently at MEMORIAS_SUMMARY_CAP by
        _prune_summaries after each insert.

        Returns the new row id, or None on the fail-open path (locked db, or a
        same-microsecond stable_key collision resolving to an IntegrityError).
        Never raises for a store error; raises MemoriaValidationError only for
        missing required inputs (RC-8: message is content-free).
        """
        profile_id = (profile_id or "").strip()
        title = " ".join((title or "").split())
        content = " ".join((content or "").split())
        signature = " ".join((signature or "").split())
        if not profile_id or not title or not content:
            raise MemoriaValidationError(
                "memoria summary requires profile_id, title, and content"
            )

        now = _now_text()
        stable_key = f"{profile_id}{_SESSION_SUMMARY_MARKER}{now}"
        row_id = f"mem_{uuid4().hex}"
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO memorias (
                        id, profile_id, stable_key, revision, title, content, status,
                        pinned, private, inactive, created_at, updated_at, signature
                    ) VALUES (?, ?, ?, 1, ?, ?, 'summary', 0, 0, 0, ?, ?, ?)
                    """,
                    (row_id, profile_id, stable_key, title, content, now, now, signature),
                )
        except sqlite3.Error as exc:
            # R4: ALWAYS warn (bypass the shared warn-once flag) — losing a
            # durable summary is the high-value signal, never a demoted repeat.
            logger.warning(f"memoria summary write failed (fail-open): {type(exc).__name__}")
            return None
        self._clear_warn()
        self._prune_summaries(profile_id)
        return row_id

    def insert_imported(
        self, profile_id: str, title: str, content: str, *,
        signature: str = "", source_key: str = "",
    ) -> Literal["created", "duplicate", "skipped", "error"]:
        """memoria_import_20260718: insert ONE imported row (status='imported').

        Mirrors insert_summary's status-literal INSERT shape, but with an
        ON CONFLICT(profile_id, stable_key) DO NOTHING clause instead of a
        unique-timestamp key (D6): a re-import of the identical claim (same
        derive_import_key) collides and is skipped, so re-importing a file
        yields zero duplicates and never overwrites an operator's later curated
        edit. status='imported' is deliberately DISTINCT from 'curated' (F5 =
        operator-*touched*) — a machine-parsed file is not an operator touch —
        and inherits curated's durability for free: prune-immune (status!='draft'
        rows are excluded from _prune_profile's draft-only WHERE) AND upsert-
        immune (upsert_draft only overwrites status='draft'), so imports never
        eat the 200-draft pool (D1). No pruner: the D2 import cap is enforced at
        the route via count_imported (silent deletion of deliberate imports is
        dishonest).

        *source_key* is the caller-derived stable_key (derive_import_key of the
        claim); when omitted it is derived from *content*.

        Returns one of four DISTINCT per-item outcomes so WU3's route can account
        for every claim honestly (R4 — a lock loss must never be reported as a
        duplicate):
          - "created"   — a genuine fresh INSERT landed a new imported row.
          - "duplicate" — ON CONFLICT DO NOTHING: an identical claim (same
            stable_key) already exists; nothing written, nothing overwritten.
          - "skipped"   — the claim has too few significant tokens to derive a
            dedup-safe key (derive_import_key → None); invalid to store, NOT a
            collision. WU3 counts this as skipped_too_short, never a duplicate.
          - "error"     — a fail-open store failure (locked db / sqlite3.Error);
            the write did NOT happen and must be retried/surfaced, never counted
            as a duplicate.

        Never raises for a store error; raises MemoriaValidationError only for
        missing required inputs (RC-8: message is content-free).
        """
        profile_id = (profile_id or "").strip()
        title = " ".join((title or "").split())
        content = " ".join((content or "").split())
        signature = " ".join((signature or "").split())
        if not profile_id or not title or not content:
            raise MemoriaValidationError(
                "memoria import requires profile_id, title, and content"
            )
        stable_key = (source_key or "").strip() or derive_import_key(profile_id, content)
        if not stable_key:
            return "skipped"  # too few significant tokens — nothing dedup-safe to store

        now = _now_text()
        row_id = f"mem_{uuid4().hex}"
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                cur = conn.execute(
                    """
                    INSERT INTO memorias (
                        id, profile_id, stable_key, revision, title, content, status,
                        pinned, private, inactive, created_at, updated_at, signature
                    ) VALUES (?, ?, ?, 1, ?, ?, 'imported', 0, 0, 0, ?, ?, ?)
                    ON CONFLICT(profile_id, stable_key) DO NOTHING
                    RETURNING id
                    """,
                    (row_id, profile_id, stable_key, title, content, now, now, signature),
                )
                created = cur.fetchone() is not None
        except sqlite3.Error as exc:
            self._warn_once(f"memoria import write failed (fail-open): {type(exc).__name__}")
            return "error"
        self._clear_warn()
        return "created" if created else "duplicate"

    def update_row(
        self, memoria_id: str, *, title: str | None = None, content: str | None = None,
        signature: str | None = None, if_revision: int | None = None,
        if_status: str | None = None, status: str | None = None,
        touch_updated_at: bool = True, raising: bool = False,
    ) -> bool:
        """Operator edit: promotes status to curated in the same statement (F5).

        Returns False if the write failed (e.g. lock contention) — the row
        was NOT curated and remains auto-capture-eligible. Callers that
        require the edit to have taken effect (the management UI, slice
        6/7) MUST check the return value and surface a False to the
        operator; silently ignoring it would let the next auto-capture on
        the same stable_key overwrite content the operator believed frozen.

        Three OPT-IN widenings (memory_promotion_20260725 D6), in the same idiom
        as upsert_draft's *return_created* — omitting all three leaves the ~50
        existing callers byte-identical:

        *signature* rewrites the retrieval signature. update_row historically
        wrote only status/updated_at/title/content, so a promotion without it
        would leave a DURABLE row indexed on the speculative words of the
        original capture pair (``_build_memoria_draft`` derives the signature
        from ``f"{user} {assistant}"``). The judge passes
        ``build_signature(judged_text)``.

        *if_revision* appends ``AND revision = ?`` — optimistic concurrency
        against the read-inference-write race. The sweep reads drafts, spends up
        to its budget in inference, then writes; if the operator speaks on the
        same topic in that window upsert_draft bumps ``revision``, this write
        matches zero rows and returns False, and the caller leaves the draft
        UNJUDGED for the next launch instead of freezing a judgment of stale
        text into a durable row.

        *if_status* appends ``AND status = ?``. ``if_revision`` alone is NOT
        enough to detect an operator EDIT racing the sweep: an edit goes through
        this very method, which sets ``status='curated'`` but never bumps
        ``revision``, so the sweep's guard would still match and would overwrite
        the operator's own words with a judgment of the text he just replaced —
        and demote the row from ``curated`` (operator intent) to ``promoted``
        (machine), the exact inversion of F5. The sweep passes
        ``if_status="draft"``; a row that stopped being a draft between read and
        write is left alone and counted stale.

        *status* overrides the hardcoded ``'curated'``. Promotion writes
        ``'promoted'``: machine-judged rows labelled curated "would misreport
        provenance on every surface" (insert_summary's own rule), and the
        operator-facing `draft` badge would silently lose its meaning.

        *touch_updated_at* False keeps ``updated_at`` byte-identical — the same
        prune/recency neutrality ``mark_judged`` has. ``build_recency_lines``
        ranks the meta-recall block by ``updated_at DESC``, so a promotion write
        that bumped it would stamp weeks-old memories with launch time and evict
        genuinely recent ones from "what did we talk about last session"; the
        judgment rewrites text about an OLD conversation, so the capture
        timestamp stays the honest recency signal. Operator edits keep the
        default: an edit IS a fresh touch.
        """
        # Parameterised, never interpolated: *status* is internal today, but a
        # bound value costs nothing and keeps it that way.
        set_clauses = ["status = ?"]
        params: list[object] = [status or "curated"]
        if touch_updated_at:
            set_clauses.append("updated_at = ?")
            params.append(_now_text())
        if title is not None:
            set_clauses.append("title = ?")
            params.append(" ".join(title.split()))
        if content is not None:
            set_clauses.append("content = ?")
            params.append(" ".join(content.split()))
        if signature is not None:
            set_clauses.append("signature = ?")
            params.append(" ".join(signature.split()))
        where = "id = ?"
        params.append(memoria_id)
        if if_revision is not None:
            where += " AND revision = ?"
            params.append(int(if_revision))
        if if_status is not None:
            where += " AND status = ?"
            params.append(if_status)
        sql = f"UPDATE memorias SET {', '.join(set_clauses)} WHERE {where}"
        return self._execute_write(sql, params, error_label="update", raising=raising)

    def mark_judged(
        self, judged: list[tuple[str, int]], *, inactive: bool = False,
        if_status: str | None = None, raising: bool = False,
    ) -> int:
        """Stamp ``judged_at`` on ``(id, observed_revision)`` pairs — the ONLY
        judgment bookkeeping verb. Returns the number of rows actually stamped.

        NEVER touches ``updated_at`` and NEVER touches ``status``. That is the
        whole point (memory_promotion_20260725 D3a): ``set_flags`` bumps
        ``updated_at`` unconditionally, and ``_prune_profile`` keeps the newest
        MEMORIAS_PROFILE_CAP unpinned drafts ``ORDER BY updated_at DESC`` — so
        routing a rejection through set_flags would push the REJECTED row into
        the keep-window and evict a newer, still-unjudged draft. Promotion must
        never call set_flags.

        *inactive* additionally hides the row (the rejection path):
        ``list_injection_candidates`` filters ``inactive = 0``, so a rejected
        vague memory stops being injected while staying visible — and
        un-hideable — in the operator's own management UI. Nothing is deleted.

        Each pair carries the ``revision`` the sweep OBSERVED when it read the
        row, and the write matches on it. The reject path has no other guard —
        it performs no ``update_row`` — so without this a draft the operator
        refreshed with BETTER content mid-sweep would be hidden on the strength
        of a judgment of text it no longer holds. A row that moved matches zero
        times, stays unjudged, and is re-judged next launch, which is what the
        design's failure-mode table promises for both write paths.

        *if_status* (opt-in) appends ``AND status = ?`` to every pair term — the
        mirror of ``update_row``'s guard, and for the same reason: ``revision``
        alone cannot see an operator EDIT (``update_row``) or PIN (``set_flags``),
        both of which write ``status='curated'`` WITHOUT bumping it. Without it a
        memory the operator curated during the sweep window is stamped judged and
        HIDDEN on the strength of a judgment of the text he replaced — and unlike
        the keep path nothing ever recovers it, because ``upsert_draft``'s
        un-hiding CASE only fires on rows that are still drafts. The sweep passes
        ``if_status="draft"`` on the REJECT call ONLY: by the time it stamps a
        KEEP, ``update_row`` has already written ``status='promoted'`` for every
        confident one, so the same guard there would leave them unstamped and
        re-judged — a full inference re-paid every launch.

        Returns the COUNT of rows stamped, not a bool: the caller's `kept` /
        `rejected` counters are the owner's only feedback loop on the judge's
        criteria, so "the judge rejected nothing" and "N rows lost the race"
        must never collapse into the same number. 0 on an empty list. A write
        error is fail-open (0) by default; *raising* propagates ``sqlite3.Error``
        so the caller can count a lock separately from a lost race.
        """
        pairs = [(i, r) for i, r in judged if i]
        if not pairs:
            return 0
        set_clauses = ["judged_at = ?"]
        params: list[object] = [_now_text()]
        if inactive:
            set_clauses.append("inactive = 1")
        term = "(id = ? AND revision = ?)" if if_status is None else "(id = ? AND revision = ? AND status = ?)"
        where = " OR ".join(term for _ in pairs)
        for row_id, revision in pairs:
            params.extend((row_id, int(revision)))
            if if_status is not None:
                params.append(if_status)
        sql = f"UPDATE memorias SET {', '.join(set_clauses)} WHERE {where}"
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                stamped = conn.execute(sql, params).rowcount
        except sqlite3.Error as exc:
            if raising:
                raise
            self._warn_once(f"memoria store mark_judged failed (fail-open): {type(exc).__name__}")
            return 0
        self._clear_warn()
        return max(stamped, 0)

    def set_flags(
        self,
        memoria_id: str,
        *,
        pinned: bool | None = None,
        private: bool | None = None,
        inactive: bool | None = None,
        raising: bool = False,
    ) -> bool:
        """Toggle pin/private/inactive flags.

        Pinning or marking private promotes the row to curated in the SAME
        statement (F5, unified freeze rule) — a content judgment the engine
        must never overwrite again. Un-pinning/un-marking-private never
        demotes a curated row back to draft (one-way). `inactive` is a pure
        visibility flag and never touches status.

        MUTING (``inactive=True``) additionally CLEARS ``judged_at``, which is
        what makes the operator's mute self-identifying (memory_promotion
        _20260725). An UNCERTAIN keep is left status='draft' WITH a stamp, so
        when the operator mutes that row ``upsert_draft``'s ``inactive = CASE
        WHEN memorias.judged_at != '' THEN 0`` would read the stamp as "the JUDGE
        hid this" and silently un-hide it on the next capture of that topic. An
        empty stamp is exactly the signal that CASE tests for. The row still
        never reaches the judge — ``list_unjudged_drafts`` filters
        ``inactive = 0``. UN-hiding deliberately does NOT clear it: a re-exposed
        reject would go straight back to the judge and be re-rejected and
        re-hidden, undoing the operator and paying an inference for it.

        Returns False if the write failed (e.g. lock contention) — the row
        was NOT curated and remains auto-capture-eligible. Callers that
        require the freeze to have taken effect (the management UI, slice
        6/7) MUST check the return value and surface a False to the
        operator; silently ignoring it would let the next auto-capture on
        the same stable_key overwrite content the operator believed frozen.
        """
        set_clauses = ["updated_at = ?"]
        params: list[object] = [_now_text()]
        promotes = False
        if pinned is not None:
            set_clauses.append("pinned = ?")
            params.append(1 if pinned else 0)
            promotes = promotes or bool(pinned)
        if private is not None:
            set_clauses.append("private = ?")
            params.append(1 if private else 0)
            promotes = promotes or bool(private)
        if inactive is not None:
            set_clauses.append("inactive = ?")
            params.append(1 if inactive else 0)
            if inactive:
                set_clauses.append("judged_at = ''")
        if promotes:
            set_clauses.append("status = 'curated'")
        params.append(memoria_id)
        sql = f"UPDATE memorias SET {', '.join(set_clauses)} WHERE id = ?"
        return self._execute_write(sql, params, error_label="set_flags", raising=raising)

    def delete_row(self, memoria_id: str) -> bool:
        """Hard-delete a single memoria row (per-row analog of purge_profile).

        Idempotent (A-N2/B-NOTE-1): returns True whenever the postcondition
        holds — the row is absent from the store — whether this call
        deleted it or it was already gone (rowcount == 0). Only a genuine
        write error (e.g. lock contention) returns False; unlike
        update_row/set_flags, a False here must mean the delete did NOT
        take effect, never "the row wasn't there", or the slice-6
        management UI would surface a misleading MEMORIAS_WRITE_FAILED_TEXT
        for an already-deleted row.
        """
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                conn.execute("DELETE FROM memorias WHERE id = ?", (memoria_id,))
        except sqlite3.Error as exc:
            self._warn_once(f"memoria store delete_row failed (fail-open): {type(exc).__name__}")
            return False
        self._clear_warn()
        return True

    def purge_profile(self, profile_id: str) -> int:
        """Hard-delete ALL rows for profile_id. Returns the deleted row count."""
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                cur = conn.execute("DELETE FROM memorias WHERE profile_id = ?", (profile_id,))
                deleted = cur.rowcount
        except sqlite3.Error as exc:
            self._warn_once(f"memoria store purge failed (fail-open): {type(exc).__name__}")
            return 0
        self._clear_warn()
        return deleted

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, memoria_id: str, *, raising: bool = False) -> sqlite3.Row | None:
        """Fetch a single row by id, or None when absent.

        Fail-open by default (UI callers): a transient read failure logs once
        and returns None. When *raising* is set (API callers), a genuine
        ``sqlite3.Error`` propagates instead — so the handler can tell a
        transient lock (503) apart from a truly absent row (None -> 404),
        rather than collapsing both into a misleading 404.
        """
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                return conn.execute("SELECT * FROM memorias WHERE id = ?", (memoria_id,)).fetchone()
        except sqlite3.Error as exc:
            if raising:
                raise
            self._warn_once(f"memoria store read failed (fail-open): {type(exc).__name__}")
            return None

    def list_for_profile(self, profile_id: str, limit: int | None = None) -> list[sqlite3.Row]:
        cap = MEMORIAS_PROFILE_CAP if limit is None else limit
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                return conn.execute(
                    "SELECT * FROM memorias WHERE profile_id = ? "
                    "ORDER BY pinned DESC, updated_at DESC, id ASC LIMIT ?",
                    (profile_id, cap),
                ).fetchall()
        except sqlite3.Error as exc:
            self._warn_once(f"memoria store list failed (fail-open): {type(exc).__name__}")
            return []

    def count_all_pinned(self, profile_id: str) -> int:
        """Total pinned rows for profile_id — UNFILTERED by private/inactive
        and UNCAPPED by MEMORIAS_PROFILE_CAP (F6b, slice 7).

        This is the honest N half of the management UI's «Fijadas: N · se
        inyectan M» counter (see pinned_injection_counter for the M half,
        which is correctly computed from the already-filtered injection
        candidate set). Unlike list_for_profile/list_injection_candidates,
        this must count every pinned row even beyond the display cap —
        pinned rows are never pruned (see _prune_profile) so their true
        count could in principle exceed MEMORIAS_PROFILE_CAP.
        """
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM memorias WHERE profile_id = ? AND pinned = 1",
                    (profile_id,),
                ).fetchone()
                return row["n"] if row is not None else 0
        except sqlite3.Error as exc:
            self._warn_once(f"memoria store count_all_pinned failed (fail-open): {type(exc).__name__}")
            return 0

    def count_all(self, profile_id: str) -> int:
        """Total row count for profile_id — UNCAPPED by MEMORIAS_PROFILE_CAP
        (SF-1, slice 7 judge round). purge_profile deletes ALL rows for a
        profile; the purge confirm must never understate that with
        len(list_for_profile(...)), which caps display reads at
        MEMORIAS_PROFILE_CAP.

        Returns -1 (never a valid count) on a genuine read failure, so a
        destructive-action caller can tell "confirmed empty" (0) apart from
        "count unknown" (-1) and show an honest warning instead of a
        misleadingly low or zero count.
        """
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM memorias WHERE profile_id = ?",
                    (profile_id,),
                ).fetchone()
                return row["n"] if row is not None else 0
        except sqlite3.Error as exc:
            self._warn_once(f"memoria store count_all failed (fail-open): {type(exc).__name__}")
            return -1

    def count_imported(self, profile_id: str) -> int:
        """Total imported rows for profile_id (status='imported'), UNCAPPED
        (memoria_import_20260718). The route's D2 import-time cap check compares
        count_imported + new against MEMORIAS_IMPORT_CAP.

        Mirrors count_all: returns -1 (never a valid count) on a genuine read
        failure so a cap caller can tell 'unknown' apart from 'zero'.
        """
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM memorias WHERE profile_id = ? AND status = 'imported'",
                    (profile_id,),
                ).fetchone()
                return row["n"] if row is not None else 0
        except sqlite3.Error as exc:
            self._warn_once(f"memoria store count_imported failed (fail-open): {type(exc).__name__}")
            return -1

    def list_injection_candidates(self, profile_id: str) -> list[sqlite3.Row]:
        """Eligible rows for retrieval injection (R9): private=0 AND
        inactive=0, scoped to profile_id, capped at MEMORIAS_PROFILE_CAP.

        Pinned rows sort first (ORDER BY pinned DESC), so they are never
        pushed out by the cap ahead of unpinned rows — pinned rows are
        always in the candidate set before the cap is applied. Bounded
        read, fail-open to [] (mirrors list_for_profile).
        """
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                return conn.execute(
                    "SELECT * FROM memorias WHERE profile_id = ? AND private = 0 AND inactive = 0 "
                    "ORDER BY pinned DESC, updated_at DESC, id ASC LIMIT ?",
                    (profile_id, MEMORIAS_PROFILE_CAP),
                ).fetchall()
        except sqlite3.Error as exc:
            self._warn_once(f"memoria store injection-candidate list failed (fail-open): {type(exc).__name__}")
            return []

    def list_unjudged_drafts(self, profile_id: str, *, limit: int) -> list[sqlite3.Row]:
        """The judge's candidate batch: OLDEST-first unjudged drafts (D7).

        Oldest-first because those are the rows closest to prune death — the
        FIFO evicts by ``updated_at DESC``, so the oldest unjudged draft is the
        one that disappears if the sweep never reaches it.

        ``private = 0`` is redundant today (``_build_memoria_draft`` refuses to
        capture anything whose private tag is not False) but it is cheap and
        fail-closed: a private row must never reach a judge, least of all a
        cloud one. Bounded read, fail-open to [] (mirrors list_for_profile).

        ``inactive = 0`` is load-bearing on BOTH halves. A row the operator muted
        by hand is content he explicitly hid, so it must not be shipped to the
        active (possibly cloud) provider; and if it WERE judged, the resulting
        ``judged_at`` stamp would make upsert_draft's ``inactive = CASE WHEN
        memorias.judged_at != '' THEN 0 ...`` read the OPERATOR's mute as a
        JUDGE's mute and silently un-hide the row on the next capture, defeating
        the one guard that CASE exists to provide.
        """
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                return conn.execute(
                    "SELECT * FROM memorias WHERE profile_id = ? AND status = 'draft' "
                    "AND judged_at = '' AND private = 0 AND inactive = 0 "
                    "ORDER BY updated_at ASC, id ASC LIMIT ?",
                    (profile_id, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            self._warn_once(f"memoria unjudged-draft list failed (fail-open): {type(exc).__name__}")
            return []

    # NOTE (memory_promotion_20260725): the design's `list_durable_keys` +
    # "arithmetic dedup" step is NOT implemented, because the state it looks for
    # is unreachable. UNIQUE(profile_id, stable_key) is a TABLE constraint, so a
    # draft and a durable row in one profile can never share a stable_key at all;
    # a re-capture of something already durable resolves the upsert to a no-op
    # and inserts nothing (see test_promoted_row_is_upsert_immune, which pins the
    # row count). Re-judging an already-promoted row is likewise impossible:
    # list_unjudged_drafts filters status='draft' AND judged_at=''. Both guards
    # would have been permanently dead code behind a green test.

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _prune_profile(self, profile_id: str) -> None:
        """Delete oldest unpinned drafts beyond MEMORIAS_PROFILE_CAP for profile_id.

        Curated and pinned rows are excluded from both sides of this query,
        so they are never candidates for deletion (R16).
        """
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                conn.execute(
                    """
                    DELETE FROM memorias
                    WHERE profile_id = ? AND status = 'draft' AND pinned = 0
                      AND id NOT IN (
                        SELECT id FROM memorias
                        WHERE profile_id = ? AND status = 'draft' AND pinned = 0
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?
                      )
                    """,
                    (profile_id, profile_id, MEMORIAS_PROFILE_CAP),
                )
        except sqlite3.Error as exc:
            self._warn_once(f"memoria growth-cap prune failed (fail-open): {type(exc).__name__}")

    def _prune_summaries(self, profile_id: str) -> None:
        """Cap the machine-authored session-summary tier at MEMORIAS_SUMMARY_CAP
        newest rows per profile (W2a).

        Summary rows carry status='summary', so _prune_profile (draft-only)
        never touches them — without this dedicated pruner the durable tier
        would grow unbounded. The DELETE guard is the status='summary' filter,
        so operator-curated rows (status='curated', including a summary an
        operator later edits/pins) are never candidates for deletion. Newest by
        created_at (which equals the stable_key timestamp, distinct per insert
        by construction). Fail-open, and ALWAYS at WARNING (R4: losing a durable
        summary is the high-value signal, never demoted to a warn-once repeat).
        """
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                conn.execute(
                    """
                    DELETE FROM memorias
                    WHERE profile_id = ? AND status = 'summary'
                      AND id NOT IN (
                        SELECT id FROM memorias
                        WHERE profile_id = ? AND status = 'summary'
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                      )
                    """,
                    (profile_id, profile_id, MEMORIAS_SUMMARY_CAP),
                )
        except sqlite3.Error as exc:
            logger.warning(f"memoria summary-cap prune failed (fail-open): {type(exc).__name__}")

    def _init_db(self) -> None:
        with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memorias (
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    private INTEGER NOT NULL DEFAULT 0,
                    inactive INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(profile_id, stable_key)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memorias_profile_id ON memorias(profile_id)")
            # v1 -> v2 (memoria_rag_followups_20260716, candidate 2): add the
            # retrieval signature column + eager backfill from title+content.
            # Gated on user_version so it runs exactly once per db (fresh dbs
            # enter here at version 0 with zero rows to backfill; a second
            # construction sees version 2 and skips). Backfill NEVER touches
            # stable_key — legacy 2026-07-15 name-keyed rows keep their keys.
            #
            # Re-entrant + resumable (4R correction round): sqlite3's legacy
            # transaction control commits the ALTER (DDL) immediately, so a
            # kill between ALTER and the version bump leaves the column present
            # with user_version still < 2. Tolerate the duplicate-column error
            # on rerun and backfill only rows still empty (column default '').
            prior_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if prior_version < 2:
                try:
                    conn.execute("ALTER TABLE memorias ADD COLUMN signature TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):
                        raise  # genuinely broken db: keep the construction-raises contract
                backfilled = 0
                for row in conn.execute(
                    "SELECT id, title, content FROM memorias WHERE signature = ''"
                ).fetchall():
                    sig = build_signature(f"{row[1]} {row[2]}")
                    conn.execute("UPDATE memorias SET signature = ? WHERE id = ?", (sig, row[0]))
                    backfilled += 1
                conn.execute("PRAGMA user_version = 2")
                logger.info(
                    "memorias.db migrated to schema v2 (prior user_version=%d, backfilled=%d rows)",
                    prior_version, backfilled,
                )
            # v2 -> v3 (memory_promotion_20260725): the judge's per-row stamp.
            # Same idempotent, duplicate-column-tolerant shape as v1->v2 above
            # (DDL commits immediately, so a kill before the PRAGMA bump must be
            # resumable). NO backfill: every existing row starts unjudged, which
            # is exactly right — none of them has been judged. Downgrade stays
            # safe because upsert_draft/insert_* name their INSERT columns.
            if prior_version < 3:
                try:
                    conn.execute("ALTER TABLE memorias ADD COLUMN judged_at TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):
                        raise  # genuinely broken db: keep the construction-raises contract
                conn.execute("PRAGMA user_version = 3")
                logger.info(
                    "memorias.db migrated to schema v3 (prior user_version=%d)", prior_version,
                )

    def _connect(self, timeout: float) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute_write(self, sql: str, params: list[object], *, error_label: str, raising: bool = False) -> bool:
        """Run a write; return True when it affected >=1 row.

        Fail-open by default (UI callers): a lock/contention error logs once
        and returns False. When *raising* is set (API callers), a genuine
        ``sqlite3.Error`` propagates so the handler maps it to a 503
        write-failed; a plain ``False`` return then means ONLY rowcount==0 —
        the row vanished between the caller's pre-check and this write — which
        the handler maps to 404, not a misleading write-failed 503.
        """
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                cur = conn.execute(sql, params)
                affected = cur.rowcount > 0
        except sqlite3.Error as exc:
            if raising:
                raise
            self._warn_once(f"memoria store {error_label} failed (fail-open): {type(exc).__name__}")
            return False
        self._clear_warn()
        return affected

    def _warn_once(self, message: str) -> None:
        """Log a failure once per episode; subsequent repeats log at debug only."""
        with self._warn_lock:
            already_warned = self._write_failure_warned
            self._write_failure_warned = True
        if already_warned:
            logger.debug(message)
        else:
            logger.warning(message)

    def _clear_warn(self) -> None:
        """A healthy operation closes any prior degradation episode."""
        with self._warn_lock:
            self._write_failure_warned = False
