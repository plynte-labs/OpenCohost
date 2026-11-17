"""opencohost/core/engine/llm_engine_reflection.py

Owner reflection memory (ADR-065): the engine side. A dedicated daemon worker distils
the owner's recent exchanges into a few durable Q&A facts while the engine is idle, and
writes them through ``_capture_memoria`` so ranking, scrub/origin rules and judge
supersession all apply. The job never runs on the engine thread and never uses the
watchdog path (which abandons a request without cancelling it): a turn arrival closes
the in-flight HTTP response from the arriving thread, the only way to free the single
Ollama runner.

Default OFF behind ``OPENCOHOST_OWNER_REFLECTION``. With the flag off no state is
created, no thread starts and every hook returns immediately.

Logging is counters and fixed reason codes only (``format_log_line``): no fact, turn,
title or provenance text ever reaches a log line.
"""
import json
import threading
import time
from typing import Optional

from opencohost.config import settings as _settings

# Module object, resolved at CALL time (see the note in llm_engine_memorias.py).
from opencohost.core import llm_engine as _eng

_monotonic = time.monotonic
_INIT_LOCK = threading.Lock()
_WATCH_POLL_S = 0.025
_RETRY_BACKOFF_S = 30.0
_BACKOFF_CAP_S = 600.0
_REPEAT_FACT_WINDOW = 20  # newest injection candidates compared against (ADR-065 section 6)


class _Cancelled(Exception):
    """A turn arrived (or the session reset) while the job was running."""


class _OllamaStreamHandle:
    """Streaming ``/api/chat`` response that ``close()`` can release from ANY thread.

    The request is sent from ``iter_lines`` on the worker thread. ``close()`` closes the
    response and the dedicated client, which also aborts a ``send`` still blocked on
    prefill; Ollama then frees the runner when it sees the socket close (live gate, T5).
    """

    def __init__(self, client, payload: dict) -> None:
        self._client = client
        self._payload = payload
        self._response = None
        self._closed = threading.Event()

    def _send(self, payload: dict):
        request = self._client.build_request("POST", "/api/chat", json=payload)
        response = self._client.send(request, stream=True)
        if response.status_code == 400 and "think" in payload:
            response.close()  # the model does not support `think`: retry once without it
            request = self._client.build_request(
                "POST", "/api/chat", json={k: v for k, v in payload.items() if k != "think"}
            )
            response = self._client.send(request, stream=True)
        if response.status_code != 200:
            code = response.status_code
            response.close()
            raise RuntimeError(f"ollama_http_{code}")
        return response

    def iter_lines(self):
        response = self._send(self._payload)
        self._response = response
        if self._closed.is_set():
            response.close()
            return
        for line in response.iter_lines():
            if line:
                yield line

    def close(self) -> None:
        self._closed.set()
        for closer in (getattr(self._response, "close", None), self._client.close):
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                pass


