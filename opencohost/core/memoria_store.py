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

MEMORIAS_ENABLED stays False until the final slice — this module is only
exercised by its own unit tests until the capture/retrieval/UI slices wire
it in.
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
from opencohost.core.editorial_matching import match_score, normalize_tokens

logger = logging.getLogger(__name__)

# Saves/reads can fire from multiple threads; never wait longer than these
# (sqlite default is 5s — a visible freeze). Mirrors agenda_persistence.py.
READ_TIMEOUT_SECONDS: float = 0.5
WRITE_TIMEOUT_SECONDS: float = 1.0

_MIN_SIGNIFICANT_TOKENS = 3
_STABLE_KEY_TOKEN_COUNT = 6
_TITLE_TOKEN_COUNT = 3

# Closed list (design v2.1 residual risk #3): near-ubiquitous tokens in
# captured co-host turns — Kira's own name, streaming tooling, and
# meta-conversation words about profiles/topics/remembering/chat — that
# survive normalize_tokens's generic stopwords but never discriminate
# between memories. Calibrated against the RC-1/RC-7 test fixtures below.
# Do not expand/shrink without updating those tests in lockstep.
_MEMORIA_DOMAIN_STOPWORDS: frozenset[str] = frozenset({
    "kira", "obs", "tema", "perfil", "usuario", "recuerda", "dice", "streamer", "chat",
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


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Retrieval + injection (slice 5, design v2.1 §7, F6) — pure, no I/O
# ---------------------------------------------------------------------------

# One shared significant title token scores 1/3 ~= 0.33 >= 0.25. RC-7's
# title derivation (domain stopwords excluded, see build_title above) is
# what keeps this threshold from firing on shared domain-noise words.
_TOPIC_MATCH_THRESHOLD = 0.25


class _TopicShim:
    """Exposes ONLY the title to match_score (design fix) — content is never
    fed into the scorer, or the 0.25 threshold would effectively never fire
    (titles are a handful of tokens; content is long and dilutes overlap).
    editorial_matching.match_score is reused verbatim, unmodified."""

    __slots__ = ("topic", "triggers")

    def __init__(self, topic: str) -> None:
        self.topic = topic
        self.triggers: list[str] = []


def select_top_k(topic_text: str, rows, k: int = 3) -> list:
    """Lexical top-k of *rows* against *topic_text* by title-only match_score.

    Returns up to *k* rows scoring >= _TOPIC_MATCH_THRESHOLD, best first.
    """
    scored = [
        (match_score(topic_text, _TopicShim(row["title"])), row)
        for row in rows
    ]
    scored = [pair for pair in scored if pair[0] >= _TOPIC_MATCH_THRESHOLD]
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

    def _try_add(text: str) -> None:
        nonlocal used
        sep = 1 if lines else 0
        if used + sep + len(text) > max_chars:
            return
        lines.append(text)
        used += sep + len(text)

    for row in pinned_rows:
        _try_add(_clip_for_injection(row["content"], pinned_clip_chars))

    for row in select_top_k(topic_text, non_pinned_rows, k=top_k):
        _try_add(row["content"])

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

    def upsert_draft(self, profile_id: str, stable_key: str, title: str, content: str) -> str | None:
        """Insert a new draft or refresh an existing draft sharing stable_key.

        Curated rows are upsert-immune (the WHERE clause makes the conflict
        resolve to a no-op — no write, no exception). Returns the row id on
        success, or None when the row was curated-immune or the write
        failed open (locked db). Raises MemoriaValidationError for missing
        required inputs.
        """
        profile_id = (profile_id or "").strip()
        stable_key = (stable_key or "").strip()
        title = " ".join((title or "").split())
        content = " ".join((content or "").split())
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
                        pinned, private, inactive, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?, 'draft', 0, 0, 0, ?, ?)
                    ON CONFLICT(profile_id, stable_key) DO UPDATE SET
                        content = excluded.content,
                        title = excluded.title,
                        revision = memorias.revision + 1,
                        updated_at = excluded.updated_at
                    WHERE memorias.status = 'draft'
                    RETURNING id, revision
                    """,
                    (row_id, profile_id, stable_key, title, content, now, now),
                )
                result = cur.fetchone()
        except sqlite3.Error as exc:
            self._warn_once(f"memoria store write failed (fail-open): {type(exc).__name__}")
            return None

        self._clear_warn()
        if result is None:
            return None  # curated row: upsert-immune, no write happened

        written_id, revision = result["id"], result["revision"]
        if revision == 1:
            self._prune_profile(profile_id)
        else:
            logger.debug("memoria upsert conflict stable_key=%s revision=%s", stable_key, revision)
        return written_id

    def update_row(self, memoria_id: str, *, title: str | None = None, content: str | None = None) -> bool:
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
        return self._execute_write(sql, params, error_label="update")

    def set_flags(
        self,
        memoria_id: str,
        *,
        pinned: bool | None = None,
        private: bool | None = None,
        inactive: bool | None = None,
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
        return self._execute_write(sql, params, error_label="set_flags")

    def delete_row(self, memoria_id: str) -> bool:
        """Hard-delete a single memoria row (per-row analog of purge_profile).

        Returns True if a row was deleted, False if not found or the write
        failed open (mirrors update_row/set_flags's bool contract, A-SF1 —
        the slice-6 management UI checks this and surfaces a False rather
        than silently assuming success).
        """
        return self._execute_write(
            "DELETE FROM memorias WHERE id = ?", [memoria_id], error_label="delete_row"
        )

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

    def get(self, memoria_id: str) -> sqlite3.Row | None:
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                return conn.execute("SELECT * FROM memorias WHERE id = ?", (memoria_id,)).fetchone()
        except sqlite3.Error as exc:
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
            conn.execute("PRAGMA user_version = 1")

    def _connect(self, timeout: float) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute_write(self, sql: str, params: list[object], *, error_label: str) -> bool:
        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                cur = conn.execute(sql, params)
                affected = cur.rowcount > 0
        except sqlite3.Error as exc:
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
