"""Vision gates: decide whether one owner interaction needs a screenshot.

Ports the V0.4 spike rules (``RuleVisionGate``) and adds the explicit,
owner-driven trigger (``ExplicitTriggerGate``). Gates are pure decision
objects: they never capture, never log, and never keep the utterance text.
Only owner sources (``direct``, ``ptt``) can ever trigger vision; viewer chat
is never allowed to make Kira look at the owner's screen.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

OWNER_SOURCES = frozenset({"direct", "ptt"})

REASON_NO_TRIGGER = "no_trigger"
REASON_SOURCE_NOT_OWNER = "source_not_owner"
REASON_EXPLICIT_LOOK = "explicit_look"
REASON_PTT_SWITCH = "ptt_switch"

# V1.26/V1.27: how a look captures. ``default`` keeps the 3-frame burst at 1280.
MODE_DEFAULT = "default"
MODE_CLIP = "clip"
MODE_TEXT = "text"
LOOK_MODES = (MODE_CLIP, MODE_TEXT)
_MODE_ALIASES = {"clip": MODE_CLIP, "video": MODE_CLIP, "text": MODE_TEXT, "texto": MODE_TEXT}
# Words accepted after ``/mira``; read by self-knowledge to derive the command list.
LOOK_MODE_ALIASES = tuple(_MODE_ALIASES)


def normalize_mode(value) -> Optional[str]:
    """``clip``/``text`` (``texto`` and ``video`` accepted), or None when *value*
    is not a look mode. Case-insensitive; never raises."""
    if not isinstance(value, str):
        return None
    return _MODE_ALIASES.get(value.strip().lower())


@dataclass(frozen=True)
class VisionDecision:
    """Outcome of a gate for one interaction."""

    needs_vision: bool
    reason_codes: tuple[str, ...]
    capture_delay_s: float
    # V1.26/V1.27: set only by an armed look that named a mode; the turn step
    # otherwise decides the mode from the utterance (``look_mode``).
    mode: str = MODE_DEFAULT


@runtime_checkable
class VisionGate(Protocol):
    """Port: decide ``needs_vision`` for one interaction."""

    def decide(self, text: str, source: str) -> VisionDecision: ...


def _no(*reasons: str, delay: float = 0.0) -> VisionDecision:
    return VisionDecision(False, tuple(reasons), delay)


class ExplicitTriggerGate:
    """Owner-driven trigger: a PTT switch plus a one-shot "look at this" flag."""

    def __init__(self, look_on_ptt: bool, capture_delay_s: float) -> None:
        self.look_on_ptt = look_on_ptt
        self.capture_delay_s = capture_delay_s
        self._armed = False
        self._armed_mode = MODE_DEFAULT
        self._lock = threading.Lock()

    def arm_look(self, mode: Optional[str] = None) -> None:
        """Arm the one-shot flag; the next owner turn consumes it. ``mode`` is
        an optional look mode (clip/text); anything else means default."""
        with self._lock:
            self._armed = True
            self._armed_mode = normalize_mode(mode) or MODE_DEFAULT

    def is_armed(self) -> bool:
        with self._lock:
            return self._armed

    def _consume(self) -> Optional[str]:
        """The armed look's mode (truthy) and disarm, or None when not armed."""
        with self._lock:
            armed, self._armed = self._armed, False
            mode, self._armed_mode = self._armed_mode, MODE_DEFAULT
            return mode if armed else None

    def decide(self, text: str, source: str) -> VisionDecision:
        # ``text`` is unused on purpose: this gate is purely explicit.
        if source not in OWNER_SOURCES:
            return _no(REASON_SOURCE_NOT_OWNER)
        is_ptt = source == "ptt"
        armed_mode = self._consume()
        if armed_mode is not None:
            delay = 0.0 if is_ptt else self.capture_delay_s
            return VisionDecision(True, (REASON_EXPLICIT_LOOK,), delay, armed_mode)
        if is_ptt and self.look_on_ptt:
            return VisionDecision(True, (REASON_PTT_SWITCH,), 0.0)
        return _no(REASON_NO_TRIGGER)