def _failure_reason(exc: BaseException) -> str:
    """A fixed reason code for a failed run. Never derived from exception text."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.HTTPError):
            return "transport"
    except Exception:
        pass
    if isinstance(exc, (OSError, ConnectionError)):
        return "transport"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "bad_stream"
    return "error"


class OwnerReflectionMixin:
    # ------------------------------------------------------------------
    # state (lazy: with the flag off nothing below ever creates anything)
    # ------------------------------------------------------------------
    def _owner_reflection_enabled(self) -> bool:
        return bool(_settings.OWNER_REFLECTION_ENABLED and _eng.MEMORIAS_ENABLED)

    def _owner_reflection_ring(self):
        with _INIT_LOCK:
            ring = getattr(self, "_owner_reflection", None)
            if ring is None:
                from opencohost.core.context.owner_reflection import JobControl, ReflectionRing

                ring = ReflectionRing()
                self._owner_reflection = ring
                self._owner_reflection_control = JobControl()
                self._owner_reflection_threads = []
                self._owner_reflection_last_skip = ""
                self._owner_reflection_next_check = 0.0
                self._owner_reflection_backoff_s = _RETRY_BACKOFF_S
                self._owner_reflection_rearm = False
                self._owner_reflection_last_fail = ""
            return ring

    def _owner_reflection_stamp_turn_done(self) -> None:
        """Called from ``_complete_processing_cycle``; a no-op with the flag off."""
        if self._owner_reflection_enabled():
            self._last_turn_done_monotonic = _monotonic()

    # ------------------------------------------------------------------
    # feed: the commit hook (owner gate only)
    # ------------------------------------------------------------------
    def _owner_reflection_submit(self, source, private, safe_context, dialogo) -> None:
        """Offer one committed exchange to the ring. Never raises."""
        try:
            if not self._owner_reflection_enabled():
                return
            clean = _eng.strip_history_wrapper(safe_context or "")
            self._owner_reflection_ring().offer(source, private, clean, dialogo or "")
        except Exception as exc:
            _eng.logger.debug("owner_reflection submit skipped: %s", type(exc).__name__)

    def _owner_reflection_reset(self) -> None:
        """Session reset / profile switch: cancel any job and forget the ring."""
        try:
            ring = getattr(self, "_owner_reflection", None)
            if ring is None:
                return
            self._owner_reflection_control.cancel()
            ring.reset()
            self._owner_reflection_rearm = False
        except Exception:
            pass

    # ------------------------------------------------------------------
    # arrival: yield the single Ollama runner
    # ------------------------------------------------------------------
    def note_owner_turn_arrival(self) -> None:
        """A turn is arriving: cancel the in-flight job, without blocking.

        Flags the job, bumps the generation (a late result is discarded) and closes the
        in-flight response from THIS thread. Inert when the flag is off or no job runs.
        """
        try:
            control = getattr(self, "_owner_reflection_control", None)
            if control is not None:
                control.cancel()
        except Exception:
            pass

    def _owner_reflection_turn_waiting(self) -> bool:
        """Something the engine will turn into work: covers the dispatcher's direct puts on
        ``command_queue``, which bypass every ``note_owner_turn_arrival`` hook."""
        try:
            if not self.command_queue.empty() or self._processing:
                return True
            # Pregen / agenda prefetch (llm_generating), playback and a held PTT all use
            # the single runner or the mic. The job itself sets none of them.
            if self.llm_generating or self.is_speaking or self._promotion_ptt_held:
                return True
            return bool(_eng._get_turn_scheduler(self).snapshot())
        except Exception:
            return True  # fail toward yielding the runner

    # ------------------------------------------------------------------
    # idle predicate and start (engine thread, idle branch of run())
    # ------------------------------------------------------------------
    def _owner_reflection_busy_reason(self, now: float) -> str:
        """First reason the engine is not idle, or "". Cheap, in-memory checks only."""
        if self.is_processing or self.llm_generating or self.is_speaking:
            return "busy"
        if self._promotion_ptt_held:
            return "busy"
        if not self.command_queue.empty():
            return "busy"
        if _eng._get_turn_scheduler(self).snapshot():
            return "busy"
        with self._accum_lock:
            if self._accumulation_buffer:
                return "busy"
        done = getattr(self, "_last_turn_done_monotonic", None)
        if done is not None and (now - done) < float(_settings.OWNER_REFLECTION_IDLE_S):
            return "busy"
        return ""

    def _owner_reflection_back_off(self, now: float) -> None:
        """Exponential back-off (30 s doubling to a 10 min cap) for the next probe."""
        wait = self._owner_reflection_backoff_s
        self._owner_reflection_next_check = now + wait
        self._owner_reflection_backoff_s = min(_BACKOFF_CAP_S, wait * 2)

    def _owner_reflection_apply_rearm(self, now: float) -> None:
        """Engine thread only: turn the worker's re-arm request into ``_promotion_swept =
        False``, but only when the engine is idle and no job holds the runner. The worker
        never touches the flag, so a sweep finishing on this thread cannot lose the re-arm."""
        if not self._owner_reflection_rearm:
            return
        if self._owner_reflection_control.active is not None or self._owner_reflection_busy_reason(now):
            return
        self._owner_reflection_rearm = False
        self._promotion_swept = False

    def _owner_reflection_skip(self, reason: str, now: float, ring) -> None:
        """Report a gate once per CHANGED reason and back off before the next probe."""
        self._owner_reflection_back_off(now)
        if reason == self._owner_reflection_last_skip:
            return
        self._owner_reflection_last_skip = reason
        from opencohost.core.context.owner_reflection import format_log_line

        _eng.logger.info(
            format_log_line("skipped", reason, considered=0, eligible=ring.pending_count())
        )

    def _owner_reflection_tick(self) -> None:
        """Start a run when one is due and the engine is idle, then hand a pending sweep
        re-arm to the engine thread. Never raises, never blocks longer than one local
        ``Client.ps()``."""
        try:
            if not self._owner_reflection_enabled():
                return
            ring = getattr(self, "_owner_reflection", None)
            if ring is None:
                return
            control = self._owner_reflection_control
            now = _monotonic()
            if control.active is None and not self._owner_reflection_try_start(ring, control, now):
                self._owner_reflection_apply_rearm(now)
        except Exception as exc:
            _eng.logger.debug("owner_reflection tick skipped: %s", type(exc).__name__)

    def _owner_reflection_try_start(self, ring, control, now: float) -> bool:
        """True when a job was started."""
        if not ring.due(_settings.OWNER_REFLECTION_EVERY_N):
            return False
        if now < self._owner_reflection_next_check or self._owner_reflection_busy_reason(now):
            return False
        batch, evicted = ring.pending_batch()
        if not batch:
            ring.reconcile()  # the counter was stale: nothing left to distil
            self._owner_reflection_next_check = now + _RETRY_BACKOFF_S
            return False
        reason = self._promotion_lifecycle_gate()
        if not reason:
            resident, failure = self._judge_model_residency()
            if not resident:
                reason = failure or "model_not_resident"
        if reason:
            self._owner_reflection_skip(reason, now, ring)
            return False
        token = control.begin()
        if token is None:
            return False
        self._owner_reflection_last_skip = ""
        model = self._judge_model()
        profile_id = self._current_profile_id
        worker = threading.Thread(
            target=self._owner_reflection_job,
            args=(token, batch, evicted, ring.pending_count(), model, profile_id),
            name="owner-reflection",
            daemon=True,
        )
        watcher = threading.Thread(
            target=self._owner_reflection_watch, args=(token,),
            name="owner-reflection-watch", daemon=True,
        )
        self._owner_reflection_threads = [worker, watcher]
        worker.start()
        watcher.start()
        return True

    def _owner_reflection_join(self, timeout: float = 5.0) -> None:
        """Wait for the worker and its watcher (tests and shutdown)."""
        for thread in list(getattr(self, "_owner_reflection_threads", ())):
            thread.join(timeout)

    def _owner_reflection_watch(self, token) -> None:
        """Cancel the job the moment work reaches the engine by a path with no hook."""
        control = self._owner_reflection_control
        while not token.done.wait(_WATCH_POLL_S):
            if token.cancel.is_set():
                return
            if self._owner_reflection_turn_waiting():
                control.cancel()
                return

    # ------------------------------------------------------------------
    # the job (worker thread)
    # ------------------------------------------------------------------
    def _owner_reflection_open_stream(self, *, model, messages, options, timeout_s):
        """Seam: a closable streaming handle on the local Ollama ``/api/chat``.

        Uses its own client whose HTTP timeout equals the job budget (the a8830bb lesson:
        a timeout longer than the budget keeps the runner busy). Cheap: nothing is sent
        until ``iter_lines``, so the handle can be registered for cancellation first.
        """
        import httpx

        from opencohost.core.vision.model_inventory import main_ollama_base_url

        normalized = (model or "").lower().replace("_", "-")
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "format": "json",
            "think": "low" if "gpt-oss" in normalized else False,
            "options": options,
            "keep_alive": _eng.LLM_KEEP_ALIVE,
        }
        client = httpx.Client(base_url=main_ollama_base_url(), timeout=httpx.Timeout(float(timeout_s)))
        return _OllamaStreamHandle(client, payload)

    def _owner_reflection_generate(self, token, batch, model) -> Optional[str]:
        """Stream the model's reply. ``None`` when cancelled."""
        from opencohost.core.context.owner_reflection import REPLY_MAX_CHARS, build_distillation_prompt

        control = self._owner_reflection_control
        budget = float(_settings.OWNER_REFLECTION_JOB_TIMEOUT_S)
        prompt = build_distillation_prompt(batch, _settings.OWNER_REFLECTION_MAX_FACTS)
        options = {"temperature": 0, "num_predict": 256}
        self._with_num_ctx(options, self._resolve_local_num_ctx(model))
        handle = self._owner_reflection_open_stream(
            model=model, messages=[{"role": "user", "content": prompt}],
            options=options, timeout_s=budget,
        )
        if not control.attach(token, handle):
            return None
        deadline = _monotonic() + budget
        parts: list = []
        size = 0
        final_chunk: Optional[dict] = None
        started = _monotonic()
        try:
            for line in handle.iter_lines():
                if token.cancel.is_set():
                    return None
                if _monotonic() > deadline:
                    raise TimeoutError("owner_reflection_budget")
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError("ollama_error")
                piece = (chunk.get("message") or {}).get("content") or ""
                parts.append(piece)
                size += len(piece)
                if chunk.get("done"):
                    final_chunk = chunk  # the only chunk carrying the counters
                if chunk.get("done") or size > REPLY_MAX_CHARS:
                    break
        except Exception:
            if token.cancel.is_set():
                return None  # the close we caused surfaces as a read error: not a failure
            raise
        finally:
            try:
                handle.close()
            except Exception:
                pass
        if token.cancel.is_set():
            return None
        try:
            from opencohost.core.usage import ledger

            ledger.record_response(
                final_chunk, model=model, is_local=True, kind="reflection",
                profile_id=getattr(self, "_current_profile_id", None) or "",
                wall_ms=int((_monotonic() - started) * 1000),
            )
        except Exception:
            pass  # best-effort: never break the job
        return "".join(parts)

    def _owner_reflection_write(self, token, candidates, batch, counters, profile_id) -> int:
        """Write each surviving fact through ``_capture_memoria``. Checks the token before
        every write; raises ``_Cancelled`` so nothing is written after a cancel."""
        from opencohost.core.context.owner_reflection import (
            ANSWER_MAX_CHARS, KIRA_LINES_WINDOW, fact_key_text, provenance_for, repeats_existing,
            repeats_spoken,
        )
        from opencohost.core.memory.memoria_store import build_signature, derive_stable_key

        control = self._owner_reflection_control
        ring = self._owner_reflection
        threshold = float(_settings.OWNER_REFLECTION_REPEAT_THRESHOLD)
        store = self._get_memoria_store()
        rows = store.list_for_profile(profile_id)
        by_key = {r["stable_key"]: r for r in rows}
        resolve_lang = getattr(self, "_resolve_turn_lang", None)
        turn_lang = resolve_lang() if callable(resolve_lang) else None
        # Recent facts: the 20 newest injection candidates plus the distilled drafts the
        # judge has not seen yet (they are not injectable but are still "recent facts").
        recent = list(store.list_injection_candidates(profile_id, lang=turn_lang)[:_REPEAT_FACT_WINDOW])
        recent += [
            r for r in rows
            if r["provenance"] and r["status"] == "draft" and not r["private"] and not r["inactive"]
        ][:_REPEAT_FACT_WINDOW]
        existing = [r["content"] for r in recent if r["content"]]
        kira_lines = ring.recent_kira_lines(KIRA_LINES_WINDOW)
        for cand in candidates:
            if not control.is_current(token):
                raise _Cancelled()
            text = f"{cand.question} {cand.answer}"
            stable_key = derive_stable_key(profile_id, fact_key_text(cand))
            if stable_key is None:
                counters["unkeyed"] += 1
                continue
            row = by_key.get(stable_key)
            if row is not None and (
                row["status"] != "draft"  # durable rows are upsert-immune: nothing to revise
                or repeats_existing(cand, [row["content"]], threshold)
            ):
                counters["repeated"] += 1
                continue
            # A collision with a draft that says something different is a correction: it is
            # revised in place by the normal upsert and re-judged.
            if repeats_existing(cand, existing, threshold) or repeats_spoken(cand, kira_lines, threshold):
                counters["repeated"] += 1
                continue
            ok = self._capture_memoria(
                profile_id, stable_key, cand.question, cand.answer[:ANSWER_MAX_CHARS],
                build_signature(text),
                provenance=provenance_for(cand, batch, ring.session_token),
                lang=turn_lang,
            )
            if ok:
                counters["written"] += 1
                existing.append(cand.answer)
        return counters["written"]

    def _owner_reflection_job(self, token, batch, evicted, eligible, model, profile_id) -> None:
        from opencohost.core.context.owner_reflection import (
            drop_ungrounded, format_log_line, parse_distillation,
        )

        started = _monotonic()
        control = self._owner_reflection_control
        ring = self._owner_reflection
        counters = {
            "considered": len(batch), "eligible": eligible, "distilled": 0, "repeated": 0,
            "schema_rejected": 0, "ungrounded": 0, "unkeyed": 0, "written": 0,
        }
        outcome, reason = "failed", "error"
        try:
            text = self._owner_reflection_generate(token, batch, model)
            if text is None or not control.is_current(token):
                raise _Cancelled()
            candidates, rejected = parse_distillation(
                text, len(batch), _settings.OWNER_REFLECTION_MAX_FACTS
            )
            counters["distilled"] = len(candidates)
            counters["schema_rejected"] = rejected
            # A fact cited only from questions or requests is not grounded: never written.
            candidates, counters["ungrounded"] = drop_ungrounded(candidates, batch)
            self._owner_reflection_write(token, candidates, batch, counters, profile_id)
            if not control.is_current(token):
                raise _Cancelled()
            ring.mark_distilled(batch[-1].seq, len(batch))
            outcome, reason = "ok", ("evicted" if evicted else "ok")
            self._owner_reflection_backoff_s = _RETRY_BACKOFF_S
        except _Cancelled:
            outcome, reason = "cancelled", "turn_arrival"
        except Exception as exc:
            outcome, reason = "failed", _failure_reason(exc)
            self._owner_reflection_back_off(_monotonic())
        finally:
            if counters["written"]:
                # Distilled drafts are unjudged and never injected until the judge keeps them.
                # Ask the ENGINE thread to re-arm the once-per-launch sweep (ADR-065 section
                # 7); it does so only when idle. Done even for a cancelled run that already
                # wrote something.
                self._owner_reflection_rearm = True
            control.finish(token)
            try:
                if outcome == "failed":
                    quiet = reason == self._owner_reflection_last_fail  # one line per failure reason
                    self._owner_reflection_last_fail = reason
                else:
                    quiet = False
                    self._owner_reflection_last_fail = ""
                if not quiet:
                    _eng.logger.info(format_log_line(
                        outcome, reason, elapsed_ms=int((_monotonic() - started) * 1000), **counters
                    ))
            except Exception:
                pass
