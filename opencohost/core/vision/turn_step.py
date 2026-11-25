"""VisionTurnStep: gate -> bridge line -> capture -> describe -> screen block.

Runs on the engine thread for one owner turn, strictly sequentially and
BEFORE LLM generation starts: V0.2 measured that a vision call overlapping
voice-model generation drops Qwen's speed by ~62-80%, so nothing here is
concurrent with the reply.

Rules:
- A "no vision" decision costs nothing: no capture, no sleep, no bridge.
- The bridge line is the first thing Kira says on a vision turn (owner
  requirement, V0.5), spoken before the capture delay, capture and describe.
- V1.11 burst: after the bridge (and the optional capture delay, default 0)
  the step grabs ``frames`` frames ``frame_interval_s`` apart. Frames taken
  while OpenCohost itself is in front are skipped (``self_capture`` when all
  are); black frames are dropped (``black_frame`` when all are);
  near-duplicates are compacted (frames.py). The kept frames, newest last,
  go to the describer in one request.
- V1.13: right before the describe request the step fires ``on_describing``
  (the "processing the frames" phase), and a successful look carries a short
  ``status_line`` in the bridge's language for the engine to speak, only
  when the bridge itself was spoken. A small OpenCohost window (the desktop
  companion) no longer counts as self; the log line carries a coarse
  ``self_kind`` (main/overlay/none), never a title or path.
- Fail-open: every error yields an empty block plus a reason code; ``run``
  never raises into the turn.
- Privacy: one ``[VISION]`` log line with the decision, reason codes, stage
  latencies and outcome. Never the question, the description or the image.
  Image bytes stay in memory and are dropped when the turn ends.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from opencohost.core.vision.capture import CaptureUnavailable
from opencohost.core.vision.describer import MOSAIC_HINT, VisionDescribeError
from opencohost.core.vision.direct_cloud import build_direct_screen_block
from opencohost.core.vision.foreground import SELF_KIND_MAIN, SELF_KIND_NONE, SELF_KIND_OVERLAY
from opencohost.core.vision.frames import MAX_FRAMES_SENT, compact
from opencohost.core.vision.gate import (
    LOOK_MODES,
    MODE_CLIP,
    MODE_DEFAULT,
    MODE_TEXT,
    look_mode,
    wants_self_look,
)
from opencohost.core.vision.mosaic import build_mosaic
from opencohost.core.vision.sanitize import (
    DEFAULT_MAX_CHARS,
    build_screen_block,
    sanitize_description,
)

logger = logging.getLogger("OpenCohost")

BRIDGE_LINES: dict[str, tuple[str, ...]] = {
    "es": ("A ver, déjame mirar…", "A ver, déjame echar un ojo…", "Déjame ver…"),
    "en": ("Let me take a look…", "Hang on, let me look…", "Let me see…"),
}
# V1.13: spoken after a successful describe so the owner knows the frames were
# seen and the reply is on its way (the V1.7 third run had ~10 s of silence).
STATUS_LINES: dict[str, tuple[str, ...]] = {
    "es": ("Ya vi, dame un segundo.",),
    "en": ("Got it, one sec.",),
}

# Outcome codes. Every look outcome is one of the published codes (V1.8): they
# reach the `vision_result` event and GET /api/vision `status.last_result`.
# The not-ready codes come from the controller's ``ready_check``.
OUTCOME_NO_VISION = "no_vision"  # gate said no look; never published
OUTCOME_OK = "ok"
OUTCOME_CAPTURE_UNAVAILABLE = "capture_unavailable"
OUTCOME_BLACK_FRAME = "black_frame"
OUTCOME_EMPTY = "describe_empty"
OUTCOME_CANCELLED = "describe_cancelled"
OUTCOME_ERROR = "error"
# V1.11: every burst frame showed OpenCohost itself in the foreground.
OUTCOME_SELF_CAPTURE = "self_capture"
REASON_BRIDGE_FAILED = "bridge_failed"
# V1.23: the controller's "not ready yet" code the step waits on (same string).
OUTCOME_MODEL_WARMING = "model_warming"
REASON_WARMING_WAITED = "warming_waited"
# V1.25: the owner asked to see OpenCohost itself, so main-window frames are kept.
REASON_SELF_LOOK_REQUESTED = "self_look_requested"

# V1.26 clip mode: 6 frames spread over ~3 s (5 gaps), tiled into one 3x2
# mosaic (mosaic.py) and described at ctx 4096 (V0.7: ~1.7k tokens, 3.3 s). The
# near-duplicate compaction is skipped on purpose: it would collapse a slow clip
# into fewer cells and break the even time spread the mosaic hint promises.
CLIP_FRAMES = 6
CLIP_FRAME_INTERVAL_S = 0.6
CLIP_NUM_CTX = 4096
# V1.27 text mode: ONE frame at full capture resolution, long side capped at
# 1920 (measured on qwen3-vl:4b: 1920x1080 = 2,071 prompt tokens, 2560x1440 =
# 3,631 which leaves no room in 4096, 3840x2160 = HTTP 400) and JPEG quality 92.
# ``num_ctx`` stays at the warmed 4096: a different value makes Ollama reload
# the model (measured 9.8 s vs 3.6 s), far worse than the VRAM it would save.
TEXT_MAX_SIDE = 1920
TEXT_JPEG_QUALITY = 92
TEXT_NUM_CTX = 4096
REASON_MODE_PREFIX = "mode_"
# P2: the look skipped the describe call and sends its frames to the cloud chat model.
REASON_ROUTE_DIRECT = "route_direct"

# V1.23: a look decided while the model is warming/starting waits this long for
# ready (owner run: a look 13 s after the toggle hit model_warming). Polled at
# WARMING_POLL_S through the injected sleep; bounded by count AND by the clock.
WARMING_WAIT_S = 10.0
WARMING_POLL_S = 0.5

# V1.28: per-mode describe limits, in one place. ``None`` means "keep the
# default": ``num_predict`` None = the describer's own cap (350 tokens),
# ``timeout_s`` None = the configured ``describe_timeout_s`` (15 s by default).
# Text mode has room for a long transcription: ctx 4096 still fits an image of
# about 2070 tokens + prompt about 300 + 1000 output tokens. A configured
# timeout longer than the profile's is never shortened.
@dataclass(frozen=True)
class LookProfile:
    num_predict: Optional[int] = None
    max_chars: int = DEFAULT_MAX_CHARS
    timeout_s: Optional[float] = None


MODE_PROFILES: dict[str, LookProfile] = {
    MODE_DEFAULT: LookProfile(),
    MODE_CLIP: LookProfile(),
    MODE_TEXT: LookProfile(num_predict=1000, max_chars=3500, timeout_s=20.0),
}

# V1.28: while a describe runs longer than this, Kira speaks a short progress
# line every interval (owner band, ``-vision-progress``), so the owner never
# waits in silence for more than one interval. Rotated, es/en by the question.
PROGRESS_INTERVAL_S = 5.0
PROGRESS_LINES: dict[str, tuple[str, ...]] = {
    "es": ("Sigo leyendo…", "Ya casi…", "Un momento más…"),
    "en": ("Still reading…", "Almost there…", "One more moment…"),
}


def _event_wait(stop: threading.Event, seconds: float) -> bool:
    """Default progress wait: True when ``stop`` was set within ``seconds``."""
    return stop.wait(seconds)


class _ProgressTicker:
    """Speaks a rotating line every ``interval_s`` until stopped.

    Race-safe: ``stop()`` sets the flag under the same lock the tick speaks
    under, so a tick that wakes concurrently with completion either spoke
    before ``stop()`` returned or never speaks. The thread is a daemon and is
    not joined (a speak is one non-blocking router submit).
    """

    def __init__(self, speak, lines, interval_s, wait) -> None:
        self._speak = speak
        self._lines = lines
        self._interval_s = interval_s
        self._wait = wait
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.count = 0
        self._thread = threading.Thread(target=self._run, name="vision-progress", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> int:
        with self._lock:
            self._stop.set()
            return self.count

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._wait(self._stop, self._interval_s):
                    return
            except Exception:
                return
            with self._lock:
                if self._stop.is_set():
                    return
                line = self._lines[self.count % len(self._lines)]
                self.count += 1
                try:
                    self._speak(line)
                except Exception:
                    logger.debug("[VISION] progress line failed", exc_info=False)


# V1.7: after the bridge line Kira answered "you showed me nothing" because a
# failed look gave her no signal. When a look was really attempted (bridge
# stage reached) and failed, the turn carries this code-built note instead of
# a screen block. Only the fixed outcome code is interpolated, never text.
VISION_FAILURE_NOTE = (
    "VISION NOTE: You tried to look at the owner's screen just now but could not "
    "(reason: {reason}). Say so briefly in character; do not claim they showed "
    "you nothing."
)

# V1.15: when a REQUESTED look fails the turn skips the LLM and Kira says one
# of these instead (owner evidence 2026-10-02: under GPU load every look timed
# out and Gemma still spent 76-97 s on a long reply). Keyed by outcome category.
FAILURE_LINES: dict[str, dict[str, str]] = {
    "busy": {
        "es": "No alcancé a ver tu pantalla, la GPU va muy cargada. Inténtalo de nuevo.",
        "en": "I couldn't see your screen in time, the GPU is busy. Try again.",
    },
    "self": {
        "es": "Solo veo OpenCohost en pantalla. Pon delante lo que quieres que vea.",
        "en": "All I can see is OpenCohost. Bring what you want me to see to the front.",
    },
    "generic": {
        "es": "No pude ver tu pantalla esta vez. Inténtalo de nuevo.",
        "en": "I couldn't see your screen this time. Try again.",
    },
    # V1.23: the vision model was still warming after the bounded wait.
    "warming": {
        "es": "Todavía me estoy despertando la vista, pídemelo otra vez en unos segundos.",
        "en": "My eyes are still waking up, ask me again in a few seconds.",
    },
    # A2: the look went to the cloud chat model, which did not answer (manual
    # fallback mode keeps routing to it, so nothing else would speak).
    "cloud_unavailable": {
        "es": "No pude ver tu pantalla: el modelo en la nube no respondió.",
        "en": "I couldn't see your screen: the cloud model didn't answer.",
    },
    # B2: manual mode, the cloud provider is out of credits/quota.
    "cloud_quota_exhausted": {
        "es": "Me quedé sin créditos en el proveedor de la nube.",
        "en": "I ran out of credits with the cloud provider.",
    },
    # A3: the owner put vision on the chat model, but the chat model cannot see.
    "chat_model_not_vision_capable": {
        "es": "Tengo la visión puesta en el modelo del chat, pero tu modelo actual no ve imágenes.",
        "en": "Vision is set to the chat model, but your current model can't see images.",
    },
    "chat_model_requires_cloud_provider": {
        "es": "Tengo la visión puesta en el modelo del chat, pero ahora el chat es local y no ve imágenes.",
        "en": "Vision is set to the chat model, but the chat is local right now and can't see images.",
    },
}
# Outcomes that never get a canned line: success, no look, an owner-cancelled
# turn (speaking after a stop would be wrong), and a step error (fail open).
_NO_FAILURE_LINE = frozenset({"", "ok", "no_vision", "describe_cancelled", "error"})
_FAILURE_CATEGORY = {
    "describe_timeout": "busy",
    "self_capture": "self",
    OUTCOME_MODEL_WARMING: "warming",
    "cloud_unavailable": "cloud_unavailable",
    "cloud_quota_exhausted": "cloud_quota_exhausted",
    "chat_model_not_vision_capable": "chat_model_not_vision_capable",
    "chat_model_requires_cloud_provider": "chat_model_requires_cloud_provider",
}

# V1.15: appended to the failure note when only the automatic PTT look failed
# and the turn still goes to the LLM.
VISION_FAILURE_SHORT_HINT = "Keep the reply short and direct."


def failure_line(outcome: str, lang: str) -> str:
    """Canned owner-band line for a failed requested look; "" when none applies."""
    if outcome in _NO_FAILURE_LINE:
        return ""
    lines = FAILURE_LINES[_FAILURE_CATEGORY.get(outcome, "generic")]
    return lines.get(lang) or lines["es"]


_ES_HINT = re.compile(
    r"[¿¡ñáéíóú]|\b(?:que|mira|esto|eso|este|esta|ese|esa|como|donde|cuanto|cuanta|"
    r"tengo|ves|puedes|dime|oye|el|la|los|las|es|de|en|y|por|para|me|mi|con|hago|queda)\b",
    re.IGNORECASE,
)
_EN_HINT = re.compile(
    r"\b(?:what|whats|look|this|that|how|where|see|can|you|the|is|my|do|does|at|which|"
    r"much|many|have|weak|read|check|say)\b",
    re.IGNORECASE,
)


def detect_language(text: str, default: str = "es") -> str:
    """Best-effort es/en guess for the bridge line; ``default`` when unclear."""
    if not text:
        return default
    lowered = text.replace("'", "").replace("’", "")
    es = len(_ES_HINT.findall(lowered))
    en = len(_EN_HINT.findall(lowered))
    if es == en:
        return default
    return "es" if es > en else "en"


@dataclass(frozen=True)
class VisionTurnResult:
    """What the turn needs from vision. Never carries image bytes."""

    screen_block: str
    reason_codes: tuple[str, ...]
    outcome: str
    timings: dict[str, int] = field(default_factory=dict)
    # Failure note for the prompt when an attempted look failed; "" otherwise.
    note: str = ""
    # V1.11: look id (keys the thumbnail ring) and burst counters.
    look_id: str = ""
    frames_captured: int = 0
    frames_sent: int = 0
    self_capture_skipped: int = 0
    # V1.13: owner-band status line to speak after a successful look; "" when
    # the look failed or the bridge was never spoken.
    status_line: str = ""
    # V1.26/V1.27: how the look captured: default, clip or text.
    mode: str = MODE_DEFAULT
    # V1.32: the sanitized VL transcription, only for a successful TEXT look (the
    # engine reads it aloud). Never logged, never published: the ``[VISION]`` log
    # line and the ``vision_result`` event carry codes and counts only.
    transcription: str = ""
    # Vision transparency: the sanitized description of a successful look, kept by
    # the controller next to the look's frames (same expiry). Same privacy as
    # ``transcription``: hidden from repr, never logged, never published.
    description: str = field(default="", repr=False)
    # cloud-providers-vision P2: the captured frames (bytes, mime) of a DIRECT look,
    # sent to the cloud chat model as image parts instead of being described. Empty
    # on every other path. Hidden from repr, never logged, never published.
    images: tuple = field(default=(), repr=False)


# Coarse per-look summary of what the foreground probe saw, worst first.
_SELF_KIND_RANK = {SELF_KIND_NONE: 0, SELF_KIND_OVERLAY: 1, SELF_KIND_MAIN: 2}


@dataclass
class _LookCounters:
    look_id: str = ""
    frames_captured: int = 0
    frames_sent: int = 0
    self_skipped: int = 0
    self_kind: str = SELF_KIND_NONE
    bridge_spoken: bool = False
    lang: str = "es"
    # V1.25: the utterance asked to see OpenCohost; main-window frames are kept.
    allow_self: bool = False
    self_kept: int = 0
    mode: str = MODE_DEFAULT
    progress_lines: int = 0

    def saw(self, kind: str) -> None:
        if _SELF_KIND_RANK.get(kind, 0) > _SELF_KIND_RANK.get(self.self_kind, 0):
            self.self_kind = kind


def _burst_size(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(MAX_FRAMES_SENT, count))


def _new_look_id() -> str:
    return uuid.uuid4().hex


class VisionTurnStep:
    """Orchestrates one owner turn's optional look at the screen."""

    def __init__(
        self,
        gate: Any,
        capture: Optional[Any],
        describer: Any,
        speak_bridge: Callable[[str], None],
        *,
        settings: Any,
        ready_check: Optional[Callable[[], Optional[str]]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        choice: Callable[[Sequence[str]], str] = random.choice,
        on_looking: Optional[Callable[[bool], None]] = None,
        on_result: Optional[Callable[[VisionTurnResult], None]] = None,
        default_language: str = "es",
        foreground_probe: Optional[Any] = None,
        on_thumbnails: Optional[Callable[[str, list], None]] = None,
        new_id: Callable[[], str] = _new_look_id,
        on_describing: Optional[Callable[[], None]] = None,
        warming_wait_s: float = WARMING_WAIT_S,
        warming_poll_s: float = WARMING_POLL_S,
        speak_progress: Optional[Callable[[str], None]] = None,
        progress_interval_s: float = PROGRESS_INTERVAL_S,
        progress_wait: Callable[[threading.Event, float], bool] = _event_wait,
        direct_check: Optional[Callable[[], bool]] = None,
        chat_model_check: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self.gate = gate
        self._capture = capture
        self._describer = describer
        self._speak_bridge = speak_bridge
        self._settings = settings
        self._ready_check = ready_check
        self._clock = clock
        self._sleep = sleep
        self._choice = choice
        self._on_looking = on_looking
        self._on_result = on_result
        self.default_language = default_language
        self._foreground_probe = foreground_probe
        self._on_thumbnails = on_thumbnails
        self._new_id = new_id
        self._on_describing = on_describing
        self._warming_wait_s = warming_wait_s
        self._warming_poll_s = warming_poll_s
        self._speak_progress = speak_progress
        self._progress_interval_s = progress_interval_s
        self._progress_wait = progress_wait
        # P2: True when the frames should go straight to a vision-capable cloud
        # chat model (no describe call, no local vision daemon). None = never.
        self._direct_check = direct_check
        # A3: a failure code when the owner chose the chat model as the eye but it
        # cannot see; the look then fails fast with a specific outcome. None = never.
        self._chat_model_check = chat_model_check
        self._looking = threading.Event()

    @property
    def looking(self) -> bool:
        """True from the bridge line until the describe stage ends."""
        return self._looking.is_set()

    def update_settings(self, settings: Any) -> None:
        """Swap the settings read by the next turn (``describe_timeout_s``)."""
        self._settings = settings

    def run(
        self, text: str, source: str, *, cancel: Optional[threading.Event] = None,
        decisions: Any = None,
    ) -> VisionTurnResult:
        """Never raises. Returns an empty block on every non-success path.

        ``decisions`` (laya-decision-layer-20261009) is the turn's
        ``TurnDecisions``; None keeps the gate's decision as is."""
        reasons: list[str] = []
        timings: dict[str, int] = {}
        decided = False
        look = _LookCounters()
        try:
            decision = self.gate.decide(text, source)
            if decisions is not None:
                from opencohost.core.decision.consumers import apply_vision_decision

                decision = apply_vision_decision(
                    decision, decisions, source=source,
                    rule_gate=getattr(self.gate, "secondary", None),
                )
            reasons.extend(decision.reason_codes)
            decided = decision.needs_vision
            if not decided:
                return self._finish("skip", reasons, timings, OUTCOME_NO_VISION)
            look.look_id = self._new_id()
            return self._look(text, decision, reasons, timings, cancel, look)
        except Exception as exc:  # fail-open: the turn proceeds without vision
            reasons.append(f"error_{type(exc).__name__}")
            # ``looking`` is set right before the bridge: past it, the look was attempted.
            return self._finish(
                "look" if decided else "skip", reasons, timings, OUTCOME_ERROR,
                attempted=self.looking, look=look,
            )
        finally:
            self._set_looking(False)

    # -- internals ------------------------------------------------------------

    def _look(self, text, decision, reasons, timings, cancel, look) -> VisionTurnResult:
        # An armed look that named a mode wins; otherwise the utterance decides.
        armed_mode = getattr(decision, "mode", MODE_DEFAULT)
        look.mode = armed_mode if armed_mode in LOOK_MODES else look_mode(text)
        if look.mode != MODE_DEFAULT:
            reasons.append(REASON_MODE_PREFIX + look.mode)
        direct = self._direct_route()
        if direct:
            reasons.append(REASON_ROUTE_DIRECT)
        if not direct:
            blocked = self._chat_model_blocked()
            if blocked:
                reasons.append(blocked)
                return self._finish(
                    "look", reasons, timings, blocked, attempted=True, look=look
                )
        if self._ready_check is not None and not direct:
            not_ready = self._ready_check()
            if not_ready == OUTCOME_MODEL_WARMING:
                not_ready = self._await_ready(not_ready, cancel, reasons)
            if not_ready:
                reasons.append(not_ready)
                return self._finish("look", reasons, timings, not_ready, look=look)
        if self._capture is None:
            reasons.append(OUTCOME_CAPTURE_UNAVAILABLE)
            return self._finish("look", reasons, timings, OUTCOME_CAPTURE_UNAVAILABLE, look=look)

        self._set_looking(True)
        look.allow_self = wants_self_look(text)
        look.lang = detect_language(text, default=self.default_language)
        look.bridge_spoken = self._bridge(look.lang, reasons)
        if decision.capture_delay_s > 0:
            self._sleep(decision.capture_delay_s)

        captured, cancelled = self._burst(timings, cancel, look)
        if look.self_kept:
            reasons.append(REASON_SELF_LOOK_REQUESTED)
        if cancelled:
            reasons.append(OUTCOME_CANCELLED)
            return self._finish(
                "look", reasons, timings, OUTCOME_CANCELLED, attempted=True, look=look
            )
        if not captured:
            # Nothing grabbed: every frame showed the app itself, or capture failed.
            code = OUTCOME_SELF_CAPTURE if look.self_skipped else OUTCOME_CAPTURE_UNAVAILABLE
            reasons.append(code)
            return self._finish("look", reasons, timings, code, attempted=True, look=look)
        usable = [frame for frame in captured if not frame.is_black]
        del captured
        if not usable:
            reasons.append(OUTCOME_BLACK_FRAME)
            return self._finish(
                "look", reasons, timings, OUTCOME_BLACK_FRAME, attempted=True, look=look
            )

        jpegs, thumbnails, extra = self._prepare(look, usable)
        del usable
        if getattr(self._settings, "show_thumbnails", False):
            self._store_thumbnails(look.look_id, thumbnails)
        if direct:
            # No describe call: the frames ride in the chat call itself.
            images = tuple((jpeg, "image/jpeg") for jpeg in jpegs)
            del jpegs
            return self._finish(
                "look", reasons, timings, OUTCOME_OK, build_direct_screen_block(),
                look=look, images=images,
            )
        profile = MODE_PROFILES.get(look.mode, MODE_PROFILES[MODE_DEFAULT])
        timeout_s = float(self._settings.describe_timeout_s)
        if profile.timeout_s:
            timeout_s = max(timeout_s, profile.timeout_s)
        if profile.num_predict:
            extra = {**extra, "num_predict": profile.num_predict}
        self._describing()
        started = self._clock()
        ticker = self._start_progress(look)
        try:
            description = self._describer.describe(
                jpegs,
                text,
                timeout_s=timeout_s,
                cancel=cancel,
                **extra,
            )
        except VisionDescribeError as exc:
            timings["describe_ms"] = _ms(self._clock() - started)
            code = f"describe_{exc.reason}"
            reasons.append(code)
            return self._finish("look", reasons, timings, code, attempted=True, look=look)
        finally:
            del jpegs
            if ticker is not None:
                look.progress_lines = ticker.stop()
        timings["describe_ms"] = _ms(self._clock() - started)

        cap = profile.max_chars
        body = sanitize_description(description or "", cap)
        block = build_screen_block(body, cap)
        if not block:
            reasons.append(OUTCOME_EMPTY)
            return self._finish(
                "look", reasons, timings, OUTCOME_EMPTY, attempted=True, look=look
            )
        transcription = body if look.mode == MODE_TEXT else ""
        return self._finish(
            "look", reasons, timings, OUTCOME_OK, block, look=look,
            transcription=transcription, description=body,
        )

    def _chat_model_blocked(self) -> str:
        """The A3 failure code, or "" (fail-open on any error)."""
        if self._chat_model_check is None:
            return ""
        try:
            return str(self._chat_model_check() or "")
        except Exception:
            logger.debug("[VISION] chat model check failed", exc_info=False)
            return ""

    def _direct_route(self) -> bool:
        """True when this look goes straight to the cloud chat model. Fail-closed
        to the describe path on any error."""
        if self._direct_check is None:
            return False
        try:
            return bool(self._direct_check())
        except Exception:
            logger.debug("[VISION] direct check failed", exc_info=False)
            return False

    def _prepare(self, look, usable) -> tuple[list, list, dict]:
        """What goes to the describer: (images, the same sent frames for the
        in-memory ring, extra describe kwargs). Sets ``look.frames_sent``. Default looks send
        the compacted burst and no extras, exactly as before V1.26."""
        if look.mode == MODE_CLIP and len(usable) > 1:
            try:
                mosaic = build_mosaic([frame.jpeg for frame in usable])
            except Exception:  # fail-open to the plain burst (e.g. Pillow trouble)
                logger.debug("[VISION] mosaic failed", exc_info=False)
            else:
                look.frames_sent = len(usable)
                return [mosaic], [mosaic], {"num_ctx": CLIP_NUM_CTX, "hint": MOSAIC_HINT}
        if look.mode == MODE_TEXT:
            frame = usable[-1]  # one frame was grabbed: the newest, at full quality
            look.frames_sent = 1
            return [frame.jpeg], [frame.jpeg], {"num_ctx": TEXT_NUM_CTX}
        sent = compact(usable)
        look.frames_sent = len(sent)
        jpegs = [f.jpeg for f in sent]
        return jpegs, list(jpegs), {}

    def _start_progress(self, look) -> Optional[_ProgressTicker]:
        """Start the progress ticker for this describe, or None when the step
        has no progress speaker. Never raises."""
        if self._speak_progress is None:
            return None
        try:
            lines = PROGRESS_LINES.get(look.lang) or PROGRESS_LINES["es"]
            ticker = _ProgressTicker(
                self._speak_progress, lines, self._progress_interval_s, self._progress_wait
            )
            ticker.start()
            return ticker
        except Exception:
            logger.debug("[VISION] progress ticker failed to start", exc_info=False)
            return None

    def _await_ready(self, not_ready: str, cancel, reasons: list[str]) -> Optional[str]:
        """Wait (bounded) for a warming model. Returns the final not-ready
        code, or None when it became ready. Never sleeps for another reason."""
        budget = max(0.0, float(self._warming_wait_s or 0.0))
        poll = max(0.05, float(self._warming_poll_s or 0.5))
        started = self._clock()
        waited = 0.0
        # ``waited`` bounds the loop even under a frozen test clock.
        while not_ready == OUTCOME_MODEL_WARMING and waited < budget:
            if cancel is not None and cancel.is_set():
                break
            if self._clock() - started >= budget:
                break
            step_s = min(poll, budget - waited)
            self._sleep(step_s)
            waited += step_s
            not_ready = self._ready_check()
        if not not_ready:
            reasons.append(REASON_WARMING_WAITED)
        return not_ready

    def _burst(self, timings, cancel, look) -> tuple[list, bool]:
        """Grab up to ``frames`` frames ``frame_interval_s`` apart.

        Returns (captured frames in time order, cancelled). A frame taken
        while the app itself is in front is skipped; a capture error ends the
        burst with the frames already taken. ``capture_ms`` sums the grabs
        only, not the interval sleeps.
        """
        count = _burst_size(getattr(self._settings, "frames", 1))
        interval = max(0.0, float(getattr(self._settings, "frame_interval_s", 0.0) or 0.0))
        grab_kwargs: dict = {}
        if look.mode == MODE_CLIP:
            count, interval = CLIP_FRAMES, CLIP_FRAME_INTERVAL_S
        elif look.mode == MODE_TEXT:
            count, interval = 1, 0.0
            grab_kwargs = {"max_side": TEXT_MAX_SIDE, "jpeg_quality": TEXT_JPEG_QUALITY}
        captured: list = []
        capture_s = 0.0
        try:
            for index in range(count):
                if index > 0 and interval > 0:
                    self._sleep(interval)
                if cancel is not None and cancel.is_set():
                    return captured, True
                kind = self._foreground_kind()
                look.saw(kind)
                if kind == SELF_KIND_MAIN:
                    if look.allow_self:
                        look.self_kept += 1  # V1.25: the owner asked to see the app
                    else:
                        look.self_skipped += 1
                        continue
                started = self._clock()
                try:
                    frame = self._capture.capture(**grab_kwargs)
                except CaptureUnavailable:
                    capture_s += self._clock() - started
                    break
                capture_s += self._clock() - started
                captured.append(frame)
                look.frames_captured += 1
        finally:
            timings["capture_ms"] = _ms(capture_s)
        return captured, False

    def _foreground_kind(self) -> str:
        """``main`` (skip the frame), ``overlay`` or ``none``. A probe without
        ``foreground_kind`` (pre-V1.13) maps its boolean to main/none."""
        probe = self._foreground_probe
        if probe is None:
            return SELF_KIND_NONE
        try:
            kind_of = getattr(probe, "foreground_kind", None)
            if kind_of is not None:
                kind = kind_of()
                return kind if kind in _SELF_KIND_RANK else SELF_KIND_NONE
            return SELF_KIND_MAIN if probe.is_self_foreground() else SELF_KIND_NONE
        except Exception:
            return SELF_KIND_NONE  # fail-open: never skip a frame on a probe error

    def _describing(self) -> None:
        if self._on_describing is None:
            return
        try:
            self._on_describing()
        except Exception:
            logger.debug("[VISION] describing callback failed", exc_info=False)

    def _store_thumbnails(self, look_id: str, thumbnails: list) -> None:
        if not thumbnails or self._on_thumbnails is None:
            return
        try:
            self._on_thumbnails(look_id, thumbnails)
        except Exception:
            logger.debug("[VISION] thumbnail callback failed", exc_info=False)

    def _bridge(self, lang: str, reasons: list[str]) -> bool:
        """Speak the bridge line; False when it could not be handed off."""
        lines = BRIDGE_LINES.get(lang) or BRIDGE_LINES["es"]
        try:
            self._speak_bridge(self._choice(lines))
            return True
        except Exception:
            reasons.append(REASON_BRIDGE_FAILED)
            return False

    def _status_line(self, look: _LookCounters) -> str:
        if not look.bridge_spoken:
            return ""
        lines = STATUS_LINES.get(look.lang) or STATUS_LINES["es"]
        try:
            return self._choice(lines)
        except Exception:
            return ""

    def _set_looking(self, value: bool) -> None:
        if self._looking.is_set() == value:
            return
        if value:
            self._looking.set()
        else:
            self._looking.clear()
        if self._on_looking is not None:
            try:
                self._on_looking(value)
            except Exception:
                logger.debug("[VISION] looking callback failed", exc_info=False)

    def _finish(
        self, decision, reasons, timings, outcome, block: str = "", *,
        attempted: bool = False, look: Optional[_LookCounters] = None,
        transcription: str = "", description: str = "", images: tuple = (),
    ) -> VisionTurnResult:
        look = look or _LookCounters()
        logger.info(
            "[VISION] decision=%s reasons=%s capture_ms=%s describe_ms=%s "
            "frames=%d/%d self_skipped=%d self_kind=%s mode=%s progress_lines=%d outcome=%s",
            decision,
            ",".join(reasons) or "-",
            timings.get("capture_ms", "-"),
            timings.get("describe_ms", "-"),
            look.frames_captured,
            look.frames_sent,
            look.self_skipped,
            look.self_kind,
            look.mode,
            look.progress_lines,
            outcome,
        )
        note = VISION_FAILURE_NOTE.format(reason=outcome) if attempted and not block else ""
        result = VisionTurnResult(
            block, tuple(reasons), outcome, dict(timings), note,
            look_id=look.look_id,
            frames_captured=look.frames_captured,
            frames_sent=look.frames_sent,
            self_capture_skipped=look.self_skipped,
            # A1: a direct look (frames ride in the chat call) has seen nothing yet.
            status_line=(
                self._status_line(look) if outcome == OUTCOME_OK and block and not images else ""
            ),
            mode=look.mode,
            transcription=transcription,
            description=description,
            images=images,
        )
        if decision == "look" and self._on_result is not None:
            try:
                self._on_result(result)
            except Exception:
                logger.debug("[VISION] result callback failed", exc_info=False)
        return result


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))