# ---------------------------------------------------------------------------
# Rule gate (V0.4 spike rules, widened lexicons)
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[¿¡?!.;:\"]", " ", t)
    t = t.replace("'", "").replace("’", "")  # what's -> whats
    return re.sub(r"\s+", " ", t).strip()


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in parts))


_DEICTIC = (r"(?:este|esta|estos|estas|ese|esa|esos|esas|esto|eso|aquel|aquella|"
            r"this|that|these|those)")
# On-screen game objects (es/en).
_OBJ = (r"(?:jefe|boss|enemigo|enemy|arma|armas|weapon|weapons|outfit|letrero|sign|icono|"
        r"icon|mision|quest|mission|thing|cosa|view|vista|paisaje|build|menu|"
        r"espada|sword|armadura|armor|escudo|shield|casco|helmet|bicho|criatura|"
        r"monstruo|creature|monster|puerta|door|item|objeto|npc|personaje|character)")
_STATE_NOUN = (r"(?:vida|hp|salud|health|balas|municion|ammo|tiempo|time|timer|mapa|map|"
               r"nivel|level|dinero|oro|gold|money)")
_STAT = r"(?:health|hp|life|level|ammo|mana|stamina|score|gold|money|xp)"

_POSITIVE = [
    ("look_verb", _rx(
        r"\bmira(?!\s*,)(?:le|lo)?\b(?! la hora)",
        r"\bchec(?:a|ate|ale)\b",
        r"\bfij(?:ate|ense)\b",
        r"\bechale (?:un )?(?:ojo|vistazo)\b", r"\bdale (?:un )?(?:ojo|vistazo)\b",
        r"\balcanzas? a ver\b", r"\bpuedes ver(?:l[oa]s?)?\b",
        r"\b(?:ves|viste) lo que\b",
        r"\bdime (?:que|lo que) ves\b",
        r"\blook at\b", r"\blook here\b",
        r"\bcheck (?:out|this|that)\b",
        r"\bglance at\b", r"\bpeek at\b", r"\bpeep (?:this|that|it)\b",
        r"\btake a look\b",
        r"\b(?:can|do) you see\b", r"\bwhat do you see\b",
    )),
    ("screen_ref", _rx(
        r"\b(?:pantalla|screen)\b",
        r"\b(?:esquina|corner)\b",
    )),
    ("read_request", _rx(
        r"\blee(?:me|lo|r)?\b",
        r"\bread (?:that|this|it)\b",
        r"\bque (?:dice|pone) (?:ahi|alli|aca|aqui|" + _DEICTIC + r")\b",
        r"\bque pone en (?:el|la|ese|esa|este|esta) \w+\b",
        r"\bwhat does " + _DEICTIC + r"(?: \w+)? say\b",
        r"\bwhat " + _DEICTIC + r"(?: \w+)? (?:says|reads)\b",
    )),
    ("onscreen_state", _rx(
        r"\bcuant[oa]s? (?:\w+ )?" + _STATE_NOUN + r"?\s*(?:me quedan?|tengo|le quedan?)\b",
        r"\bme alcanza el (?:dinero|oro)\b",
        r"\bhow (?:much|many) (?:\w+ ){0,2}(?:do i have|is left|are left|left)\b",
        r"\bdonde estoy\b", r"\bwhere am i\b",
        r"\b(?:en|on) (?:el |the )?(?:mapa|map)\b",
        r"\b(?:timer|cronometro)\b",
        r"\bwhat " + _STAT + r" am i\b",
        r"\bwhats my " + _STAT + r"\b", r"\bwhat is my " + _STAT + r"\b",
        r"\bis my " + _STAT + r"\b",
        r"\bcual es mi (?:nivel|vida|salud)\b",
    )),
    ("clip_ref", _rx(
        r"\b(?:este|ese|el|mi|the|this|that|my) (?:\w+ )?(?:video|clip)s?\b",
        # A bare "video(s)" matches like a bare "clip(s)": look_mode() already
        # maps both to clip mode, and "del video" missed the determiner list.
        r"\bclips?\b", r"\bvideos?\b",
        r"\bwatch (?:this|that)\b",
        r"\bwhats happening (?:here|on screen|on the screen)\b",
    )),
    ("image_ref", _rx(
        r"\b(?:el|la|las|los|este|esta|estas|estos|esa|esas|ese|mi|mis|the|this|that|these|those|my)"
        r" (?:\w+ )?(?:imagen|imagenes|foto|fotos|captura|capturas|image|images|picture|pictures|"
        r"screenshot|screenshots)\b",
    )),
    ("deictic_object", _rx(r"\b" + _DEICTIC + r" (?:\w+ )?" + _OBJ + r"\b")),
    ("deictic_query", _rx(r"\bque es (?:eso|esto|aquello)\b", r"\bwhat(?:s| is) (?:that|this)\b")),
    ("onscreen_appearance", _rx(
        r"\b(?:que tal|como) se ve\b",
        r"\bhow does (?:my|this|that) \w+ look\b",
    )),
    ("ui_ref", _rx(
        r"\bbarra (?:de vida|del|de la)\b", r"\b(?:health|boss) bar\b",
        r"\b(?:minimapa|minimap|cartel|hud)\b",
    )),
    ("choice_request", _rx(
        r"\bizquierda o (?:la )?derecha\b", r"\bleft or (?:the )?right\b",
        r"\bcual de (?:estas|estos|esas|esos)\b",
        r"\bcual (?:puerta|camino|salida)\b",
        r"\beste o (?:al |el )?(?:otro|otra)\b", r"\bthis (?:one )?or (?:the )?other\b",
        r"\bwhich (?:\w+ ){0,2}should i\b",
        r"\bpor donde (?:me )?(?:voy|sigo)\b",
    )),
]

