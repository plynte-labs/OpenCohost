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
  - stable_key / title derivation applies a small domain-stopword list on
    top of opencohost.core.editorial_matching.normalize_tokens (which stays
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
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from opencohost.config.settings import (
    MEMORIAS_MAX_INJECT_CHARS,
    MEMORIAS_MAX_PINNED_INJECT,
    MEMORIAS_PINNED_CLIP_CHARS,
    MEMORIAS_PROFILE_CAP,
)
from opencohost.core.editorial_matching import normalize_tokens

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
})


class MemoriaValidationError(ValueError):
    """Raised when a memoria write is missing required content.

    The message is a static, content-free string by design (RC-8): it must
    never interpolate row title/content, since callers may log it.
    """


# ---------------------------------------------------------------------------
# Pure helpers — stable_key / title derivation (RC-1, RC-7)
# ---------------------------------------------------------------------------

def _significant_tokens(text: str) -> list[str]:
    """normalize_tokens(text) minus domain stopwords, deduped, first-occurrence order."""
    seen: set[str] = set()
    result: list[str] = []
    for token in normalize_tokens(text or ""):
        if token in _MEMORIA_DOMAIN_STOPWORDS or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


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


def select_top_k(topic_text: str, rows, k: int = 3) -> list:
    """Lexical top-k of *rows* against *topic_text* by signature overlap.

    Candidate 2 (memoria_rag_followups_20260716): each row is scored on the
    shared-token count between the topic's significant tokens and the row's
    stored 12-token signature (title fallback when the signature is empty,
    e.g. an all-stopword legacy backfill). Requires >= _MIN_SHARED_TOKENS
    shared tokens — one incidental shared word never matches (stricter than
    the old 1-token/0.25 title threshold, by design: precision over recall,
    ADR-034). Returns up to *k* rows, best overlap ratio first.
    """
    topic = set(_significant_tokens(topic_text)) - _SCORING_STOPWORDS
    scored = []
    for row in rows:
        sig = set((row["signature"] or row["title"]).split()) - _SCORING_STOPWORDS
        shared = len(topic & sig)
        if shared >= _MIN_SHARED_TOKENS:
            scored.append((shared / max(len(sig), 1), row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored[:k]]


def _clip_for_injection(text: str, limit: int) -> str:
    """Hard-cut *text* to at most *limit* chars total, ellipsis included.

    Never mutates the caller's row — operates on a plain string copy.
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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

    def _try_add(text: str, *, clip_to_remaining: bool = False) -> None:
        nonlocal used
        sep = 1 if lines else 0
        remaining = max_chars - used - sep
        if len(text) > remaining:
            # Candidate 2: selected non-pinned rows are clipped into whatever
            # budget remains instead of being silently dropped — unless the
            # remainder is too small to carry meaning. Pinned rows keep the
            # original reject-when-over-budget behavior (already pre-clipped).
            if not clip_to_remaining or remaining < _MIN_CLIP_REMAINDER_CHARS:
                return
            text = _clip_for_injection(text, remaining)
        lines.append(text)
        used += sep + len(text)

    for row in pinned_rows:
        _try_add(_clip_for_injection(row["content"], pinned_clip_chars))

    for row in select_top_k(topic_text, non_pinned_rows, k=top_k):
        _try_add(row["content"], clip_to_remaining=True)

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
        Off by default so the ~50 existing callers keep the bare-id contract —
        only the engine's memoria.captured notice needs the flag.
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
                        updated_at = excluded.updated_at
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
            logger.debug("memoria upsert conflict stable_key=%s revision=%s", stable_key, revision)
        return (written_id, revision == 1) if return_created else written_id

    def update_row(self, memoria_id: str, *, title: str | None = None, content: str | None = None, raising: bool = False) -> bool:
        """Operator edit: promotes status to curated in the same statement (F5).

        Returns False if the write failed (e.g. lock contention) — the row
        was NOT curated and remains auto-capture-eligible. Callers that
        require the edit to have taken effect (the management UI, slice
        6/7) MUST check the return value and surface a False to the
        operator; silently ignoring it would let the next auto-capture on
        the same stable_key overwrite content the operator believed frozen.
        """
        set_clauses = ["status = 'curated'", "updated_at = ?"]
        params: list[object] = [_now_text()]
        if title is not None:
            set_clauses.append("title = ?")
            params.append(" ".join(title.split()))
        if content is not None:
            set_clauses.append("content = ?")
            params.append(" ".join(content.split()))
        params.append(memoria_id)
        sql = f"UPDATE memorias SET {', '.join(set_clauses)} WHERE id = ?"
        return self._execute_write(sql, params, error_label="update", raising=raising)

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
