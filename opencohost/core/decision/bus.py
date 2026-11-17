"""DecisionBus: one Laya call per turn, rule fallback per decision
(laya-decision-layer-20261009).

``decide()`` asks every decision whose mode is not ``off`` and whose source
applies, all in ONE backend call (one forward pass). It returns a
``TurnDecisions`` holding Laya's raw answers, or ``None`` when nothing was
asked or no backend is ready, which callers treat as "base behavior".

The rule value is only known at the consumer (the vision gate, memory recall,
the reply-length heuristic), so each consumer calls
``TurnDecisions.resolve(id, rule_value, allowed=...)``. That applies the mode
and threshold, logs one ``[DECISION]`` line and caches the outcome, so a
cloud->local retry or a pregen reuse resolves (and logs) once.

Privacy: the state is handed to the backend and never logged or kept.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Optional, Protocol, runtime_checkable

from opencohost.config import decisions as decision_settings
from opencohost.config import settings
from opencohost.core.decision.questions import QUESTIONS, SOURCES, VALUES
from opencohost.core.decision.types import (
    ACTED_BY_LAYA,
    ACTED_BY_RULE,
    DECISION_IDS,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    STATUS_ERROR,
    STATUS_LOW_CONFIDENCE,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    DecisionOutcome,
)

logger = logging.getLogger("OpenCohost")


@runtime_checkable
class DecisionBackend(Protocol):
    """Port: answer a set of questions about one state in one call.

    Returns the worker response (``{"ok", "answers": {qid: {...}}, ...}``) or
    ``None`` on timeout. ``available()`` is optional; a backend without it is
    treated as always available.
    """

    def ask(self, state: str, questions: dict, timeout_s: float) -> Optional[dict]: ...


class _Answer:
    __slots__ = ("mode", "threshold", "value", "confidence", "status", "reason", "latency_ms")

    def __init__(self, mode, threshold, value, confidence, status, reason, latency_ms):
        self.mode = mode
        self.threshold = threshold
        self.value = value
        self.confidence = confidence
        self.status = status
        self.reason = reason
        self.latency_ms = latency_ms


def _confidence(answer: dict) -> Optional[float]:
    """``answer_confidence``, else ``confidence``, else the top probability."""
    for key in ("answer_confidence", "confidence"):
        value = answer.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return min(1.0, max(0.0, float(value)))
    probs = answer.get("probabilities")
    if isinstance(probs, dict):
        nums = [float(v) for v in probs.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums:
            return min(1.0, max(0.0, max(nums)))
    return None


def _parse(decision_id: str, response, mode: str, threshold: float, latency_ms: int) -> _Answer:
    if response is None:
        return _Answer(mode, threshold, None, None, STATUS_TIMEOUT, "", latency_ms)
    if not isinstance(response, dict) or not response.get("ok"):
        reason = response.get("error") if isinstance(response, dict) else None
        return _Answer(mode, threshold, None, None, STATUS_ERROR, _code(reason) or "bad_response", latency_ms)
    answers = response.get("answers")
    answer = answers.get(decision_id) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        return _Answer(mode, threshold, None, None, STATUS_ERROR, "no_answer", latency_ms)
    choice = answer.get("choice")
    if choice not in VALUES[decision_id]:
        return _Answer(mode, threshold, None, None, STATUS_ERROR, "bad_choice", latency_ms)
    confidence = _confidence(answer)
    status = STATUS_OK if confidence is not None and confidence >= threshold else STATUS_LOW_CONFIDENCE
    return _Answer(mode, threshold, choice, confidence, status, "", latency_ms)


def _code(value) -> str:
    """A short, log-safe code (letters, digits, ``_``), or ""."""
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isalnum() or c == "_")[:40]


def _record_usage(outcome: DecisionOutcome) -> None:
    """Count this outcome in the usage ledger (enum labels only; never raises)."""
    try:
        from opencohost.core.usage import ledger

        ledger.record_decision(outcome.id, outcome.mode, outcome.acted_by, outcome.status)
    except Exception:
        logger.debug("usage ledger decision hook failed", exc_info=True)


class TurnDecisions:
    """Laya's answers for one turn; resolves each decision against its rule."""

    def __init__(self, answers: dict[str, _Answer]) -> None:
        self._answers = answers
        self._resolved: dict[str, DecisionOutcome] = {}
        self._lock = threading.Lock()

    def asked(self, decision_id: str) -> bool:
        return decision_id in self._answers

    def resolve(
        self, decision_id: str, rule_value: str, *, allowed: Optional[Iterable[str]] = None
    ) -> Optional[DecisionOutcome]:
        """The outcome for ``decision_id`` given the rule's value; None when
        the decision was not asked this turn. ``allowed`` limits the values
        Laya may switch to (None = any). Logs once; later calls return the
        cached outcome. Never raises."""
        with self._lock:
            cached = self._resolved.get(decision_id)
            if cached is not None:
                return cached
            answer = self._answers.get(decision_id)
            if answer is None:
                return None
            outcome = self._outcome(decision_id, answer, rule_value, allowed)
            self._resolved[decision_id] = outcome
        _log(outcome)
        _record_usage(outcome)
        return outcome

    @staticmethod
    def _outcome(decision_id, answer: _Answer, rule_value, allowed) -> DecisionOutcome:
        status, reason, acted_by = answer.status, answer.reason, ACTED_BY_RULE
        if status == STATUS_OK and answer.value != rule_value:
            permitted = allowed is None or answer.value in tuple(allowed)
            if not permitted:
                status, reason = STATUS_SKIPPED, "not_allowed"
            elif answer.mode == MODE_ON:
                acted_by = ACTED_BY_LAYA
        return DecisionOutcome(
            id=decision_id,
            mode=answer.mode,
            rule_value=rule_value,
            laya_value=answer.value,
            confidence=answer.confidence,
            acted_by=acted_by,
            latency_ms=answer.latency_ms,
            status=status,
            reason=reason,
        )

    def outcomes(self) -> list[dict]:
        """Resolved outcomes as dicts, in decision order (prompt trace)."""
        with self._lock:
            return [self._resolved[d].to_dict() for d in DECISION_IDS if d in self._resolved]