# "what data/info can you see / do you have": a question about the assistant's
# own knowledge, not a look request. Shared with the self-knowledge gate
# (capabilities topic). A screen reference keeps it a look request.
DATA_QUESTION_PATTERNS = (
    r"\bque (?:datos|informacion|info) (?:puedes ver|puedes acceder|ves|tienes|manejas|conoces)\b"
    r"(?!.*\b(?:pantalla|screen)\b)",
    r"\bwhat (?:data|info|information) (?:can you (?:see|access)|do you (?:see|have))\b"
    r"(?!.*\b(?:screen|pantalla)\b)",
)

_SUPPRESS = [
    ("suppressed_data_question", _rx(*DATA_QUESTION_PATTERNS)),
    ("suppressed_discourse_marker", _rx(
        r"^(?:\w+ )?(?:mira|look|listen|a ver)\s*,",
        r"\bya veo\b", r"\bno veo la hora\b", r"\bnos vemos\b",
        r"\bi see what\b", r"\blets see\b", r"\bsee you\b", r"\ba ver si\b",
        r"\b(?:te ves|you look|youre looking|you are looking)\b",
    )),
    ("suppressed_negation", _rx(
        r"\b(?:no|ni) (?:lo |la )?mir(?:es|en)\b", r"\b(?:dont|do not) look\b",
    )),
    ("suppressed_past", _rx(
        r"\brecuerd", r"\bremember", r"\breminds?\b", r"\bmemories\b",
        r"\bayer\b", r"\bsemana pasada\b", r"\blast (?:week|stream|time)\b",
        r"\bhace rato\b", r"\bearlier\b",
    )),
    ("suppressed_meta", _rx(
        r"\bchat\b", r"\bviewers?\b", r"\bfollowers?\b", r"\bsubs?\b",
        r"\bdirecto\b", r"\bstream\b", r"\bwiki\b", r"\bgoogle\b", r"\bgente\b",
    )),
    ("suppressed_general", _rx(r"\bnormalmente\b", r"\bin general\b", r"\ben general\b")),
]


