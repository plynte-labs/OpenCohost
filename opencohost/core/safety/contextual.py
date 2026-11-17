"""Contextual safety arbiter, multi-turn trajectory trackers, and agenda deck safety.

Promoted from ``docs/SAFETY_CONTEXTUAL_CRISIS_DESIGN.md``:
- Separates genuine first-person crisis signals from gaming/fiction mortality,
  laughter-dampened hyperbole, second-person troll harassment, and
  quote/coercion prompt-injection bait.
- Enforces serious-amplifier veto (any genuine preparation or distress
  amplifier plus a self-harm stem overrides laughter and gaming markers).
- Laughter or joke markers never lower an explicit first-person self-harm stem
  below CRISIS_INTERVENTION (owner decision 2026-11-16, option B; design H8).
- Tracks owner safety escalation and privacy-preserving per-viewer chat state
  using ephemeral per-session HMAC-SHA256 digests (zero raw chat, usernames,
  or unkeyed hashes stored or logged).
- Provides pre-prompt sarcasm stripping, topic safety classification,
  deterministic move filtering (preserving ``prefetch == live`` parity), and 9B
  nihilistic-drift output checks.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from opencohost.core.safety.crisis import (
    _normalize,
    _normalize_obfuscated,
    has_explicit_crisis_stem,
)
from opencohost.i18n import active as i18n_active

# ---------------------------------------------------------------------------
# Enums & data models
# ---------------------------------------------------------------------------


class SafetySeverity(str, Enum):
    NONE = "none"
    BENIGN_HYPERBOLE = "benign_hyperbole"
    GAMING_FICTION = "gaming_fiction"
    TROLL_ABUSE_BAIT = "troll_abuse_bait"
    SENSITIVE_CARE = "sensitive_care"
    CRISIS_INTERVENTION = "crisis_intervention"


# Alias exported for callers referencing SafetyTier
SafetyTier = SafetySeverity


class TurnSource(str, Enum):
    DIRECT = "direct"
    PTT = "ptt"
    OWNER_BUNDLE = "owner-bundle"
    CHAT = "chat"
    KIRA_AGENDA = "kira-agenda"
    KIRA_AGENDA_STOP = "kira-agenda-stop"


OWNER_CRISIS_SOURCES = frozenset({
    TurnSource.DIRECT.value,
    TurnSource.PTT.value,
    TurnSource.OWNER_BUNDLE.value,
})


class TopicSafetyTier(str, Enum):
    STANDARD = "standard"
    EXISTENTIAL_DARK = "existential_dark"
    VULNERABLE_PERSONAL = "vulnerable_personal"


@dataclass(frozen=True)
class StreamContextMetadata:
    active_game: str | None = None
    game_genres: tuple[str, ...] = ()
    active_topic_title: str | None = None
    active_card_tags: tuple[str, ...] = ()
    chat_activity_mood: str = "neutral"


@dataclass(frozen=True)
class SignalBreakdown:
    lexical_tier: str
    base_score: float
    laughter_dampener: float
    gaming_dampener: float
    serious_amplifier: float
    trajectory_delta: float
    final_score: float
    dampeners_vetoed: bool
    dampeners_applied: tuple[str, ...]
    amplifiers_applied: tuple[str, ...]
    is_troll_or_bait: bool = False
    troll_kind: str = ""


@dataclass(frozen=True)
class CrisisAssessment:
    severity: SafetySeverity
    should_trigger_crisis_turn: bool
    should_strip_sarcasm: bool
    should_withhold_chat_msg: bool
    bypass_llm_with_canned_reply: bool
    signals: SignalBreakdown


@dataclass(frozen=True)
class SafetyTurnRecord:
    timestamp_monotonic: float
    severity: SafetySeverity
    effective_score: float
    had_distress_signal: bool
    had_dampener_only: bool


@dataclass(frozen=True)
class TopicSafetyProfile:
    tier: TopicSafetyTier
    blocked_families: frozenset[str]
    framing_anchor_key: str | None


# ---------------------------------------------------------------------------
# Linear-time patterns (no nested quantifiers / ReDoS-safe)
# ---------------------------------------------------------------------------

_CRISIS_THRESHOLD = 0.80
_SENSITIVE_THRESHOLD = 0.48
_SEVERITY_RANK = {
    SafetySeverity.NONE: 0,
    SafetySeverity.BENIGN_HYPERBOLE: 1,
    SafetySeverity.GAMING_FICTION: 1,
    SafetySeverity.TROLL_ABUSE_BAIT: 2,
    SafetySeverity.SENSITIVE_CARE: 3,
    SafetySeverity.CRISIS_INTERVENTION: 4,
}

_OBFUSCATION_TRIGGER_RE = re.compile(
    r"[013457@$._*\-]|([aiou])\1|(?<![^\W\d_])[^\W\d_] [^\W\d_] [^\W\d_]"
)

_NEGATION_RE = re.compile(
    r"\b("
    r"(?:no me|nunca me|jamas me) (?:quiero|voy a) (?:matar|morir|suicidar)|"
    r"no quiero (?:morir|morirme|matarme|suicidarme)|"
    r"nunca me (?:mataria|suicidaria)|"
    r"(?:no|nunca|jamas) pienso (?:matarme|suicidarme|morir)|"
    r"no (?:estoy|me siento) suicida|"
    r"(?:not going to|not gonna|would never|never going to|never gonna|wouldnt|"
    r"wont|will not|never|dont want to|do not want to) (?:kill myself|end my life|commit suicide)|"
    r"(?:dont|do not) want to die|"
    r"(?:i am |im )?not suicidal"
    r")\b"
)

# Tier B: acute overwhelm / implicit distress / colloquial plea
_TIER_B_IMPLICIT_DISTRESS = re.compile(
    r"\b("
    r"ya no aguanto mas|ya no puedo mas|nada tiene sentido|estoy roto|estoy rota|"
    r"no sirvo para nada|todo seria mejor sin mi|"
    r"matenme|kill me now|just kill me|"
    r"i cant take this anymore|nothing matters anymore|i feel completely empty"
    r")\b"
)

# Tier C: subtle fatigue / mood downshift (never fires alone; contributes to trajectory)
_TIER_C_SUBTLE_FATIGUE = re.compile(
    r"\b("
    r"estoy agotado|estoy agotada|me siento muy vacio|me siento vacia|"
    r"sin fuerzas|no le veo sentido|estoy cansado de todo|im exhausted with everything"
    r")\b"
)

# Benign idioms that remain hyperbole without needing "jaja" / "lol"
_BENIGN_IDIOM_RE = re.compile(
    r"\b("
    r"me muero de (?:risa|cringe|verguenza|hambre|sueno|calor|frio|miedo)|"
    r"dying of laughter|dyin of cringe|dead from laughing|killed it"
    r")\b"
)

_LAUGHTER_DAMPENER_RE = re.compile(
    r"\b("
    r"a?(?:ja+j[ja]*|je+j[je]*|ha+h[ha]*|he+h[he]*)|"
    r"js+j[js]*|xd+|lo+l+|lmao+|lmfao|rofl|ded|cringe"
    r")\b|"
    r"[😂🤣💀]"
)

# Functional in-game character death / reset / round mechanics (distinguishes active
# gameplay from mere game mentions like "juego Elden Ring para no pensarlo")
_STRICT_INGAME_MECHANIC_RE = re.compile(
    r"\b("
    r"en el juego|en este juego|en esta partida|en la partida|en el run|"
    r"del juego|en el boss|el boss|para resetear|al checkpoint|checkpoint|"
    r"respawn|respawnear|spawn|wipe|wipear|"
    r"fallar ese tiro|ese tiro|after that miss|for the clip|"
    r"fase \d+|in game|this game|next round|reset the run|i died to|died to"
    r")\b"
)

# Broader gaming tokens valid only for third-person / non-explicit phrases
_GAMING_LOCATOR_RE = re.compile(
    r"\b("
    r"en el juego|en este juego|en esta partida|en la partida|en el run|"
    r"del juego|en el boss|el boss|para resetear|al checkpoint|checkpoint|"
    r"respawn|respawnear|spawn|wipe|wipear|gg|nerf|hitbox|clip|miss|missed|"
    r"fallar ese tiro|ese tiro|after that miss|partida|ranked|elo|boss|fase \d+|dungeon|"
    r"malenia|in game|this game|next round|reset the run|for the clip|i died to|died to"
    r")\b"
)

# Explicit non-idiomatic crisis expressions: never mitigated by laughter ("jaja"/"lol")
# or mere game-title mentions; only strict in-game reset mechanics can apply.
_VETO_LAUGHTER_AND_WEAK_GAMING_RE = re.compile(
    r"\b("
    r"suicid\w*|acabar con mi (?:propia )?vida|terminar con mi (?:propia )?vida|"
    r"quitar(?:me)? la vida|end my life|take my life|end it al{1,2}|"
    r"mejor sin mi|better of{1,2} without me|no despertar|"
    r"(?:ya )?no quiero vivir|(?:dont|do not) want to live|nothing (?:left )?to live for|"
    r"no vale la pena (?:vivir|seguir)|wish i (?:was|were) dead|disappear forever"
    r")\b"
)

# Explicit physical self-harm / suicidal ideation phrases: veto ALL dampeners unconditionally
_HARD_SELF_HARM_VETO_RE = re.compile(
    r"\b("
    r"ideas suicidas|suicidal thoughts|suicidal ideation|"
    r"hacerme dano|autolesion\w*|cortarme(?:\s+las\s+venas)?|cortar las venas|"
    r"cut(?:ting)? my\s*self|hurt(?:ing)? my\s*self|harm(?:ing)? my\s*self|"
    r"self\s*-?\s*harm\w*|slit(?:ting)? my wrists?"
    r")\b"
)

_THIRD_PERSON_OR_GAME_DEATH_RE = re.compile(
    r"\b("
    r"me mataron|el boss se suicida|se suicido el|los enemigos|"
    r"si muero pierdo|si muero en|cuando muero en|me mato el|"
    r"got killed|killed me in|boss kills itself|if i die i lose|i died to"
    r")\b"
)

_DARK_GAME_GENRES = frozenset({"horror", "souls-like", "soulslike", "psychological", "survival"})
_DARK_GAME_TITLES = frozenset({
    "elden ring",
    "phasmophobia",
    "cyberpunk 2077",
    "cyberpunk",
    "dead by daylight",
    "doki doki literature club",
    "doki doki",
    "silent hill",
})

# Serious intent / lethal preparation / isolation amplifiers (veto ALL dampeners)
_SERIOUS_AMPLIFIER_RE = re.compile(
    r"\b("
    r"en serio|de verdad|no es broma|no es chiste|hablo en serio|"
    r"no es del juego|fuera de broma|fuera del juego|"
    r"ya no puedo mas|ya no aguanto mas|estoy muy solo|estoy muy sola|estoy solo|estoy sola|"
    r"no tengo a nadie|nadie me escucha|ayuda por favor|por favor ayuda|"
    r"hoy mismo|esta noche|hoy en el stream|despues del stream|cuando termine el stream|"
    r"despues de (?:esta |la )?(?:partida|raid)|despues de este juego|"
    r"adios a todos|adios para siempre|perdon por todo|"
    r"mi despedida|ya escribi (?:mi|la) despedida|carta de despedida|ya escribi la carta|"
    r"compre las pastillas|tengo las pastillas|tome pastillas|las pastillas|"
    r"tengo la soga|una soga|una cuerda|cortarme las venas|cortar las venas|"
    r"seriously|not joking|no joke|for real|not about the game|"
    r"i need help|nobody cares|im all alone|all alone|tonight|"
    r"after stream|after the stream|after (?:this |the )?(?:raid|game|match)|"
    r"goodbye everyone|my goodbye note|suicide note|wrote a note|"
    r"bought the pills|got the pills|took the pills|slit my wrists"
    r")\b"
)

# Second-person abuse & third-person incitement (never self-harm crisis; withheld on chat)
_TROLL_SECOND_PERSON_RE = re.compile(
    r"\b("
    r"matate|suicidate|muerete|ojala te mueras|"
    r"(?:por\s*que|pq|xq)\s+no\s+te\s+(?:matas|suicidas|mueres)|"
    r"(?:ve|vete|anda)\s+a\s+(?:matarte|suicidarte|morirte|matar|suicidar)|"
    r"kys|kil{1,2} your\s*self|go kil{1,2} your\s*self|unalive your\s*self|"
    r"why\s+(?:dont|do\s+not)\s+you\s+(?:kil{1,2}\s+your\s*self|die|end\s+it)|"
    r"end your life|"
    r"dile al streamer que se mate|que se mate el streamer|que se suicide|"
    r"tell the streamer to kil{1,2} (?:him|her|them)self"
    r")\b"
)

# Explicit self-contained coercion frames (always bait even without 1st-person stem)
_DIRECT_COERCION_BAIT_RE = re.compile(
    r"\b("
    r"di\s+que\s+te\s+(?:quieres|vas\s+a)\s+(?:morir|matar|suicidar)|"
    r"say\s+(?:that\s+)?you\s+want\s+to\s+(?:die|kil{1,2}\s+your\s*self)"
    r")\b"
)

# Framing imperatives trying to get Kira to echo/translate a harmful payload
_FRAME_COERCION_BAIT_RE = re.compile(
    r"\b("
    r"(?:repite|repitelo|repeti)\s+conmigo\b(?:\s*[:,\-\"“”']|\s+)|"
    r"repeat\s+after\s+me\b(?:\s*[:,\-\"“”']|\s+)|"
    r"(?:di|dilo|pronuncia|escribe|lee)\s+(?:en\s+voz\s+alta\s+)?(?:esto|la\s+frase)\b(?:\s*[:,\-\"“”']|\s+)|"
    r"responde(?:\s+solo)?\s+con(?:\s+esto)?\b(?:\s*[:,\-\"“”']|\s+)|"
    r"reply(?:\s+only)?\s+with\b(?:\s*[:,\-\"“”']|\s+)|"
    r"traduce(?:\s+(?:al\s+[a-z]+|esto))?\s*[:\"“”'’«]|"
    r"translate(?:\s+(?:to|into)\s+[a-z]+|\s+this)?\s*[:\"“”'’«]"
    r")"
)
_QUOTE_COERCION_BAIT_RE = re.compile(
    r"(?<!\bme )(?<!\bte )(?<!\ble )(?<!\bse )(?<!\bnos )(?<!\bto )(?<!\bcan )(?<!\bi )"
    r"\b(?:di|dilo|pronuncia|escribe|lee|say)\s*[:\"“”'’«]"
)
_QUOTE_OR_COLON_CHARS = frozenset(":'\"“”’«")
_COERCION_BAIT_RE = _FRAME_COERCION_BAIT_RE


def _normalize_with_quotes(text: str) -> str:
    nfkc = unicodedata.normalize("NFKC", text).lower()
    stripped = "".join(
        ch
        for ch in unicodedata.normalize("NFD", nfkc)
        if unicodedata.category(ch) not in {"Mn", "Cf"}
    )
    return " ".join(stripped.split())


def parse_safety_severity(value: object) -> SafetySeverity:
    raw = getattr(value, "value", value)
    norm = str(raw or "none").strip().lower()
    for member in SafetySeverity:
        if member.value == norm:
            return member
    return SafetySeverity.NONE


def max_safety_severity(a: SafetySeverity, b: SafetySeverity) -> SafetySeverity:
    return a if _SEVERITY_RANK[a] >= _SEVERITY_RANK[b] else b


class ContextualCrisisArbiter:
    """Multi-signal contextual safety arbiter with zero ReDoS exposure."""

    def assess(
        self,
        text: str,
        *,
        source: str = TurnSource.DIRECT.value,
        stream_ctx: StreamContextMetadata | None = None,
        trajectory_delta: float = 0.0,
        hyperbole_repeat_delta: float = 0.0,
    ) -> CrisisAssessment:
        if not isinstance(text, str) or not text.strip():
            return self._build_assessment(
                SafetySeverity.NONE,
                source=source,
                signals=SignalBreakdown(
                    lexical_tier="NONE",
                    base_score=0.0,
                    laughter_dampener=0.0,
                    gaming_dampener=0.0,
                    serious_amplifier=0.0,
                    trajectory_delta=0.0,
                    final_score=0.0,
                    dampeners_vetoed=False,
                    dampeners_applied=(),
                    amplifiers_applied=(),
                ),
            )

        ctx = stream_ctx or StreamContextMetadata()
        norm = _normalize(text)
        eval_text = norm
        has_stem = has_explicit_crisis_stem(text)

        # Normalize obfuscated tokens once if obfuscation markers/non-ASCII exist
        if _OBFUSCATION_TRIGGER_RE.search(text) or any(ord(ch) > 127 for ch in text):
            obf = _normalize_obfuscated(text)
            if obf and obf != norm:
                if (
                    has_stem
                    or _TROLL_SECOND_PERSON_RE.search(obf)
                    or _DIRECT_COERCION_BAIT_RE.search(obf)
                    or _FRAME_COERCION_BAIT_RE.search(obf)
                    or _NEGATION_RE.search(obf)
                    or _SERIOUS_AMPLIFIER_RE.search(obf)
                    or _LAUGHTER_DAMPENER_RE.search(obf)
                ):
                    eval_text = obf

        # Collect serious distress / preparation amplifiers upfront
        amp_labels = tuple(dict.fromkeys(_SERIOUS_AMPLIFIER_RE.findall(eval_text)))
        has_serious_amp = len(amp_labels) > 0

        # Strip explicit negated phrasing before checking un-negated crisis stems
        has_negation = bool(_NEGATION_RE.search(eval_text))
        stripped_after_neg = _NEGATION_RE.sub(" ", eval_text) if has_negation else eval_text
        has_unnegated_stem = (
            has_explicit_crisis_stem(stripped_after_neg) if has_negation else has_stem
        )
        has_tier_b = bool(_TIER_B_IMPLICIT_DISTRESS.search(stripped_after_neg))

        # 0. Troll / second-person harassment / prompt-injection coercion bait check
        is_second_person_troll = bool(_TROLL_SECOND_PERSON_RE.search(eval_text))
        is_direct_coercion = bool(_DIRECT_COERCION_BAIT_RE.search(eval_text))
        has_harmful_payload = has_unnegated_stem or has_tier_b or is_second_person_troll
        is_quote_coercion = False
        if has_harmful_payload and any(ch in _QUOTE_OR_COLON_CHARS for ch in text):
            is_quote_coercion = bool(
                _QUOTE_COERCION_BAIT_RE.search(_normalize_with_quotes(text))
            )
        is_framed_coercion = bool(
            has_harmful_payload
            and (is_quote_coercion or _FRAME_COERCION_BAIT_RE.search(eval_text))
        )
        is_coercion_bait = is_direct_coercion or is_framed_coercion
        if (is_second_person_troll and not has_unnegated_stem) or (
            is_coercion_bait and not has_serious_amp
        ):
            troll_kind = (
                "coercion_quote_bait" if is_coercion_bait else "second_person_abuse"
            )
            signals = SignalBreakdown(
                lexical_tier="TROLL_OR_BAIT",
                base_score=0.0,
                laughter_dampener=0.0,
                gaming_dampener=0.0,
                serious_amplifier=0.0,
                trajectory_delta=0.0,
                final_score=0.0,
                dampeners_vetoed=False,
                dampeners_applied=(),
                amplifiers_applied=(),
                is_troll_or_bait=True,
                troll_kind=troll_kind,
            )
            return self._build_assessment(
                SafetySeverity.TROLL_ABUSE_BAIT, source=source, signals=signals
            )

        # 1. Explicit negation check unless overridden by un-negated crisis/Tier B/serious amp
        if (
            has_negation
            and not has_serious_amp
            and not has_unnegated_stem
            and not _TIER_B_IMPLICIT_DISTRESS.search(stripped_after_neg)
        ):
            signals = SignalBreakdown(
                lexical_tier="NEGATED",
                base_score=0.0,
                laughter_dampener=0.0,
                gaming_dampener=0.0,
                serious_amplifier=0.0,
                trajectory_delta=0.0,
                final_score=0.0,
                dampeners_vetoed=False,
                dampeners_applied=("explicit_negation",),
                amplifiers_applied=(),
            )
            return self._build_assessment(
                SafetySeverity.NONE, source=source, signals=signals
            )

        # 2. Determine lexical tier
        lexical_tier = "NONE"
        base_score = 0.0

        if _BENIGN_IDIOM_RE.search(stripped_after_neg) and not has_unnegated_stem:
            lexical_tier = "BENIGN_IDIOM"
            base_score = 0.10
        elif has_unnegated_stem:
            lexical_tier = "TIER_A_EXPLICIT"
            base_score = 0.85
        elif _THIRD_PERSON_OR_GAME_DEATH_RE.search(stripped_after_neg):
            lexical_tier = "GAMING_THIRD_PERSON"
            base_score = 0.18
        elif _TIER_B_IMPLICIT_DISTRESS.search(stripped_after_neg):
            lexical_tier = "TIER_B_IMPLICIT"
            base_score = 0.62
        elif _TIER_C_SUBTLE_FATIGUE.search(stripped_after_neg):
            lexical_tier = "TIER_C_SUBTLE"
            base_score = 0.35

        # 3. Serious amplifiers and explicit physical self-harm stems veto ALL dampeners
        has_hard_selfharm_veto = bool(_HARD_SELF_HARM_VETO_RE.search(stripped_after_neg))
        has_non_idiomatic_veto = bool(
            _VETO_LAUGHTER_AND_WEAK_GAMING_RE.search(stripped_after_neg)
        )
        dampeners_vetoed = has_serious_amp or has_hard_selfharm_veto
        serious_amplifier = min(0.25, 0.12 * len(amp_labels)) if has_serious_amp else 0.0

        # 4. Compute dampeners (only when not vetoed and a signal matched)
        dampeners_applied: list[str] = []
        laughter_dampener = 0.0
        gaming_dampener = 0.0

        has_laughter = bool(_LAUGHTER_DAMPENER_RE.search(eval_text))
        has_strict_game = bool(_STRICT_INGAME_MECHANIC_RE.search(eval_text))
        has_broad_game = bool(_GAMING_LOCATOR_RE.search(eval_text))
        is_dark_game = self._is_dark_game_context(ctx)

        if not dampeners_vetoed and base_score > 0.0:
            # Owner decision 2026-11-16 (option B): laughter or joke markers never
            # lower an explicit first-person self-harm statement (TIER_A) below crisis.
            if (
                has_laughter
                and not has_non_idiomatic_veto
                and lexical_tier != "TIER_A_EXPLICIT"
            ):
                laughter_dampener = 0.42
                dampeners_applied.append("laughter_or_slang")
            if lexical_tier == "TIER_A_EXPLICIT":
                if has_strict_game:
                    gaming_dampener = 0.45
                    dampeners_applied.append("in_game_locator")
            elif lexical_tier == "TIER_B_IMPLICIT":
                if has_strict_game or (has_broad_game and not has_non_idiomatic_veto):
                    gaming_dampener = 0.45
                    dampeners_applied.append("in_game_locator")
            elif has_broad_game:
                gaming_dampener = 0.45
                dampeners_applied.append("in_game_locator")
            elif is_dark_game and lexical_tier == "GAMING_THIRD_PERSON":
                gaming_dampener = 0.25
                dampeners_applied.append("dark_game_metadata")

        total_dampener = min(0.55, laughter_dampener + gaming_dampener)

        effective_trajectory = trajectory_delta
        if laughter_dampener > 0.0 and not dampeners_vetoed:
            effective_trajectory += hyperbole_repeat_delta

        # Safety floor: serious amplifier + any self-harm stem stays at CRISIS_INTERVENTION
        raw_final = base_score - total_dampener + serious_amplifier + effective_trajectory
        final_score = round(max(0.0, min(1.0, raw_final)), 4)

        # 5. Severity classification
        if final_score >= _CRISIS_THRESHOLD:
            severity = SafetySeverity.CRISIS_INTERVENTION
        elif final_score >= _SENSITIVE_THRESHOLD:
            severity = SafetySeverity.SENSITIVE_CARE
        elif lexical_tier == "GAMING_THIRD_PERSON" or gaming_dampener > 0.0:
            severity = SafetySeverity.GAMING_FICTION
        elif lexical_tier == "BENIGN_IDIOM" or laughter_dampener > 0.0:
            severity = SafetySeverity.BENIGN_HYPERBOLE
        else:
            severity = SafetySeverity.NONE

        amplifiers_applied: list[str] = []
        if has_serious_amp:
            amplifiers_applied.append("serious_distress")
        elif has_hard_selfharm_veto or has_non_idiomatic_veto:
            amplifiers_applied.append("explicit_plan_or_farewell")

        signals = SignalBreakdown(
            lexical_tier=lexical_tier,
            base_score=base_score,
            laughter_dampener=laughter_dampener,
            gaming_dampener=gaming_dampener,
            serious_amplifier=serious_amplifier,
            trajectory_delta=round(effective_trajectory, 4),
            final_score=final_score,
            dampeners_vetoed=dampeners_vetoed,
            dampeners_applied=tuple(dampeners_applied),
            # Store count/kind only rather than verbatim substrings
            amplifiers_applied=tuple(amplifiers_applied),
        )
        return self._build_assessment(severity, source=source, signals=signals)

    @staticmethod
    def _is_dark_game_context(ctx: StreamContextMetadata) -> bool:
        if ctx.active_game and ctx.active_game.strip().lower() in _DARK_GAME_TITLES:
            return True
        lower_genres = {g.strip().lower() for g in ctx.game_genres}
        return bool(lower_genres & _DARK_GAME_GENRES)

    @staticmethod
    def _build_assessment(
        severity: SafetySeverity, *, source: str, signals: SignalBreakdown
    ) -> CrisisAssessment:
        is_owner = source in OWNER_CRISIS_SOURCES
        is_chat = source == TurnSource.CHAT.value
        return CrisisAssessment(
            severity=severity,
            should_trigger_crisis_turn=(
                is_owner and severity == SafetySeverity.CRISIS_INTERVENTION
            ),
            should_strip_sarcasm=severity in {
                SafetySeverity.SENSITIVE_CARE,
                SafetySeverity.CRISIS_INTERVENTION,
            },
            should_withhold_chat_msg=(
                is_chat
                and severity in {
                    SafetySeverity.CRISIS_INTERVENTION,
                    SafetySeverity.TROLL_ABUSE_BAIT,
                    SafetySeverity.SENSITIVE_CARE,
                }
            ),
            bypass_llm_with_canned_reply=(
                is_owner and severity == SafetySeverity.CRISIS_INTERVENTION
            ),
            signals=signals,
        )


_DEFAULT_ARBITER = ContextualCrisisArbiter()


def assess_crisis_flag(
    text: str, *, context: object = None, source: str = TurnSource.DIRECT.value
) -> bool:
    """Fast module-level boolean wrapper backing ``crisis.detect_crisis``."""
    ctx = context if isinstance(context, StreamContextMetadata) else None
    assessment = _DEFAULT_ARBITER.assess(text, source=source, stream_ctx=ctx)
    return assessment.severity == SafetySeverity.CRISIS_INTERVENTION


# ---------------------------------------------------------------------------
# Multi-turn owner & privacy-preserving viewer trackers
# ---------------------------------------------------------------------------


class OwnerSafetyTrajectory:
    """Rolling in-memory window over the streamer's recent turns."""

    def __init__(self, *, window_size: int = 8, ttl_seconds: float = 900.0) -> None:
        self._window_size = window_size
        self._ttl_seconds = ttl_seconds
        self._history: deque[SafetyTurnRecord] = deque(maxlen=window_size)

    def _prune(self, now: float) -> None:
        while self._history and (now - self._history[0].timestamp_monotonic) > self._ttl_seconds:
            self._history.popleft()

    def pending_deltas(self, *, now: float | None = None) -> tuple[float, float]:
        ts = time.monotonic() if now is None else now
        self._prune(ts)
        distress_turns = sum(1 for r in self._history if r.had_distress_signal)
        hyperbole_turns = sum(1 for r in self._history if r.had_dampener_only)

        trajectory_delta = 0.0
        if distress_turns == 1:
            trajectory_delta = 0.12
        elif distress_turns >= 2:
            trajectory_delta = 0.25

        hyperbole_delta = 0.04 * min(3, hyperbole_turns)
        return trajectory_delta, hyperbole_delta

    def record(self, assessment: CrisisAssessment, *, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else now
        self._prune(ts)
        sig = assessment.signals
        had_distress = (
            sig.lexical_tier in {"TIER_A_EXPLICIT", "TIER_B_IMPLICIT", "TIER_C_SUBTLE"}
            and sig.final_score >= 0.30
            and not sig.dampeners_applied
        ) or assessment.severity in {
            SafetySeverity.SENSITIVE_CARE,
            SafetySeverity.CRISIS_INTERVENTION,
        }
        had_dampener_only = (
            assessment.severity == SafetySeverity.BENIGN_HYPERBOLE
            and sig.laughter_dampener > 0.0
        )

        # A calm turn gradually cools active distress history by clearing the oldest signal
        if (
            assessment.severity == SafetySeverity.NONE
            and sig.lexical_tier == "NONE"
            and self._history
        ):
            for idx, item in enumerate(self._history):
                if item.had_distress_signal:
                    del self._history[idx]
                    break

        self._history.append(
            SafetyTurnRecord(
                timestamp_monotonic=ts,
                severity=assessment.severity,
                effective_score=sig.final_score,
                had_distress_signal=had_distress,
                had_dampener_only=had_dampener_only,
            )
        )

    def clear(self) -> None:
        self._history.clear()


class ViewerCrisisTracker:
    """Privacy-preserving per-viewer safety tracker using ephemeral HMAC-SHA256.

    Never stores raw usernames, raw message text, or unkeyed SHA-256 hashes.
    """

    def __init__(
        self,
        *,
        max_users: int = 512,
        max_viewers: int | None = None,
        ttl_seconds: float = 600.0,
    ) -> None:
        cap = max(1, int(max_viewers if max_viewers is not None else max_users))
        self._ephemeral_key: bytes = os.urandom(32)
        self._max_users = cap
        self._ttl_seconds = float(ttl_seconds)
        # Stores only (timestamp, lexical_tier, final_score, was_hyperbole) tuples
        self._buckets: OrderedDict[bytes, deque[tuple[float, str, float, bool]]] = OrderedDict()
        self.counters: dict[str, int] = {
            "seen": 0,
            "withheld": 0,
            "troll_bait_withheld": 0,
            "care_flagged": 0,
            "gaming_allowed": 0,
            "hyperbole_allowed": 0,
        }

    @property
    def max_users(self) -> int:
        return self._max_users

    @property
    def max_viewers(self) -> int:
        return self._max_users

    def reset_ephemeral_key(self) -> None:
        self._ephemeral_key = os.urandom(32)
        self._buckets.clear()

    def _digest(self, username: str) -> bytes:
        normalized_user = (username or "anon").strip().lower().encode("utf-8")
        return hmac.new(self._ephemeral_key, normalized_user, hashlib.sha256).digest()

    def _prune_bucket(
        self, key: bytes, now: float
    ) -> deque[tuple[float, str, float, bool]]:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = deque(maxlen=6)
            self._buckets[key] = bucket
        while bucket and (now - bucket[0][0]) > self._ttl_seconds:
            bucket.popleft()
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_users:
            self._buckets.popitem(last=False)
        return bucket

    def evaluate_chat_message(
        self,
        username: str,
        raw_text: str,
        *,
        arbiter: ContextualCrisisArbiter | None = None,
        stream_ctx: StreamContextMetadata | None = None,
        now: float | None = None,
        logger: logging.Logger | None = None,
    ) -> CrisisAssessment:
        active_arbiter = arbiter or _DEFAULT_ARBITER
        ts = time.monotonic() if now is None else float(now)
        self.counters["seen"] += 1
        digest = self._digest(username)
        bucket = self._prune_bucket(digest, ts)

        prior_distress = sum(
            1 for (_, tier, score, is_hyp) in bucket
            if not is_hyp and tier != "NONE" and score >= 0.30
        )
        prior_hyperbole = sum(
            1 for (_, tier, _, is_hyp) in bucket
            if is_hyp and tier in {"TIER_A_EXPLICIT", "TIER_B_IMPLICIT"}
        )
        traj_delta = 0.12 if prior_distress == 1 else (0.25 if prior_distress >= 2 else 0.0)
        hyperbole_delta = 0.04 * min(3, prior_hyperbole)

        assessment = active_arbiter.assess(
            raw_text,
            source=TurnSource.CHAT.value,
            stream_ctx=stream_ctx,
            trajectory_delta=traj_delta,
            hyperbole_repeat_delta=hyperbole_delta,
        )
        is_hyperbole_only = (
            assessment.severity == SafetySeverity.BENIGN_HYPERBOLE
            and assessment.signals.laughter_dampener > 0.0
        )
        bucket.append(
            (
                ts,
                assessment.signals.lexical_tier,
                assessment.signals.final_score,
                is_hyperbole_only,
            )
        )

        if assessment.should_withhold_chat_msg:
            if assessment.severity == SafetySeverity.TROLL_ABUSE_BAIT:
                self.counters["troll_bait_withheld"] += 1
                if logger is not None:
                    logger.info(
                        "[CHAT_TROLL_BAIT] withheld=%d kind=%s",
                        self.counters["troll_bait_withheld"],
                        assessment.signals.troll_kind,
                    )
            else:
                self.counters["withheld"] += 1
                if assessment.severity == SafetySeverity.SENSITIVE_CARE:
                    self.counters["care_flagged"] += 1
                if logger is not None:
                    logger.info(
                        "[CHAT_CRISIS] withheld=%d tier=%s score=%.2f",
                        self.counters["withheld"],
                        assessment.signals.lexical_tier,
                        assessment.signals.final_score,
                    )
        elif assessment.severity == SafetySeverity.GAMING_FICTION:
            self.counters["gaming_allowed"] += 1
        elif assessment.severity == SafetySeverity.BENIGN_HYPERBOLE:
            self.counters["hyperbole_allowed"] += 1

        return assessment


# ---------------------------------------------------------------------------
# Prompt precedence & sarcasm stripping
# ---------------------------------------------------------------------------

CARE_RECENCY_OVERRIDE = (
    "[CRITICAL CARE MODE: The user may be in emotional distress. Drop all sarcasm, "
    "roasting, and dark humor immediately. Respond with grounded, brief warmth.]"
)
CARE_RECENCY_OVERRIDE_ES = (
    "[MODO CUIDADO CRÍTICO: La persona puede estar pasando por un momento difícil. "
    "Mantén calidez breve, calma, escucha serena y absoluto respeto en todo momento.]"
)

_SARCASM_LINE_RE = re.compile(
    r"\b(?:"
    r"sarcas[mt]\w*|acid wit|dark humor|dark jokes?|gallows humor|humor negro|"
    r"humor acido|humor ácido|mordaz|ironic\w*|ironi[ac]|ironía|roast\w*|burla\w*|"
    r"sarcastico|sarcastica|sarcástico|sarcástica|picante|edgy|afilad[oa]|"
    r"acida|ácida|acido|ácido|snark\w*|teasing|mock(?:ing)?|cynic(?:al)?|"
    r"cinic[oa]|cínic[oa]|biting|banter|provoca(?:r|dor|dora|tiv[oa])|"
    r"sin filtros?|mofa|caustic|sassy"
    r")\b",
    re.IGNORECASE,
)


def strip_sarcasm_directives(text: str) -> str:
    """Remove lines containing persona sarcasm/roast/dark-humor instructions."""
    if not text:
        return ""
    kept_lines = [
        line for line in text.splitlines() if not _SARCASM_LINE_RE.search(line)
    ]
    return "\n".join(kept_lines).strip()


def care_recency_line(locale: str = "es") -> str:
    return CARE_RECENCY_OVERRIDE if str(locale).lower().startswith("en") else CARE_RECENCY_OVERRIDE_ES


def care_style_fallback(locale: str = "es") -> str:
    if str(locale).lower().startswith("en"):
        return "Warm, grounded, calm, and brief empathetic tone."
    return "Tono cercano, sereno, breve y respetuoso."


def assemble_safety_aware_system_prompt(
    *,
    base_system_prompt: str,
    core_rules: str,
    warmth_block: str,
    assessment: CrisisAssessment,
) -> tuple[str, str | None]:
    """Assemble system prompt with strict safety > persona precedence.

    - CRISIS_INTERVENTION (owner channels): bypasses LLM with canned ``i18n_active.crisis_reply()``.
    - SENSITIVE_CARE: strips sarcasm/dark-humor/roast directives before assembly
      and appends ``CARE_RECENCY_OVERRIDE``.
    - Other severities: returns parts untouched.
    """
    if assessment.bypass_llm_with_canned_reply:
        return ("", i18n_active.crisis_reply())

    if assessment.should_strip_sarcasm:
        clean_base = strip_sarcasm_directives(base_system_prompt)
        clean_rules = strip_sarcasm_directives(core_rules)
        clean_warmth = strip_sarcasm_directives(warmth_block)
        parts = [p for p in (clean_base, clean_rules, clean_warmth, CARE_RECENCY_OVERRIDE) if p]
        return ("\n\n".join(parts), None)

    parts = [p for p in (base_system_prompt, core_rules, warmth_block) if p]
    return ("\n\n".join(parts), None)


# ---------------------------------------------------------------------------
# Agenda Move Deck safety & 9B nihilistic-drift prevention
# ---------------------------------------------------------------------------

_EXISTENTIAL_TOPIC_RE = re.compile(
    r"\b("
    r"internet muerto|dead internet|simulacion|universo nos controla|"
    r"sentido de la vida|libre albedrio|nada importa|nihilismo|extincion|"
    r"control mental|realidad falsa"
    r")\b"
)

_VULNERABLE_TOPIC_RE = re.compile(
    r"\b(soledad|depresion|duelo|ansiedad|burnout|trauma|crisis personal)\b"
)

_GROUNDING_ANCHORS = {
    TopicSafetyTier.STANDARD: None,
    TopicSafetyTier.EXISTENTIAL_DARK: "safety.agenda.grounding.existential_playful",
    TopicSafetyTier.VULNERABLE_PERSONAL: "safety.agenda.grounding.vulnerable_care",
}

_GROUNDING_TEXT_ES = {
    TopicSafetyTier.EXISTENTIAL_DARK: (
        "ANCLA DE SEGURIDAD DEL TEMA: Mantén el debate en un plano curioso, lúdico y con los pies en la tierra; "
        "nunca afirmes que la vida carece de sentido ni adoptes fatalismo o nihilismo."
    ),
    TopicSafetyTier.VULNERABLE_PERSONAL: (
        "ANCLA DE CUIDADO DEL TEMA: Trata este tema con empatía práctica y calma, "
        "evita dilemas extremos o burlas y nunca invalides la experiencia emocional."
    ),
}

_GROUNDING_TEXT_EN = {
    TopicSafetyTier.EXISTENTIAL_DARK: (
        "TOPIC SAFETY ANCHOR: Keep the discussion curious, playful, and grounded; "
        "never claim life is meaningless or drift into fatalism or nihilism."
    ),
    TopicSafetyTier.VULNERABLE_PERSONAL: (
        "TOPIC CARE ANCHOR: Approach this topic with calm, practical empathy; "
        "avoid extreme dilemmas, teasing, or dismissing emotional strain."
    ),
}

_EXTREME_FLAT_CARD_RE = re.compile(
    r"(extremo|dilema|si todo fuera falso|nadie fuera real|fin del mundo|extreme|dilemma)",
    re.IGNORECASE,
)

_NIHILISTIC_DRIFT_RE = re.compile(
    r"\b("
    r"ninguno de nosotros es real|nada tiene ningun sentido|no importa si vives|"
    r"deberias rendirte|todos deberiamos desaparecer|mejor dejar de existir|"
    r"vivir no sirve de nada|la vida no vale nada|"
    r"none of us are real|nothing matters at all|might as well end it|"
    r"life is completely meaningless|no point in living"
    r")\b"
)


def classify_topic_safety(
    topic_title: str,
    *,
    last_owner_severity: SafetySeverity = SafetySeverity.NONE,
) -> TopicSafetyProfile:
    norm = _normalize(topic_title or "")
    if last_owner_severity in {
        SafetySeverity.SENSITIVE_CARE,
        SafetySeverity.CRISIS_INTERVENTION,
    } or _VULNERABLE_TOPIC_RE.search(norm):
        return TopicSafetyProfile(
            tier=TopicSafetyTier.VULNERABLE_PERSONAL,
            blocked_families=frozenset(
                {"extreme_case", "moral_dilemma", "absurd_extension", "inversion"}
            ),
            framing_anchor_key=_GROUNDING_ANCHORS[TopicSafetyTier.VULNERABLE_PERSONAL],
        )
    if _EXISTENTIAL_TOPIC_RE.search(norm):
        return TopicSafetyProfile(
            tier=TopicSafetyTier.EXISTENTIAL_DARK,
            blocked_families=frozenset({"extreme_case", "moral_dilemma"}),
            framing_anchor_key=_GROUNDING_ANCHORS[TopicSafetyTier.EXISTENTIAL_DARK],
        )
    return TopicSafetyProfile(
        tier=TopicSafetyTier.STANDARD,
        blocked_families=frozenset(),
        framing_anchor_key=None,
    )


def filter_safe_deck_moves(
    moves: tuple[str, ...],
    *,
    topic_profile: TopicSafetyProfile,
    move_family_map: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Deterministically exclude unsafe cards while preserving order (prefetch == live)."""
    if not topic_profile.blocked_families or not moves:
        return moves
    if move_family_map is not None:
        filtered = tuple(
            m for m in moves if move_family_map.get(m, "") not in topic_profile.blocked_families
        )
    else:
        filtered = tuple(m for m in moves if not _EXTREME_FLAT_CARD_RE.search(m))
    return filtered or moves


def filter_safe_family_moves(
    family_moves: Mapping[str, tuple[str, ...]],
    *,
    topic_profile: TopicSafetyProfile,
) -> dict[str, tuple[str, ...]]:
    """Deterministically exclude unsafe families while keeping non-empty fallback."""
    if not topic_profile.blocked_families or not family_moves:
        return dict(family_moves)
    filtered = {
        fam: cards
        for fam, cards in family_moves.items()
        if fam not in topic_profile.blocked_families and cards
    }
    return filtered or dict(family_moves)


def grounding_line_for_profile(topic_profile: TopicSafetyProfile, locale: str = "es") -> str:
    if topic_profile.tier == TopicSafetyTier.STANDARD:
        return ""
    table = _GROUNDING_TEXT_EN if str(locale).lower().startswith("en") else _GROUNDING_TEXT_ES
    return table.get(topic_profile.tier, "")


def detect_nihilistic_drift(response_text: str) -> tuple[bool, str]:
    """Detect fatalistic/nihilistic 9B output drift while allowing playful sci-fi banter."""
    norm = _normalize(response_text or "")
    if _NIHILISTIC_DRIFT_RE.search(norm):
        return False, "nihilistic_drift_guard"
    assessment = _DEFAULT_ARBITER.assess(response_text or "", source=TurnSource.KIRA_AGENDA.value)
    if assessment.severity in {
        SafetySeverity.SENSITIVE_CARE,
        SafetySeverity.CRISIS_INTERVENTION,
    }:
        return False, "nihilistic_drift_guard"
    return True, ""


# ---------------------------------------------------------------------------
# End-to-end owner turn facade
# ---------------------------------------------------------------------------


class SafetyTurnEvaluator:
    def __init__(self) -> None:
        self.arbiter = ContextualCrisisArbiter()
        self.owner_trajectory = OwnerSafetyTrajectory()
        self.viewer_tracker = ViewerCrisisTracker()

    def evaluate_owner_turn(
        self,
        text: str,
        *,
        source: str = TurnSource.DIRECT.value,
        stream_ctx: StreamContextMetadata | None = None,
        now: float | None = None,
    ) -> CrisisAssessment:
        traj_delta, hyp_delta = self.owner_trajectory.pending_deltas(now=now)
        assessment = self.arbiter.assess(
            text,
            source=source,
            stream_ctx=stream_ctx,
            trajectory_delta=traj_delta,
            hyperbole_repeat_delta=hyp_delta,
        )
        self.owner_trajectory.record(assessment, now=now)
        return assessment
