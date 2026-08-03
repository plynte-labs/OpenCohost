"""Per-request context telemetry ring, moved verbatim out of llm_engine.py
(Phase C3, refactor_core_api_20260802/proposal.md). Pure leaf over stdlib —
no settings/llm_engine import, so MotorVocalIA owns sizing (CTX_TELEMETRY_RING_MAXLEN)
and passes it in; this module only knows about the deque + snapshot shape.

Unit 2.3 (runtime_findings_batch_20260731 F10) origin: bounded per-request
context telemetry, appended once per completed generation whose
ctx_utilization line actually logs (see MotorVocalIA._generar_dialogo).
"""
from collections import deque
from typing import Optional


class CtxTelemetryRing:
    """Bounded ring of ctx-telemetry entries + a read-only snapshot accessor."""

    def __init__(self, maxlen: int) -> None:
        self._ring: "deque[dict]" = deque(maxlen=maxlen)

    def append(self, entry: dict) -> None:
        self._ring.append(entry)

    def snapshot(self, sources: Optional[tuple] = None) -> dict:
        """Read-only view of the ring for API/status consumers (unit 2.5).

        "latest" is honestly defined as the most recently APPENDED entry
        regardless of source -- a background pregen generation can be
        "latest" even while a different (previous) turn is still being
        spoken. Pass `sources` (e.g. ("direct", "kira-agenda")) to restrict
        "latest" to entries whose `source` matches, letting a caller pick the
        latest FOREGROUND entry instead. `ring` is always the full unfiltered
        history, oldest first, so a caller wanting a filtered ring can filter
        the list itself.

        Returns ``{"latest": <entry dict or None>, "ring": [<entry dict>, ...]}``.
        """
        ring = list(self._ring)
        latest = None
        candidates = ring
        if sources is not None:
            candidates = [entry for entry in ring if entry.get("source") in sources]
        if candidates:
            latest = candidates[-1]
        return {"latest": latest, "ring": ring}
