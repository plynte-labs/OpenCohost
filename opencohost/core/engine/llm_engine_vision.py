"""
opencohost/core/engine/llm_engine_vision.py

Vision-on-demand seam of MotorVocalIA (V1.5): runs the optional
VisionTurnStep for owner turns before generation and speaks its bridge line
through the speech router. The step itself and its lifecycle are owned by
EngineHost (`opencohost/core/vision/controller.py`); this mixin only reads
`self._vision.step`, so a motor without vision (CTk, `__new__`-built test
doubles) keeps the prompt byte-identical.

V1.13:
- Interactive pregen owner turns (PTT/direct queued while Kira is still
  speaking) look too: `_generar_dialogo(allow_vision=...)` replaces the old
  `commit_history` gate. A pregen look is stashed for its (source, payload)
  so a discarded draft regenerated for the same payload (retrigger loop or
  the foreground fallback) reuses it once instead of looking twice. The stash
  is dropped when the draft is consumed and expires after
  `VISION_PREGEN_LOOK_TTL_S`.
- After a successful look whose bridge was really spoken, a short status line
  goes out on the owner band; it is retired like the bridge when the reply is
  ready and it never started.

V1.15 (owner runs 2026-10-02: under GPU load every look timed out and the LLM
still spent 76-97 s on a long reply):
- A REQUESTED look (armed button/command, or a screen reference) that was
  attempted and failed skips generation. The turn's reply is a short canned
  line (es/en, by outcome) committed to history like any reply and spoken
  once, by whoever speaks the reply, with a ``-vision-fail`` source tag.
- A failed automatic PTT look still generates, with a "keep it short"
  instruction appended to the failure note.
- Any error while building the shortcut falls back to normal generation.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import NamedTuple, Optional

from opencohost.core.speech.sentence_splitter import SentenceSplitter
from opencohost.core.vision import direct_cloud
from opencohost.core.vision.gate import is_requested_look, look_request_reasons
from opencohost.core.vision.turn_step import (
    OUTCOME_MODEL_WARMING,
    VISION_FAILURE_SHORT_HINT,
    detect_language,
    failure_line,
)

logger = logging.getLogger("OpenCohost")

VISION_TURN_SOURCES = frozenset({"direct", "ptt"})
# The bridge tag keeps the turn's source as its PREFIX so emergency stop and
# the owner sweeps (prefix matches on "ptt"/"direct") still reach it, while
# the exact tag stays distinguishable from the reply in logs.
VISION_BRIDGE_SUFFIX = "-vision-bridge"
# Sibling tag for the "frames seen, reply coming" status line (V1.13): same
# owner-source prefix, so emergency stop and the owner sweeps reach it too.
VISION_STATUS_SUFFIX = "-vision-status"
# V1.28: progress lines ("Sigo leyendo...") every few seconds while a describe
# runs long. Same owner-source prefix as the bridge/status, so emergency stop
# and the owner sweeps reach it.
VISION_PROGRESS_SUFFIX = "-vision-progress"
# V1.32: the transcription read aloud in text mode. Same owner-source prefix, so
# emergency stop and the owner sweeps reach it; the router job is ``protected``
# so the reaction to it queues behind it instead of preempting it.
VISION_READ_SUFFIX = "-vision-read"
# V1.32: trusted, code-built system text for the reaction turn.
VISION_READ_ALOUD_NOTE = (
    "READ ALOUD: The text on the owner's screen was already read aloud to them, word for word, "
    "just before your reply. Never repeat, quote, summarize or re-transcribe it. "
    "Give only a short reaction (one or two sentences), in character, to what it says or means."
)
# V1.15: tag of the canned failure reply. Same owner-source prefix, so
# emergency stop and the owner sweeps reach it; spoken on the owner band.
VISION_FAIL_SUFFIX = "-vision-fail"
# A stashed pregen look older than this is not reused (the screen moved on).
VISION_PREGEN_LOOK_TTL_S = 60.0


class VisionLook(NamedTuple):
    """One turn's look as the generation funnel needs it.

    ``fail_line`` is non-empty only for a requested look that was attempted
    and failed: the turn then skips the LLM and replies with it.
    """

    block: str = ""
    note: str = ""
    fail_outcome: str = ""
    fail_line: str = ""
    # V1.32: non-empty only when the transcription was read aloud this turn.
    read_note: str = ""
    # P2: (bytes, mime) frames of a DIRECT look, sent to the cloud chat model as
    # image parts. Empty on every other path.
    images: tuple = ()


NO_LOOK = VisionLook()

_monotonic = time.monotonic

# vision-off-quick-enable-20261009: module-level because the pregen worker and
# the engine thread may notify the same off request at once.
_OFF_REQUEST_LOCK = threading.Lock()


class VisionTurnMixin:
    """Engine-side glue for the VisionTurnStep. All state lives on the motor."""

    # Class-level defaults so motors built without __init__ never raise here.
    _vision = None
    _vision_bridge_job = None
    _vision_bridge_source = "direct"
    _vision_status_job = None
    _vision_progress_job = None
    # V1.36: True while a pregen look (stash) runs its step. The step's bridge
    # and progress callbacks (the latter from its ticker thread) check it, so a
    # background look never speaks over the turn Kira is currently voicing.
    _vision_look_silent = False
    _vision_read_seq = 0  # V1.33: turn tokens for reading jobs (router same-turn check)
    _vision_read_turn_id: Optional[int] = None  # token of THIS turn's reading, if any
    _vision_pregen_look = None  # ((source, payload), VisionLook, at)
    # vision-off-quick-enable-20261009: host callback (no args) fired when the
    # owner asks to look while vision is off; the last fired (key, at) dedupes
    # the pregen and main-turn calls for one request.
    on_vision_off_request = None
    _vision_off_request_last = None
    # P2: pasted images waiting for their turn (see ``stash_turn_images``).
    _turn_images = None

    def stash_turn_images(self, text: str, images) -> None:
        """Hold the pasted *images* for the owner turn whose text is *text*."""
        if self._turn_images is None:
            self._turn_images = direct_cloud.TurnImageStash()
        self._turn_images.put(text, images)

    def peek_turn_images(self, text) -> list:
        stash = self._turn_images
        return stash.peek(text) if stash is not None and isinstance(text, str) else []

    def take_turn_images(self, text) -> list:
        stash = self._turn_images
        return stash.take(text) if stash is not None and isinstance(text, str) else []

    def _vision_direct_eligible(self) -> bool:
        """P2: True when images go straight to the cloud chat model: the active
        provider is cloud (and not in local fallback), its model sees images, the
        owner opted in and vision is on. Logs one fixed-literal route line."""
        try:
            from opencohost.config import settings

            cfg = self._provider_config
            decision = direct_cloud.decide_route(
                provider_is_cloud=not (self._cfg_is_local(cfg) or self._cloud_fallback_active),
                model_vision=direct_cloud.profile_model_vision(cfg),
                opted_in=getattr(settings, "VISION_CLOUD_DIRECT_ALLOWED", False) is True,
                vision_enabled=getattr(settings, "VISION_ENABLED", False) is True,
            )
        except Exception:
            decision = direct_cloud.RouteDecision(False, "check_failed")
        direct_cloud.log_route(decision)
        return decision.direct

    def _vision_chat_model_ineligible(self) -> Optional[str]:
        """A3: the stable failure code when the owner chose ``chat_model`` as the
        vision source but the active provider/model cannot see images; else None.
        Config-based on purpose (a temporary local fallback is not an ineligible
        model). Never raises."""
        try:
            from opencohost.config import settings

            if getattr(settings, "VISION_SOURCE", "local") != "chat_model":
                return None
            return direct_cloud.chat_model_eligibility(self._provider_config)[1]
        except Exception:
            return None

    def _vision_enabled(self) -> bool:
        """True while the vision controller is not off (V1.24 capability line)."""
        try:
            return getattr(getattr(self, "_vision", None), "enabled", False) is True
        except Exception:
            return False

    def _run_vision_for_turn(self, contexto, source: str, *, stash: bool = False) -> tuple[str, str]:
        """``(screen_block, vision_note)`` view of ``_run_vision_look``."""
        look = self._run_vision_look(contexto, source, stash=stash)
        return look.block, look.note

    def _run_vision_look(
        self, contexto, source: str, *, stash: bool = False, decisions=None
    ) -> VisionLook:
        """This owner turn's ``VisionLook`` (never raises).

        At most one is non-empty: the block on a successful look, the note
        when a look was attempted (bridge spoken) but failed (V1.8), so Kira
        says she could not look instead of "you showed me nothing".

        Runs on the calling generation thread (engine or interactive pregen),
        strictly before the LLM call: V0.2 showed a vision call overlapping
        voice generation drops Qwen's speed ~62-80%. ``stash`` (pregen calls)
        keeps an attempted look for one reuse by the same payload.
        ``decisions`` (laya-decision-layer-20261009) reaches the step only when
        not None, so a base turn calls ``step.run`` exactly as before.
        """
        if source not in VISION_TURN_SOURCES:
            return NO_LOOK
        vision = getattr(self, "_vision", None)
        step = getattr(vision, "step", None) if vision is not None else None
        if step is None:
            self._note_vision_off_request(contexto, source)
            return NO_LOOK
        text = contexto if isinstance(contexto, str) else str(contexto or "")
        key = (source, text)
        reused = self._take_vision_pregen_look(key)
        if reused is not None:
            return reused
        if source == "ptt":
            try:
                from opencohost.core.memory.memoria_store import strip_history_wrapper

                text = strip_history_wrapper(text) or text
            except Exception:
                pass
        self._vision_bridge_source = source
        self._vision_bridge_job = None
        status_line = ""
        self._vision_look_silent = bool(stash)
        try:
            if decisions is not None:
                result = step.run(text, source, decisions=decisions)
            else:
                result = step.run(text, source)
            block = result.screen_block or ""
            note = "" if block else (getattr(result, "note", "") or "")
            if block and self._vision_bridge_started():
                status_line = getattr(result, "status_line", "") or ""
        except Exception as exc:  # belt: the step itself never raises
            logger.info("[VISION] decision=look reasons=error_%s outcome=error", type(exc).__name__)
            return NO_LOOK
        finally:
            self._vision_look_silent = False
            self._retire_vision_bridge()
            self._retire_vision_progress()
        # V1.32: text mode reads the transcription aloud. It starts only after
        # the progress line is retired (the ticker already stopped with the
        # describe) and replaces the "got it" status line; the chat LLM still
        # runs right after this returns, with the "already read" note.
        read_note = ""
        transcription = getattr(result, "transcription", "")
        # V1.35: only a foreground look reads. A pregen look runs in the
        # background while Kira may still be speaking the previous turn, and a
        # reading submitted from there would outlive a discarded draft. A
        # pregen look therefore carries no read note, and a foreground turn
        # that reuses it answers like V1.28 (screen block, no reading).
        if not stash and block and isinstance(transcription, str) and transcription:
            if self._vision_read_aloud(transcription, source):
                read_note = VISION_READ_ALOUD_NOTE
                status_line = ""
        # V1.36: a pregen look speaks nothing, status line included.
        if status_line and not stash:
            self._vision_speak_status(status_line)
        images = tuple(getattr(result, "images", ()) or ()) if block else ()
        look = VisionLook(block, note, read_note=read_note, images=images)
        # V1.23: a look that never started because the model was still warming
        # carries no note, but a requested one still takes the fail shortcut.
        if note or (not block and getattr(result, "outcome", "") == OUTCOME_MODEL_WARMING):
            look = self._vision_failure_look(result, text, step, note)
        if stash and (block or note or look.fail_line):
            self._vision_pregen_look = (key, look, _monotonic())
        return look

    def _note_vision_off_request(self, contexto, source: str) -> None:
        """Vision is off (or never wired): when this owner turn asks to look,
        log one ``[VISION] decision=off_request`` line and fire
        ``on_vision_off_request`` once per request (vision-off-quick-enable).

        The pregen draft and the main turn both reach here for the same
        (source, text); a repeat within ``VISION_PREGEN_LOOK_TTL_S`` is the same
        request. Logs reason codes only, never the text. Never raises.
        """
        try:
            text = contexto if isinstance(contexto, str) else str(contexto or "")
            # Shared with the prompt's vision_off section (no drift).
            reasons = look_request_reasons(text, source)
            if not reasons:
                return
            key = (source, text)
            now = _monotonic()
            with _OFF_REQUEST_LOCK:
                last = self._vision_off_request_last
                if last is not None and last[0] == key and now - last[1] <= VISION_PREGEN_LOOK_TTL_S:
                    return
                self._vision_off_request_last = (key, now)
            logger.info("[VISION] decision=off_request source=%s reasons=%s", source, reasons)
            callback = getattr(self, "on_vision_off_request", None)
            if callable(callback):
                callback()
        except Exception:
            logger.debug("[VISION] off_request notify failed", exc_info=False)

    @staticmethod
    def _vision_failure_look(result, text: str, step, note: str) -> VisionLook:
        """Classify an attempted look that failed (V1.15). Never raises.

        Requested look: a canned line in the question's language replaces the
        reply. Automatic PTT look: the note gains a keep-it-short instruction.
        """
        try:
            outcome = getattr(result, "outcome", "") or ""
            if is_requested_look(getattr(result, "reason_codes", ()), text):
                lang = detect_language(text, default=getattr(step, "default_language", "es"))
                line = failure_line(outcome, lang)
                return VisionLook("", note, outcome if line else "", line)
            if not note:
                return NO_LOOK  # automatic look that never started: nothing to say
            return VisionLook("", f"{note} {VISION_FAILURE_SHORT_HINT}")
        except Exception:
            logger.debug("[VISION] failure classification failed", exc_info=False)
            return VisionLook("", note)

    def _vision_fail_reply(
        self, look: VisionLook, contexto, source: str, *,
        commit_history: bool, history_text: Optional[str] = None,
    ):
        """The turn's reply for a failed requested look, or None to fall back
        to normal generation (any error here must not lose the turn).

        Committed to history like a reply when the caller commits; never
        spoken here: the caller that speaks replies (foreground turn or
        pregen consume) speaks it once, under ``speak_source``.
        """
        try:
            from opencohost.core.engine.generation_orchestrator import GenerationResult

            line = str(look.fail_line)
            reply = GenerationResult(
                line, answered_by_provider="local", answered_by_transport="local"
            )
            reply.speak_source = f"{source}{VISION_FAIL_SUFFIX}"
            if commit_history:
                self._commit_history(contexto, line, source=source, history_text=history_text)
        except Exception:
            logger.debug("[VISION] fail shortcut failed; generating instead", exc_info=False)
            return None
        logger.info("[VISION] fail_shortcut outcome=%s skipped_llm=true", look.fail_outcome)
        return reply

    def _vision_cloud_failure_reply(
        self, contexto, source: str, *, commit_history: bool, history_text: Optional[str] = None,
        outcome: str = "cloud_unavailable",
    ):
        """A2: the canned reply for a look-carrying cloud turn that failed in
        manual fallback mode, in the owner's language. None on any error.
        B2: ``outcome="cloud_quota_exhausted"`` picks the out-of-credits line."""
        try:
            text = contexto if isinstance(contexto, str) else str(contexto or "")
            vision = getattr(self, "_vision", None)
            step = getattr(vision, "step", None)
            lang = detect_language(text, default=getattr(step, "default_language", "es"))
            line = failure_line(outcome, lang)
            if not line:
                return None
            return self._vision_fail_reply(
                VisionLook("", "", outcome, line), contexto, source,
                commit_history=commit_history, history_text=history_text,
            )
        except Exception:
            logger.debug("[VISION] cloud failure line failed", exc_info=False)
            return None

    def _take_vision_pregen_look(self, key) -> Optional[VisionLook]:
        """Consume the stashed pregen look for ``key`` (once), if still fresh."""
        stashed = self._vision_pregen_look
        if stashed is None or stashed[0] != key:
            return None
        self._vision_pregen_look = None
        if _monotonic() - stashed[2] > VISION_PREGEN_LOOK_TTL_S:
            return None
        return stashed[1]

    def _drop_vision_pregen_look(self) -> None:
        """The pregen draft carrying the look was consumed: nothing to reuse."""
        self._vision_pregen_look = None

    def _vision_bridge_started(self) -> bool:
        """True when the bridge line actually reached the speaker."""
        job = self._vision_bridge_job
        if job is None:
            return False
        from opencohost.core.speech.router import SpeechJobState

        return getattr(job, "state", None) in (SpeechJobState.ACTIVE, SpeechJobState.FINISHED)

    def _vision_speak_bridge(self, line: str) -> None:
        """Speak the bridge line now, on the owner band. Router only: on the
        legacy path a blocking `_hablar` would stall the turn, so it is skipped."""
        if not getattr(self, "_speech_router_enabled", False) or self._vision_look_silent:
            return
        from opencohost.core.speech.router import PRIORITY_OWNER

        source = f"{self._vision_bridge_source}{VISION_BRIDGE_SUFFIX}"
        self._vision_bridge_job = self._ensure_router().submit(
            line, source, PRIORITY_OWNER, releases_arrival_hold=False
        )

    def _vision_speak_status(self, line: str) -> None:
        """Speak the post-describe status line on the owner band (router only)."""
        if not getattr(self, "_speech_router_enabled", False):
            return
        from opencohost.core.speech.router import PRIORITY_OWNER

        source = f"{self._vision_bridge_source}{VISION_STATUS_SUFFIX}"
        try:
            self._vision_status_job = self._ensure_router().submit(
                line, source, PRIORITY_OWNER, releases_arrival_hold=False
            )
        except Exception:
            logger.debug("[VISION] status line submit failed", exc_info=False)

    def _vision_read_aloud(self, transcription: str, source: str) -> bool:
        """Speak the text-mode transcription on the owner band (V1.32).

        Each sentence goes through the same stages as a streamed reply: TTS
        sanitization, then the per-sentence output guard, then TTS fragmenting
        (``handle_stream_sentence``). A blocked sentence is dropped, never
        spoken. One sealed, ``protected`` router job carries the fragments, so
        the reaction (submitted later on the owner band) plays after it and
        cancel / PTT hold / pause act on it like any owner speech. Returns True
        only when something was queued. Never raises. Logs counts only.
        """
        if not transcription or not getattr(self, "_speech_router_enabled", False):
            return False
        job = None
        try:
            from opencohost.config import settings

            if not getattr(settings, "VISION_READ_ALOUD", False):
                return False
            read_source = f"{source}{VISION_READ_SUFFIX}"
            if self._speech_cancelled(read_source):
                return False
            from opencohost.config.validation import redact_block_preview
            from opencohost.core.engine.generation_orchestrator import (
                _output_guard_with_tts_check,
            )
            from opencohost.core.speech.router import PRIORITY_OWNER

            splitter = SentenceSplitter()
            sentences = splitter.feed(transcription) + splitter.flush()
            chunks: list = []
            spoken = blocked = 0
            for sentence in sentences:
                pieces = [p for p in self._sanitize_for_tts(sentence) if p.strip()]
                if not pieces:
                    continue
                # Screen text must never reach a log: the guard's own block line
                # would otherwise carry the first ~120 chars as a preview.
                with redact_block_preview():
                    allowed, _reason = _output_guard_with_tts_check(sentence, source=source)
                if not allowed:
                    blocked += 1
                    continue
                fragments = self._fragment_for_tts(pieces)
                if fragments:
                    chunks.extend(fragments)
                    spoken += 1
            logger.info("[VISION] read_aloud sentences=%d blocked=%d", spoken, blocked)
            if not chunks:
                return False
            router = self._ensure_router()
            # V1.33: the reading carries a turn token; the same turn's reaction
            # submits with it (see `_vision_read_turn_id`), so the router lets
            # the reaction wait while a NEW owner reply may cut the reading
            # when the discard toggle is on.
            self._vision_read_seq += 1
            turn_id = self._vision_read_seq
            job = router.submit_streaming(
                read_source, PRIORITY_OWNER, turn_id=turn_id, protected=True,
            )
            self._vision_read_turn_id = turn_id
            if not router.append_chunks(job, chunks):
                return False
            router.seal(job)
            return True
        except Exception:
            logger.debug("[VISION] read aloud failed", exc_info=False)
            if job is not None:
                try:
                    self._ensure_router().discard_job(job)
                except Exception:
                    pass
            return False

    def _vision_speak_progress(self, line: str) -> None:
        """Speak one progress line on the owner band (router only), called from
        the step's ticker thread. Skipped while the bridge or the previous
        progress line is still queued/active/suspended, so lines never pile up
        or talk over each other (the router's owner band preempts on submit)."""
        if not getattr(self, "_speech_router_enabled", False) or self._vision_look_silent:
            return
        from opencohost.core.speech.router import PRIORITY_OWNER, SpeechJobState

        busy = (SpeechJobState.QUEUED, SpeechJobState.ACTIVE, SpeechJobState.SUSPENDED)
        for job in (self._vision_bridge_job, self._vision_progress_job):
            if job is not None and getattr(job, "state", None) in busy:
                return
        source = f"{self._vision_bridge_source}{VISION_PROGRESS_SUFFIX}"
        try:
            self._vision_progress_job = self._ensure_router().submit(
                line, source, PRIORITY_OWNER, releases_arrival_hold=False
            )
        except Exception:
            logger.debug("[VISION] progress line submit failed", exc_info=False)

    def _retire_vision_progress(self) -> None:
        """Drop a progress line that never started once the look is over."""
        job = self._vision_progress_job
        self._vision_progress_job = None
        self._retire_vision_job(job)

    def _retire_vision_bridge(self) -> None:
        """Drop a bridge that never started (or was suspended) once the look is
        over: saying "let me look" after the answer would be backwards. A bridge
        already playing is left to finish."""
        job = self._vision_bridge_job
        self._vision_bridge_job = None
        self._retire_vision_job(job)

    def _retire_vision_status(self) -> None:
        """Same rule for the status line, once the reply is ready to play."""
        job = self._vision_status_job
        self._vision_status_job = None
        self._retire_vision_job(job)

    def _retire_vision_job(self, job) -> None:
        if job is None:
            return
        from opencohost.core.speech.router import SpeechJobState

        if getattr(job, "state", None) not in (SpeechJobState.QUEUED, SpeechJobState.SUSPENDED):
            return
        router = getattr(self, "_speech_router", None)
        if router is None:
            return
        try:
            router.discard_job(job)
        except Exception:
            logger.debug("[VISION] job retire failed", exc_info=False)
