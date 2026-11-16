"""Owner-speech hook for the PTT dispatch (vision-and-prompt-leaks T4).

``PttController`` dispatches the transcript as ``process_context`` with
``source="ptt"``. This proxy sits between it and the real ``Dispatcher`` (wired
in ``create_app``, outside ``ptt_session.py``), removes Whisper silence
hallucinations from the owner's words and drops the turn when nothing is left.

The raw transcript is recovered from ``history_text`` (the honest
"El streamer dijo (PTT): ..." wrapper). Without it the proxy fails open and
forwards the turn untouched. Logs carry codes and counts only.
"""

from __future__ import annotations

import logging

from opencohost.api.dispatch import DispatchResult
from opencohost.core.memory.memoria_store import strip_history_wrapper
from opencohost.i18n import active as i18n_active
from opencohost.stt.hallucination_filter import filter_owner_speech

logger = logging.getLogger("OpenCohost")

STATE_FILTERED = "filtered"


class OwnerSpeechFilterDispatcher:
    """Delegating dispatcher that filters PTT transcripts."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def dispatch(self, command, payload, *args, history_text=None, source=None, **kwargs):
        if command == "process_context" and source == "ptt" and isinstance(history_text, str):
            try:
                raw = strip_history_wrapper(history_text)
                result = filter_owner_speech(raw)
                if result.removed:
                    logger.info(
                        "[STT_HALLUCINATION] source=%s action=%s removed=%d chars=%d",
                        source,
                        "drop" if result.dropped else "strip",
                        result.removed,
                        len(raw),
                    )
                    if result.dropped:
                        return DispatchResult(state=STATE_FILTERED)
                    payload = i18n_active.ptt_wrapper().format(text=result.text)
                    history_text = i18n_active.ptt_history_wrapper().format(text=result.text)
            except Exception:
                logger.exception("Owner speech filter failed; forwarding the turn untouched")
        return self._inner.dispatch(
            command, payload, *args, history_text=history_text, source=source, **kwargs
        )
