"""Local usage activity ledger (usage-activity-ledger-20261109).

A small SQLite store of daily COUNTS: calls, tokens, compute time, estimated
cache reuse and LAYA decision outcomes. It never stores chat text, prompts,
replies, image bytes, URLs or search queries -- only numbers, fixed enum labels,
model names and profile ids. The data never leaves the machine unless the owner
exports it.

Writes are best-effort: they never raise into a turn. A failure logs ONE
content-free WARNING per process. ``settings.USAGE_LEDGER_DB`` and
``settings.USAGE_LEDGER_ENABLED`` are read lazily at call time.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from opencohost.config import settings

logger = logging.getLogger("OpenCohost")

KINDS = (
    "turn", "guardrail_retry", "vision", "web_search",
    "topic_scout", "memoria_judge", "reflection",
)
SOURCES = ("direct", "ptt", "chat", "agenda", "accumulated", "other")
_SOURCE_ALIASES = {"kira-agenda": "agenda", "kira-agenda-stop": "agenda"}
_MAX_TEXT = 120

_lock = threading.RLock()
_initialised: set[str] = set()
_warned = False
_profile_provider: Optional[Callable[[], Optional[str]]] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily (
    date TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL,
    kind TEXT NOT NULL, source TEXT NOT NULL, profile_id TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0, compute_ms INTEGER NOT NULL DEFAULT 0,
    cache_est_reused INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, model, provider, kind, source, profile_id)
);
CREATE TABLE IF NOT EXISTS decision_daily (
    date TEXT NOT NULL, decision_id TEXT NOT NULL, mode TEXT NOT NULL,
    acted_by TEXT NOT NULL, status TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, decision_id, mode, acted_by, status)
);
CREATE TABLE IF NOT EXISTS cache_real_daily (
    date TEXT PRIMARY KEY, prompt_tokens INTEGER NOT NULL DEFAULT 0,
    reused_tokens INTEGER NOT NULL DEFAULT 0, full_reprocess INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_USAGE_COLS = ("date", "model", "provider", "kind", "source", "profile_id", "calls",
               "prompt_tokens", "completion_tokens", "compute_ms", "cache_est_reused", "failures")
_DECISION_COLS = ("date", "decision_id", "mode", "acted_by", "status", "count")
_CACHE_COLS = ("date", "prompt_tokens", "reused_tokens", "full_reprocess")


def _reset_for_tests() -> None:
    global _warned, _profile_provider
    with _lock:
        _initialised.clear()
        _warned = False
        _profile_provider = None


def set_profile_provider(fn: Optional[Callable[[], Optional[str]]]) -> None:
    """Register the callable returning the active profile id (id only)."""
    global _profile_provider
    _profile_provider = fn


def current_profile_id() -> str:
    """The active profile id, or "" when unknown. Never raises."""
    try:
        value = _profile_provider() if _profile_provider else None
        return _clean(value)
    except Exception:
        return ""


def enabled() -> bool:
    return bool(getattr(settings, "USAGE_LEDGER_ENABLED", True))


def _db_path() -> str:
    return str(settings.USAGE_LEDGER_DB)


def _today() -> str:
    return date.today().isoformat()


def _clean(value: Any) -> str:
    return value.strip()[:_MAX_TEXT] if isinstance(value, str) else ""


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _warn_once(exc: BaseException) -> None:
    global _warned
    with _lock:
        first = not _warned
        _warned = True
    if first:
        logger.warning("[USAGE_LEDGER] write failed: %s", type(exc).__name__)


def _connect(create: bool) -> Optional[sqlite3.Connection]:
    """Open the DB; None when absent and ``create`` is False."""
    path = _db_path()
    existed = os.path.exists(path)
    if not existed and not create:
        return None
    if not existed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=2.0)
    try:
        if path not in _initialised or not existed:
            con.executescript(_SCHEMA)
            con.commit()
            if not existed:
                _restrict(path)
            _initialised.add(path)
    except Exception:
        con.close()
        raise
    return con


def _restrict(path: str) -> None:
    try:
        from opencohost.stream_admin.oauth_store import restrict_file_permissions

        restrict_file_permissions(path)
    except Exception:
        logger.debug("usage ledger ACL failed", exc_info=True)


def _write(fn: Callable[[sqlite3.Connection], None]) -> None:
    if not enabled():
        return
    try:
        with _lock:
            con = _connect(create=True)
            try:
                fn(con)
                con.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('first_recorded', ?)",
                    (_today(),),
                )
                con.commit()
            finally:
                con.close()
    except Exception as exc:  # never break a turn
        _warn_once(exc)


def _norm_source(source: Any) -> str:
    s = _SOURCE_ALIASES.get(source, source) if isinstance(source, str) else ""
    return s if s in SOURCES else "other"


def call_model_name(request_model: Any, *, is_local: bool, provider_cfg: Any = None) -> str:
    """The model that actually answers a call, for the ledger and the trace.

    A local call runs ``request_model``. A cloud call runs the active cloud
    profile's model; the engine's ``request_model`` is still the loaded LOCAL
    model there, so booking it would file cloud usage under the wrong name.
    Falls back to ``request_model`` when the profile names no model. Never raises.
    """
    fallback = request_model if isinstance(request_model, str) else str(request_model or "")
    if is_local:
        return fallback
    try:
        profile = (provider_cfg.get("profiles") or {}).get(provider_cfg.get("active_provider"))
        model = profile.get("model") if isinstance(profile, dict) else None
        return model.strip() if isinstance(model, str) and model.strip() else fallback
    except Exception:
        return fallback


def record_call(
    *,
    model: str,
    is_local: bool,
    kind: str,
    source: str = "",
    profile_id: Optional[str] = None,
    prompt_tokens: Any = 0,
    completion_tokens: Any = 0,
    compute_ms: Any = 0,
    cache_est_reused: Any = 0,
    failed: bool = False,
    calls: int = 1,
) -> None:
    """Add one LLM call to today's row. Content-free; never raises."""
    try:
        if not enabled():
            return
        if kind not in KINDS:
            kind = "turn"
        key = (
            _today(), _clean(model), "local" if is_local else "cloud", kind,
            _norm_source(source) if kind in ("turn", "guardrail_retry") else "other",
            _clean(profile_id) if profile_id is not None else current_profile_id(),
        )
        vals = (_count(calls), _count(prompt_tokens), _count(completion_tokens),
                _count(compute_ms), _count(cache_est_reused), 1 if failed else 0)
    except Exception as exc:
        _warn_once(exc)
        return

    def op(con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO usage_daily(date,model,provider,kind,source,profile_id,calls,"
            "prompt_tokens,completion_tokens,compute_ms,cache_est_reused,failures) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,model,provider,kind,source,"
            "profile_id) DO UPDATE SET calls=calls+excluded.calls,"
            "prompt_tokens=prompt_tokens+excluded.prompt_tokens,"
            "completion_tokens=completion_tokens+excluded.completion_tokens,"
            "compute_ms=compute_ms+excluded.compute_ms,"
            "cache_est_reused=cache_est_reused+excluded.cache_est_reused,"
            "failures=failures+excluded.failures",
            key + vals,
        )

    _write(op)


def _field(resp: Any, key: str) -> Any:
    if resp is None:
        return None
    if isinstance(resp, dict):
        return resp.get(key)
    return getattr(resp, key, None)


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def usage_from_response(resp: Any, *, is_local: bool,
                        wall_ms: Any = None) -> tuple[int, int, int]:
    """``(prompt_tokens, completion_tokens, compute_ms)`` from a chat response.

    Local (Ollama): the counters and ``load + prefill + decode`` durations (ns).
    Cloud: ``usage.prompt_tokens/completion_tokens`` and the wall-clock ms.
    A local response without durations falls back to the wall-clock ms.
    Only numbers are read; never any text.
    """
    wall = _count(wall_ms)
    if not is_local:
        usage = _field(resp, "usage")
        return (_count(_field(usage, "prompt_tokens")),
                _count(_field(usage, "completion_tokens")), wall)
    ns = [_int_or_none(_field(resp, k)) for k in
          ("load_duration", "prompt_eval_duration", "eval_duration")]
    present = [v for v in ns if v is not None]
    compute = int(sum(present) / 1_000_000) if present else wall
    return (_count(_field(resp, "prompt_eval_count")),
            _count(_field(resp, "eval_count")), compute)


def record_response(resp: Any, *, model: str, is_local: bool, kind: str,
                    source: str = "", profile_id: Optional[str] = None,
                    wall_ms: Any = None, cache_est_reused: Any = 0) -> None:
    """Record one finished chat call from its response. Never raises."""
    try:
        if not enabled():
            return
        prompt, completion, compute = usage_from_response(resp, is_local=is_local, wall_ms=wall_ms)
    except Exception as exc:
        _warn_once(exc)
        return
    record_call(model=model, is_local=is_local, kind=kind, source=source,
                profile_id=profile_id, prompt_tokens=prompt, completion_tokens=completion,
                compute_ms=compute, cache_est_reused=cache_est_reused)


def record_web_search(*, profile_id: Optional[str] = None) -> None:
    """Count one web search execution (calls only; no tokens, no query)."""
    record_call(model="", is_local=True, kind="web_search", source="other",
                profile_id=profile_id)


def record_decision(decision_id: str, mode: str, acted_by: str, status: str) -> None:
    """Count one LAYA decision outcome. Enum labels only."""
    try:
        if not enabled():
            return
        key = (_today(), _clean(decision_id), _clean(mode), _clean(acted_by), _clean(status))
    except Exception as exc:
        _warn_once(exc)
        return

    def op(con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO decision_daily(date,decision_id,mode,acted_by,status,count) "
            "VALUES(?,?,?,?,?,1) ON CONFLICT(date,decision_id,mode,acted_by,status) "
            "DO UPDATE SET count=count+1",
            key,
        )

    _write(op)


# -- reads ------------------------------------------------------------------

def _agg(con, group_cols: str, where: str, params: tuple, extra_where: str = "") -> list[dict]:
    cols = group_cols.split(",")
    rows = con.execute(
        f"SELECT {group_cols}, SUM(calls), SUM(prompt_tokens), SUM(completion_tokens), "
        f"SUM(compute_ms) FROM usage_daily WHERE {where}{extra_where} "
        f"GROUP BY {group_cols} ORDER BY SUM(calls) DESC, {cols[0]}",
        params,
    ).fetchall()
    out = []
    for r in rows:
        d = {c: r[i] for i, c in enumerate(cols)}
        d.update(calls=r[len(cols)], prompt_tokens=r[len(cols) + 1],
                 completion_tokens=r[len(cols) + 2], compute_ms=r[len(cols) + 3])
        out.append(d)
    return out


def _ingest_real_cache(con: sqlite3.Connection) -> None:
    """Fold new runner-log tasks into ``cache_real_daily`` (best-effort)."""
    try:
        from opencohost.core.usage import runner_cache

        runner_cache.ingest(con)
    except Exception as exc:
        _warn_once(exc)


def summary(date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    """The ``/api/usage/summary`` payload (see the feature contract)."""
    today = date.today()
    d_to = date_to or today.isoformat()
    d_from = date_from or (today - timedelta(days=365)).isoformat()
    out: dict[str, Any] = {
        "from": d_from, "to": d_to, "enabled": enabled(), "first_recorded": None,
        "days": [], "by_model": [], "by_kind": [], "by_source": [], "by_profile": [],
        "cache": {"estimated_reused_tokens": 0, "real_available": False,
                  "real_prompt_tokens": None, "real_reused_tokens": None},
        "decisions": [],
    }
    try:
        with _lock:
            con = _connect(create=False)
            if con is None:
                return out
            try:
                _ingest_real_cache(con)
                p = (d_from, d_to)
                w = "date >= ? AND date <= ?"
                fr = con.execute("SELECT value FROM meta WHERE key='first_recorded'").fetchone()
                out["first_recorded"] = fr[0] if fr else None
                out["days"] = [
                    {"date": r[0], "calls": r[1], "prompt_tokens": r[2],
                     "completion_tokens": r[3], "compute_ms": r[4]}
                    for r in con.execute(
                        "SELECT date, SUM(calls), SUM(prompt_tokens), SUM(completion_tokens), "
                        f"SUM(compute_ms) FROM usage_daily WHERE {w} GROUP BY date ORDER BY date", p)
                ]
                out["by_model"] = _agg(con, "model,provider", w, p, " AND kind != 'web_search'")
                out["by_kind"] = _agg(con, "kind", w, p)
                out["by_source"] = _agg(con, "source", w, p, " AND kind = 'turn'")
                out["by_profile"] = _agg(con, "profile_id", w, p)
                est = con.execute(
                    f"SELECT COALESCE(SUM(cache_est_reused),0) FROM usage_daily WHERE {w}", p
                ).fetchone()[0]
                out["cache"]["estimated_reused_tokens"] = est
                seen = con.execute("SELECT value FROM meta WHERE key='runner_log_seen'").fetchone()
                if seen and seen[0] == "1":
                    pt, rt = con.execute(
                        "SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(reused_tokens),0) "
                        f"FROM cache_real_daily WHERE {w}", p).fetchone()
                    out["cache"].update(real_available=True, real_prompt_tokens=pt,
                                        real_reused_tokens=rt)
                decisions: dict[str, dict] = {}
                for did, acted, status, n in con.execute(
                    "SELECT decision_id, acted_by, status, SUM(count) FROM decision_daily "
                    f"WHERE {w} GROUP BY decision_id, acted_by, status ORDER BY decision_id", p):
                    d = decisions.setdefault(did, {"decision_id": did, "asked": 0, "laya_acted": 0,
                                                   "rule_acted": 0, "by_status": {}})
                    d["asked"] += n
                    if acted == "laya":
                        d["laya_acted"] += n
                    elif acted == "rule":
                        d["rule_acted"] += n
                    d["by_status"][status] = d["by_status"].get(status, 0) + n
                out["decisions"] = list(decisions.values())
            finally:
                con.close()
    except Exception as exc:
        _warn_once(exc)
    return out


def export_rows() -> list[dict]:
    """Every stored daily row, each tagged with its ``table``."""
    rows: list[dict] = []
    try:
        with _lock:
            con = _connect(create=False)
            if con is None:
                return rows
            try:
                for table, cols in (("usage_daily", _USAGE_COLS),
                                    ("decision_daily", _DECISION_COLS),
                                    ("cache_real_daily", _CACHE_COLS)):
                    for r in con.execute(
                        f"SELECT {','.join(cols)} FROM {table} ORDER BY date"
                    ):
                        rows.append({"table": table, **dict(zip(cols, r))})
            finally:
                con.close()
    except Exception as exc:
        _warn_once(exc)
    return rows


def purge() -> bool:
    """Delete every ledger row (and the runner-log offset). True on success."""
    try:
        with _lock:
            con = _connect(create=False)
            if con is None:
                return True
            try:
                for table in ("usage_daily", "decision_daily", "cache_real_daily"):
                    con.execute(f"DELETE FROM {table}")
                # Keep the runner-log offset so purged history is not re-ingested.
                con.execute("DELETE FROM meta WHERE key NOT IN "
                            "('runner_log_offset','runner_log_seen')")
                con.commit()
            finally:
                con.close()
        return True
    except Exception as exc:
        _warn_once(exc)
        return False