def _log(outcome: DecisionOutcome) -> None:
    conf = "-" if outcome.confidence is None else f"{outcome.confidence:.2f}"
    logger.info(
        "[DECISION] id=%s mode=%s rule=%s laya=%s conf=%s acted_by=%s latency_ms=%d status=%s reason=%s",
        outcome.id, outcome.mode, outcome.rule_value or "-", outcome.laya_value or "-", conf,
        outcome.acted_by, outcome.latency_ms, outcome.status, outcome.reason or "-",
    )


class DecisionBus:
    """Asks the active decisions in one backend call; fail-open to the rules."""

    def __init__(
        self,
        backend: Optional[DecisionBackend] = None,
        *,
        budget_ms: Optional[Callable[[], int]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self._budget_ms = budget_ms or (lambda: int(settings.DECISION_BUDGET_MS))
        self._clock = clock
        self._unavailable_logged = False

    def _active(self, source: str, ids: Optional[Iterable[str]]) -> dict[str, tuple[str, float]]:
        wanted = DECISION_IDS if ids is None else tuple(ids)
        snapshot = decision_settings.get_decision_settings()
        active = {}
        for decision_id in DECISION_IDS:
            entry = snapshot.get(decision_id)
            if decision_id not in wanted or entry is None or source not in SOURCES[decision_id]:
                continue
            mode = entry["mode"]
            if mode == MODE_OFF:
                continue
            if mode == MODE_ON and decision_id in settings.DECISION_SHADOW_ONLY:
                mode = MODE_SHADOW
            active[decision_id] = (mode, float(entry["threshold"]))
        return active

    def _ready(self) -> bool:
        backend = self.backend
        if backend is None:
            return False
        available = getattr(backend, "available", None)
        try:
            return bool(available()) if callable(available) else True
        except Exception:
            return False

    def decide(
        self, state: str, source: str, *, ids: Optional[Iterable[str]] = None
    ) -> Optional[TurnDecisions]:
        """Laya's answers for this turn, or None (nothing asked / no backend).
        Never raises; never logs the state."""
        try:
            active = self._active(source, ids)
            if not active:
                return None
            if not self._ready():
                if not self._unavailable_logged:
                    self._unavailable_logged = True
                    reason = "unconfigured" if self.backend is None else "not_ready"
                    logger.info("[DECISION] backend unavailable reason=%s; rules act", reason)
                return None
            questions = {d: QUESTIONS[d] for d in active}
            started = self._clock()
            try:
                response = self.backend.ask(
                    state if isinstance(state, str) else str(state or ""),
                    questions,
                    max(0.0, self._budget_ms() / 1000.0),
                )
            except Exception as exc:
                response = {"ok": False, "error": f"backend_{type(exc).__name__}"}
            latency_ms = int((self._clock() - started) * 1000)
            return TurnDecisions(
                {d: _parse(d, response, mode, threshold, latency_ms) for d, (mode, threshold) in active.items()}
            )
        except Exception:
            logger.debug("decision bus failed", exc_info=True)
            return None
