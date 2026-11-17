"""Engine-side glue for the decision layer (laya-decision-layer-20261009).

``_turn_decisions`` runs one ``DecisionBus.decide()`` per owner turn, at the
top of ``_generar_dialogo`` and before the vision look. The result is cached
for one payload so the cloud->local retry (same call) and a foreground turn
that follows a pregen for the same payload reuse it instead of asking Laya
again. ``None`` (no backend, nothing active, any failure) means the base
path, and callers then pass nothing new to their seams.
"""
from __future__ import annotations

import time
from datetime import datetime

from opencohost.core.context import clock
from opencohost.core.decision.consumers import apply_clock_decision
from opencohost.core.decision import runtime as decision_runtime
from opencohost.core.decision.questions import SOURCES
from opencohost.core.decision.types import CLOCK, MEMORY, REPLY_LENGTH, VISION, WEB

# A pregen and its foreground turn for the same payload land within seconds.
DECISION_CACHE_TTL_S = 30.0
_monotonic = time.monotonic


class DecisionTurnMixin:
    """All state lives on the motor."""

    _decision_cache = None  # ((source, text, ids), TurnDecisions, at)
    # clock-and-compact-polish-20261010: injectable local clock.
    _clock_now = staticmethod(datetime.now)

    def _clock_kwargs(self, source: str, text, decisions) -> dict:
        """``{"clock_now": now}`` when this owner turn's ``clock`` decision
        says ``time`` (the rule, or a confident Laya in ``on``), else ``{}``
        so a base turn calls the assembler exactly as before. Never raises."""
        if source not in clock.CLOCK_SOURCES:
            return {}
        try:
            if apply_clock_decision(decisions, clock.clock_rule(text)) != clock.TIME:
                return {}
            return {"clock_now": self._clock_now()}
        except Exception:
            return {}

    def _turn_decisions(self, contexto, source: str, *, allow_vision: bool):
        """This turn's ``TurnDecisions`` or None. Never raises; never logs text."""
        try:
            bus = decision_runtime.get_bus()
            if bus.backend is None:
                return None
            ids = [MEMORY, REPLY_LENGTH]
            if allow_vision and self._vision_enabled():
                ids.insert(0, VISION)
            # web-fetch-laya-20261009: asked only while the owner's switch is on.
            web_enabled = getattr(self, "_web_enabled", None)
            if callable(web_enabled) and web_enabled():
                ids.append(WEB)
            text = contexto if isinstance(contexto, str) else str(contexto or "")
            # clock-and-compact-polish-20261010: the clock question joins this
            # same single call, and only when a loose time/date word is present,
            # so a plain turn asks nothing more.
            if clock.may_ask_time(self._decision_state(text)):
                ids.append(CLOCK)
            ids = tuple(d for d in ids if source in SOURCES[d])
            if not ids:
                return None
            key = (source, text, ids)
            now = _monotonic()
            cached = self._decision_cache
            if cached is not None and cached[0] == key and now - cached[2] <= DECISION_CACHE_TTL_S:
                return cached[1]
            turn = bus.decide(self._decision_state(text), source, ids=ids)
            self._decision_cache = (key, turn, now) if turn is not None else None
            return turn
        except Exception:
            return None

    @staticmethod
    def _decision_state(text: str) -> str:
        """The clean user text Laya reads (the PTT/history wrapper stripped)."""
        try:
            from opencohost.core.memory.memoria_store import strip_history_wrapper

            return strip_history_wrapper(text) or text
        except Exception:
            return text


def decision_kwargs(turn) -> dict:
    """``{"decisions": turn}``, or ``{}`` so a base turn calls every seam
    exactly as before."""
    return {"decisions": turn} if turn is not None else {}