class RuleVisionGate:
    """Lexicon rules for utterances that reference the screen.

    A positive rule fires unless a suppressor fires; suppressors win because a
    false trigger costs a few seconds and precision matters more than recall.
    """

    def __init__(self, capture_delay_s: float = 0.0) -> None:
        self.capture_delay_s = capture_delay_s

    def decide(self, text: str, source: str) -> VisionDecision:
        if source not in OWNER_SOURCES:
            return _no(REASON_SOURCE_NOT_OWNER)
        t = _normalize(text)
        hits = [code for code, rx in _POSITIVE if rx.search(t)]
        if not hits:
            return _no(REASON_NO_TRIGGER)
        suppressed = [code for code, rx in _SUPPRESS if rx.search(t)]
        if suppressed:
            return _no(*hits, *suppressed)
        delay = 0.0 if source == "ptt" else self.capture_delay_s
        return VisionDecision(True, tuple(hits), delay)


class CompositeGate:
    """Ask ``primary`` first; only if it says no, ask ``secondary``."""

    def __init__(self, primary: VisionGate, secondary: Optional[VisionGate] = None) -> None:
        self.primary = primary
        self.secondary = secondary

    def decide(self, text: str, source: str) -> VisionDecision:
        first = self.primary.decide(text, source)
        if first.needs_vision or self.secondary is None:
            return first
        second = self.secondary.decide(text, source)
        merged = tuple(dict.fromkeys((*first.reason_codes, *second.reason_codes)))
        delay = second.capture_delay_s if second.needs_vision else first.capture_delay_s
        mode = second.mode if second.needs_vision else first.mode
        return VisionDecision(second.needs_vision, merged, delay, mode)


# ---------------------------------------------------------------------------
# V1.15: requested look vs the automatic PTT look
# ---------------------------------------------------------------------------

REASON_SCREEN_REF = "screen_ref"
REASON_LOOK_VERB = "look_verb"
REASON_READ_REQUEST = "read_request"
_REQUEST_REASONS = frozenset({REASON_SCREEN_REF, REASON_LOOK_VERB, REASON_READ_REQUEST})
_SCREEN_RULES = RuleVisionGate()


def is_requested_look(reason_codes, text) -> bool:
    """True when the owner explicitly asked for this look.

    Requested means the armed look (button/command, ``explicit_look``), a
    screen reference in the utterance (the rule gate's ``screen_ref``), an
    imperative look verb ("mira esto", V1.23: the owner asked her to look) or a
    read request ("léeme eso": reading the screen is looking at it). The
    automatic PTT look (``ptt_switch``) alone is not a request; because the
    composite gate never consults the rule gate once the PTT switch fired, a
    PTT utterance is re-checked here for an unsuppressed screen reference.
    Never raises; the text is only matched, never kept.
    """
    try:
        codes = tuple(reason_codes or ())
        if ({REASON_EXPLICIT_LOOK} | _REQUEST_REASONS) & set(codes):
            return True
        if REASON_PTT_SWITCH not in codes or not isinstance(text, str):
            return False
        decision = _SCREEN_RULES.decide(text, "ptt")
        return decision.needs_vision and bool(_REQUEST_REASONS & set(decision.reason_codes))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# V1.25: the owner asked to see OpenCohost itself
# ---------------------------------------------------------------------------

_SELF_LOOK_RX = _rx(
    r"\bopen ?cohost\b",
    r"\bla app\b", r"\bla aplicacion\b", r"\bla interfaz\b",
    r"\bthe app\b", r"\bthe interface\b",
)


def wants_self_look(text) -> bool:
    """True when the utterance names OpenCohost / the app / the interface.

    Accent- and case-insensitive. Only meaningful on a look turn: it lifts the
    self_capture skip so the main OpenCohost window may be captured (V1.25).
    Never raises; the text is only matched, never kept.
    """
    try:
        return isinstance(text, str) and bool(_SELF_LOOK_RX.search(_normalize(text)))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# V1.26/V1.27: look mode from the utterance
