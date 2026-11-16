"""Per-call prompt traceability (prompt-trace-20261007).

T1 -- ALWAYS ON, content-free: one INFO ``[PROMPT_TRACE]`` line per LLM call in
the main log (section names, char counts, short hashes, token stats; never text).

T2 -- OPT-IN (``OPENCOHOST_PROMPT_TRACE=1``, default OFF): one JSON line per call
appended to ``<data_root>/logs/prompt_trace/YYYYMMDD.jsonl`` with the full
messages sent and the raw reply, plus a ``decisions`` key (the turn's
decision outcomes, laya-decision-layer-20261009) when any decision ran. This is the ONLY place raw chat may be
persisted, by explicit owner authorization (2026-10-07); it never reaches the
main log. Any IO failure logs one content-free WARNING and the turn goes on.

Ollama reports ``prompt_eval_count`` as only the tokens it newly evaluated when
it reuses the KV prefix, and does not report the total. Reuse is therefore an
ESTIMATE: per model we remember the highest tokens-per-char ratio seen (a cold
evaluation) and ``cache_reused = chars * best_ratio - prompt_eval`` (``~N``).
The first call of a model reports 0. Cloud calls report ``n/a``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from opencohost.config import settings
from opencohost.core.engine import turn_state

logger = logging.getLogger("OpenCohost")

RETENTION_SECONDS = 3 * 86400

_lock = threading.Lock()
_pruned = False
_warned = False
_best_tokens_per_char: dict[str, float] = {}


def _reset_for_tests() -> None:
    global _pruned, _warned
    with _lock:
        _pruned = False
        _warned = False
        _best_tokens_per_char.clear()


def short_hash(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:6]


def _get(resp: Any, key: str) -> Any:
    if resp is None:
        return None
    if isinstance(resp, dict):
        return resp.get(key)
    return getattr(resp, key, None)


def _reply_parts(resp: Any) -> tuple[str, str]:
    msg = _get(resp, "message")
    content = _get(msg, "content") or ""
    thinking = _get(msg, "thinking") or ""
    if not content and isinstance(resp, dict):
        content = resp.get("text") or ""
    return str(content), str(thinking)


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _content_len(message: Any) -> int:
    content = message.get("content", "") if isinstance(message, dict) else ""
    return len(content) if isinstance(content, str) else len(str(content))


class TraceCall:
    """One traced LLM call. ``finish``/``fail`` emit T1 (and T2 when enabled)."""

    def __init__(
        self,
        *,
        source: str,
        model: str,
        is_local: bool,
        messages: Sequence[Any],
        options: dict[str, Any],
        sections: Sequence[tuple[str, str]],
        path: str,
        decisions: Optional[Sequence[dict]] = None,
        profile: Optional[str] = None,
        layout: Optional[str] = None,
        evicted_pairs: Optional[int] = None,
        head_moved: Optional[int] = None,
    ) -> None:
        self.turn = uuid.uuid4().hex[:8]
        # The foreground turn's tid (None off-turn); captured here, on the thread
        # that builds the request, because the line may be emitted elsewhere.
        self.tid = turn_state.current_tid()
        self.source = source
        self.model = model
        self.is_local = is_local
        self.path = path
        self.options = dict(options or {})
        self.sections = [(str(n), str(t)) for n, t in sections]
        # Snapshot: eviction may later mutate the live list; log what was sent.
        self.messages = [dict(m) if isinstance(m, dict) else m for m in messages]
        # laya-decision-layer-20261009: decision outcomes (ids, values, codes;
        # never text). None keeps the T2 record unchanged.
        self.decisions = [dict(d) for d in decisions] if decisions is not None else None
        # compact-prompt-profile-20261009: "full"/"compact"; None keeps the
        # T1 line and the T2 record unchanged.
        self.profile = profile if isinstance(profile, str) and profile else None
        # ADR-064: prompt layout ("v2"/"legacy") and the pairs evicted this
        # turn. None keeps the T1 line and the T2 record unchanged.
        self.layout = layout if isinstance(layout, str) and layout else None
        self.evicted_pairs = evicted_pairs if isinstance(evicted_pairs, int) else None
        # 1 when the first history message differs from the previous turn's.
        self.head_moved = head_moved if isinstance(head_moved, int) else None
        self._done = False
        self._started = time.monotonic()

    # -- helpers
    def _history(self) -> tuple[int, int]:
        body = [m for m in self.messages if isinstance(m, dict)]
        if body and body[0].get("role") == "system":
            body = body[1:]
        if body:
            body = body[:-1]  # the last message is the current user turn
        return len(body), sum(_content_len(m) for m in body)

    def _history_pairs(self) -> int:
        """History pairs sent: one per assistant message, so an assistant-only
        chat history (no user turns) still counts its replies."""
        body = [m for m in self.messages if isinstance(m, dict)]
        if body and body[0].get("role") == "system":
            body = body[1:]
        if body:
            body = body[:-1]
        return sum(1 for m in body if m.get("role") == "assistant")

    def _stable_prefix(self) -> str:
        """Short hash of the system message (ADR-064); "-" without one."""
        first = next((m for m in self.messages if isinstance(m, dict)), None)
        if isinstance(first, dict) and first.get("role") == "system":
            return short_hash(first.get("content", ""))
        return "-"

    def _prompt_chars(self) -> int:
        return sum(_content_len(m) for m in self.messages)

    def _cache_reused(self, prompt_eval: Optional[int]) -> str:
        if not self.is_local:
            return "n/a"
        if prompt_eval is None:
            return "unknown"
        chars = self._prompt_chars()
        if chars <= 0:
            return "unknown"
        with _lock:
            best = _best_tokens_per_char.get(self.model, 0.0)
            ratio = prompt_eval / chars
            if ratio > best:
                _best_tokens_per_char[self.model] = ratio
                best = ratio
        reused = int(round(chars * best)) - prompt_eval
        return f"~{reused}" if reused > 0 else "0"

    def _ledger(self, response: Any, error: Optional[str]) -> None:
        """Record this call in the local usage ledger (numbers only; never raises)."""
        try:
            from opencohost.core.usage import ledger

            if not ledger.enabled():
                return
            wall_ms = int((time.monotonic() - self._started) * 1000)
            kind = "guardrail_retry" if self.source == "guardrail_retry" else "turn"
            if error:
                ledger.record_call(model=self.model, is_local=self.is_local, kind=kind,
                                   source=self.source, compute_ms=wall_ms, failed=True)
                return
            reused = 0
            if self.is_local:
                prompt_eval = _int(_get(response, "prompt_eval_count"))
                est = self._cache_reused(prompt_eval)
                reused = int(est[1:]) if est.startswith("~") else 0
            ledger.record_response(response, model=self.model, is_local=self.is_local,
                                   kind=kind, source=self.source, wall_ms=wall_ms,
                                   cache_est_reused=reused)
        except Exception:
            logger.debug("usage ledger hook failed", exc_info=True)

    def _t1(self, prompt_eval, eval_count, done_reason, error) -> None:
        sections = ",".join(f"{n}:{len(t)}#{short_hash(t)}" for n, t in self.sections) or "-"
        h_msgs, h_chars = self._history()
        think = self.options.get("think")
        line = (
            f"[PROMPT_TRACE] turn={self.turn} source={self.source} model={self.model} "
            f"sections={sections} history_msgs={h_msgs} history_chars={h_chars} "
            f"prompt_chars={self._prompt_chars()} "
            f"num_predict={self.options.get('num_predict', '-')} "
            f"think={'-' if think is None else ('on' if think else 'off')} "
            f"prompt_eval={'-' if prompt_eval is None else prompt_eval} "
            f"eval={'-' if eval_count is None else eval_count} "
            f"done={done_reason or '-'} cache_reused={self._cache_reused(prompt_eval)}"
        )
        if error:
            line += f" error={error}"
        if self.profile:
            line += f" profile={self.profile}"
        if self.layout:
            # Appended after every legacy field, so a key=value parser keeps working.
            line += (
                f" layout={self.layout} stable_prefix={self._stable_prefix()}"
                f" history_pairs={self._history_pairs()}"
                f" evicted_pairs={0 if self.evicted_pairs is None else self.evicted_pairs}"
                f" head_moved={0 if not self.head_moved else 1}"
            )
        if self.tid:
            line += f" tid={self.tid}"
        logger.info(line)

    def _t2(self, record: dict[str, Any]) -> None:
        global _pruned, _warned
        if not getattr(settings, "PROMPT_TRACE_ENABLED", False):
            return
        try:
            directory = os.path.join(str(settings.LOG_DIR), "prompt_trace")
            os.makedirs(directory, exist_ok=True)
            payload = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            name = datetime.now().strftime("%Y%m%d") + ".jsonl"
            with _lock:
                if not _pruned:
                    _pruned = True
                    _prune(directory)
                with open(os.path.join(directory, name), "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(payload)
        except Exception as exc:  # never break a turn
            with _lock:
                first = not _warned
                _warned = True
            if first:
                logger.warning("[PROMPT_TRACE] content trace write failed: %s", type(exc).__name__)

    def _emit(self, *, reply, thinking, prompt_eval, eval_count, done_reason, error) -> None:
        if self._done:
            return
        self._done = True
        try:
            self._t1(prompt_eval, eval_count, done_reason, error)
        except Exception:
            logger.debug("prompt trace T1 failed", exc_info=True)
        if getattr(settings, "PROMPT_TRACE_ENABLED", False):
            record = {
                "turn": self.turn,
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "source": self.source,
                "model": self.model,
                "path": self.path,
                "local": self.is_local,
                "options": {
                    k: self.options.get(k)
                    for k in ("num_predict", "think", "temperature", "num_ctx")
                },
                "sections": [
                    {"name": n, "chars": len(t), "hash": short_hash(t)} for n, t in self.sections
                ],
                "messages": self.messages,
                "reply": reply,
                "thinking": thinking,
                "done_reason": done_reason,
                "prompt_eval_count": prompt_eval,
                "eval_count": eval_count,
                "error": error,
            }
            if self.decisions is not None:
                record["decisions"] = self.decisions
            if self.profile:
                record["profile"] = self.profile
            if self.layout:
                record["layout"] = self.layout
            self._t2(record)

    def _ledger_once(self, response: Any, error: Optional[str]) -> None:
        if self._done:
            return
        self._ledger(response, error)

    # -- public
    def finish(self, response: Any) -> None:
        self._ledger_once(response, None)
        try:
            reply, thinking = _reply_parts(response)
            dr = _get(response, "done_reason")
            self._emit(
                reply=reply,
                thinking=thinking,
                prompt_eval=_int(_get(response, "prompt_eval_count")),
                eval_count=_int(_get(response, "eval_count")),
                done_reason=dr if isinstance(dr, str) else None,
                error=None,
            )
        except Exception:
            logger.debug("prompt trace finish failed", exc_info=True)

    def fail(self, exc: BaseException, *, partial: str = "", thinking: str = "") -> None:
        self._ledger_once(None, type(exc).__name__)
        try:
            self._emit(
                reply=partial,
                thinking=thinking,
                prompt_eval=None,
                eval_count=None,
                done_reason=None,
                error=type(exc).__name__,
            )
        except Exception:
            logger.debug("prompt trace fail failed", exc_info=True)


def _prune(directory: str) -> None:
    cutoff = time.time() - RETENTION_SECONDS
    try:
        for entry in os.scandir(directory):
            if entry.is_file() and entry.name.endswith(".jsonl"):
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.remove(entry.path)
                except OSError:
                    continue
    except OSError:
        pass


def begin(
    *,
    source: str,
    model: str,
    is_local: bool,
    messages: Sequence[Any],
    options: dict[str, Any],
    sections: Sequence[tuple[str, str]] = (),
    path: str = "buffered",
    decisions: Optional[Sequence[dict]] = None,
    profile: Optional[str] = None,
    layout: Optional[str] = None,
    evicted_pairs: Optional[int] = None,
    head_moved: Optional[int] = None,
) -> Optional[TraceCall]:
    """Start tracing one LLM call; never raises (returns None on failure)."""
    try:
        return TraceCall(
            source=source,
            model=model,
            is_local=is_local,
            messages=messages,
            options=options,
            sections=sections,
            path=path,
            decisions=decisions,
            profile=profile,
            layout=layout,
            evicted_pairs=evicted_pairs,
            head_moved=head_moved,
        )
    except Exception:
        logger.debug("prompt trace begin failed", exc_info=True)
        return None