# ---------------------------------------------------------------------------

# Explicit clip words: they beat a text word in the same sentence.
_CLIP_EXPLICIT_RX = _rx(
    r"\bclips?\b", r"\bvideos?\b", r"\banimacion(?:es)?\b", r"\bmovimiento\b",
    r"\bgameplay\b", r"\bwatch (?:this|that|it)\b",
)
# Soft clip phrases ("qué pasa"): a clip on their own, but a text word wins.
_CLIP_SOFT_RX = _rx(
    r"\bque (?:esta )?pasa(?:ndo)?\b",
    r"\bwhats happening\b", r"\bwhat is happening\b",
    r"\bwhats going on\b", r"\bwhat is going on\b",
)
_TEXT_RX = _rx(
    r"\blee(?:me|lo|r)?\b", r"\bque dice\b", r"\btexto\b", r"\bletra\b",
    r"\bread\b", r"\bwhat does \w+(?: \w+)? say\b", r"\bsays\b",
)


def look_mode(text) -> str:
    """``clip``, ``text`` or ``default`` for a look turn's utterance.

    Accent- and case-insensitive, Spanish and English. An explicit clip word
    wins over a text word; a soft clip phrase ("qué pasa") yields to one.
    Never raises; the text is only matched, never kept.
    """
    try:
        if not isinstance(text, str):
            return MODE_DEFAULT
        t = _normalize(text)
        if _CLIP_EXPLICIT_RX.search(t):
            return MODE_CLIP
        if _TEXT_RX.search(t):
            return MODE_TEXT
        if _CLIP_SOFT_RX.search(t):
            return MODE_CLIP
        return MODE_DEFAULT
    except Exception:
        return MODE_DEFAULT


# ---------------------------------------------------------------------------
# V1.30: the utterance talks about screen content (look or no look)
# ---------------------------------------------------------------------------

_SCREEN_CONTENT_RX = _rx(
    r"\b(?:pantalla|screen|screens|screenshots?|captura|capturas)\b",
    r"\b(?:imagen|imagenes|image|images|foto|fotos|fotografia|fotografias|photo|photos|"
    r"picture|pictures)\b",
    r"\b(?:clip|clips|video|videos)\b",
    r"\blo que ves\b", r"\bwhat you see\b", r"\bwhat do you see\b",
    # A document the owner made is on their screen (live 2026-10-17: "mi CV").
    r"\b(?:cv|curriculum(?: vitae)?|documento)\b",
)


def references_screen_content(text) -> bool:
    """True when the utterance names the screen, an image, a clip or a video.

    Used on a turn that carries no screen block: Kira must then say she did not
    look instead of inventing content (V1.30). Accent- and case-insensitive,
    Spanish and English. Never raises; the text is only matched, never kept.
    """
    try:
        return isinstance(text, str) and bool(_SCREEN_CONTENT_RX.search(_normalize(text)))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# vision-off-quick-enable-20261009: the owner asked to look (vision on or off)
# ---------------------------------------------------------------------------

REASON_SCREEN_CONTENT = "screen_ref"


def look_request_reasons(text, source) -> str:
    """Comma-joined reason codes when an owner utterance asks to look, else "".

    One classifier for both the vision-off prompt section and the engine's
    ``vision_off_request`` event, so they cannot drift: an unsuppressed rule
    gate hit, or a screen/image/clip reference (``screen_ref``). A PTT history
    wrapper is stripped first. Owner sources only. Pure; never raises; the
    text is only matched, never kept.
    """
    try:
        if source not in OWNER_SOURCES or not isinstance(text, str):
            return ""
        if source == "ptt":
            try:
                from opencohost.core.memory.memoria_store import strip_history_wrapper

                text = strip_history_wrapper(text) or text
            except Exception:
                pass
        decision = _SCREEN_RULES.decide(text, source)
        if decision.needs_vision:
            return ",".join(decision.reason_codes)
        return REASON_SCREEN_CONTENT if references_screen_content(text) else ""
    except Exception:
        return ""
