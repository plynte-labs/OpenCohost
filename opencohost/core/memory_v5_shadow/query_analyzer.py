"""
Episodic Query Analyzer for Memory v5.

Analyzes current user requests to derive:
- Recall intent: EXPLICIT, IMPLICIT, or NONE
- Retrieval scope: EPISODIC_TOPIC, SESSION_RECALL, or PROFILE_SYNTHESIS
- Relative AND absolute temporal constraints (today, yesterday, last week,
  N days/weeks ago, absolute calendar day, calendar month, etc.)
- Extracted lexical anchors (accent-folded)
- Normalized query text suitable for embedding

Deterministic by construction: accent/case normalization plus verb-family
stems and memory/session/temporal cue sets. There is deliberately NO
unbounded exact-sentence regex list — coverage comes from small closed
families, so a new conjugation is a stem hit, not a new pattern.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Optional


def _fold(text: str) -> str:
    """Lowercase + accent-fold for deterministic cue matching."""
    norm = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in norm if unicodedata.category(c) != "Mn").lower()


class RecallIntent(str, Enum):
    EXPLICIT = "EXPLICIT"
    IMPLICIT = "IMPLICIT"
    NONE = "NONE"


class RecallScope(str, Enum):
    EPISODIC_TOPIC = "EPISODIC_TOPIC"
    SESSION_RECALL = "SESSION_RECALL"
    PROFILE_SYNTHESIS = "PROFILE_SYNTHESIS"


class TemporalConstraintType(str, Enum):
    TODAY = "TODAY"
    YESTERDAY = "YESTERDAY"
    LAST_WEEK = "LAST_WEEK"
    N_DAYS_AGO = "N_DAYS_AGO"
    N_WEEKS_AGO = "N_WEEKS_AGO"
    LAST_TIME = "LAST_TIME"
    ABSOLUTE_DAY = "ABSOLUTE_DAY"
    MONTH = "MONTH"


@dataclass(frozen=True)
class TemporalConstraint:
    constraint_type: TemporalConstraintType
    n_value: int = 0
    month: int = 0
    day: int = 0

    def matches(self, episode_started_at: str, reference_time: Optional[datetime] = None) -> bool:
        if reference_time is None:
            reference_time = datetime.now(timezone.utc)
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)

        try:
            clean = episode_started_at.replace("Z", "+00:00")
            ep_dt = datetime.fromisoformat(clean)
            if ep_dt.tzinfo is None:
                ep_dt = ep_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return False

        ep_date = ep_dt.date()
        ref_date = reference_time.date()

        if self.constraint_type == TemporalConstraintType.TODAY:
            return ep_date == ref_date

        elif self.constraint_type == TemporalConstraintType.YESTERDAY:
            return ep_date == (ref_date - timedelta(days=1))

        elif self.constraint_type == TemporalConstraintType.LAST_WEEK:
            # Calendar week preceding reference_time
            # Monday of current week
            current_week_start = ref_date - timedelta(days=ref_date.weekday())
            last_week_start = current_week_start - timedelta(days=7)
            last_week_end = current_week_start
            return last_week_start <= ep_date < last_week_end

        elif self.constraint_type == TemporalConstraintType.N_WEEKS_AGO:
            # Target week n_value weeks ago
            delta_days = (reference_time - ep_dt).total_seconds() / 86400.0
            target_days = self.n_value * 7
            # Match within ±3.5 days of target_days
            return abs(delta_days - target_days) <= 3.5

        elif self.constraint_type == TemporalConstraintType.N_DAYS_AGO:
            delta_days = (ref_date - ep_date).days
            return delta_days == self.n_value

        elif self.constraint_type == TemporalConstraintType.ABSOLUTE_DAY:
            # Calendar day; year is always the reference year. month == 0
            # means "day N of the reference month" (`el dia 2`).
            try:
                target = date(
                    ref_date.year,
                    self.month or reference_time.month,
                    self.day,
                )
            except Exception:
                return False
            return ep_date == target

        elif self.constraint_type == TemporalConstraintType.MONTH:
            # Calendar month of the reference year (`en septiembre`).
            return ep_date.year == ref_date.year and ep_date.month == self.month

        return True


@dataclass(frozen=True)
class EpisodicQuery:
    raw_text: str
    normalized_query: str
    recall_intent: RecallIntent
    temporal_constraint: Optional[TemporalConstraint]
    lexical_anchors: list[str]
    profile_id: str
    scope: RecallScope = RecallScope.EPISODIC_TOPIC


# Deterministic recall features on folded (accent-free, lowercase) text.
# Verb families, not sentences: any conjugation carrying the stem counts.
# Kept deliberately narrow — corroboration downstream rejects unanchored
# false positives, but intent should not fire on topical turns.
_RECALL_VERB_STEMS = (
    # recordar / acordarse (all persons and tenses)
    r"record", r"recuerd", r"acord", r"acuerd",
)
_CONVERSATION_VERB_STEMS = (
    # hablar / discutir / platicar / conversar / charlar / comentar /
    # mencionar / opinar / pensar / concluir / decir / decidir / estudiar
    r"habl", r"discut", r"platic", r"convers", r"charl", r"coment",
    r"mencion", r"opina", r"pens", r"conclu", r"dij", r"dich", r"decid",
    r"estudi", r"vist",
)
# "tocamos el tema" and morphological siblings — the bare verb `tocar`
# alone is too broad (topical "me toca"), so it only counts with `tema`.
_TOCAR_TEMA_RE = re.compile(r"\btoc\w*\s+el\s+tema\b")
# Memory / session nouns that frame a recall request.
_MEMORY_NOUNS = (r"memoria", r"recuerdo", r"registr")
_SESSION_NOUNS = (r"sesion", r"conversacion", r"charla", r"stream")
# Past markers that turn a session noun into a previous-session reference.
_PAST_MARKERS = (r"pasad", r"anterior", r"previ", r"ultim", r"otra\s+vez")
# Profile-identity cues -> PROFILE_SYNTHESIS scope (complete regexes).
_PROFILE_PATTERNS = (
    r"\bdedic\w*",
    r"\bhobb\w*",
    r"\binteres\w*",
    r"\bsabes\s+de\s+mi\b",
    r"\bsabes\s+sobre\s+mi\b",
    r"\brecuerdas\s+de\s+mi\b",
    r"\brecordas\s+de\s+mi\b",
    r"\bquien\s+soy\b",
    r"\bmi\s+perfil\b",
)

_EXPLICIT_PATTERNS = [
    r"\b(te acuerdas|recuerdas|te acord[aá]s)\b",
    r"\bqu[eé] hab[ií]amos (dicho|hablado|decidido|visto|concluido)\b",
    r"\bde qu[eé] hablamos\b",
    r"\bte coment[eé]\b",
    r"\bhablamos (de|sobre)\b",
    r"\bdo you remember\b",
    r"\bwhat did we (say|talk|discuss|decide)\b",
    r"\bdid i mention\b",
]

_IMPLICIT_PATTERNS = [
    r"\bvolv[ií] a (probar|ver|checar|intentar)\b",
    r"\bsigue (igual|fallando|con el mismo|sin)\b",
    r"\blo que (vimos|platicamos|comentamos|hablamos|decidimos)\b",
    r"\baquello que\b",
    r"\baquella conversaci[oó]n\b",
    r"\bla [uú]ltima vez\b",
    r"\bcomo te dec[ií]a\b",
    r"\bcomo comentamos\b",
]

_SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_MONTH_ALTERNATION = "|".join(sorted(_SPANISH_MONTHS, key=len, reverse=True))

_SPANISH_NUMBER_WORDS = {
    "un": 1,
    "una": 1,
    "uno": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
}

_STOPWORDS = {
    "a", "al", "algo", "algunas", "algunos", "ante", "antes", "como", "con", "contra",
    "acordas", "acordás", "acuerdas", "acuerdo", "recuerdas", "recuerdo", "hablamos", "hablado", "dicho", "dijiste", "dije", "remember",
    "cual", "cuando", "de", "del", "desde", "donde", "durante", "e", "el", "ella",
    "ellas", "ellos", "en", "entre", "era", "erais", "eran", "eras", "eres", "es",
    "esa", "esas", "ese", "eso", "esos", "esta", "estaba", "estabais", "estaban",
    "estabas", "estad", "estada", "estadas", "estado", "estados", "estamos", "estando",
    "estar", "estaremos", "estará", "estarán", "estarás", "estaré", "estaréis",
    "estaría", "estaríais", "estaríamos", "estarían", "estarías", "estas", "este",
    "estemos", "esto", "estos", "estoy", "estuve", "estuviera", "estuvierais",
    "estuvieran", "estuvieras", "estuvieron", "estuviese", "estuvieseis", "estuviesen",
    "estuvieses", "estuvimos", "estuviste", "estuvisteis", "estuvo", "fue", "fuera",
    "fueran", "fueras", "fueron", "fuese", "fuesen", "fui", "fuimos", "fuiste", "ha",
    "habida", "habidas", "habido", "habidos", "habiendo", "habremos", "habrá", "habrán",
    "habrás", "habré", "habréis", "habría", "habríais", "habríamos", "habrían",
    "habrías", "habéis", "había", "habíais", "habíamos", "habían", "habías", "han",
    "has", "hasta", "hay", "haya", "hayamos", "hayan", "hayas", "hayáis", "he", "hemos",
    "hube", "hubiera", "hubierais", "hubieran", "hubieras", "hubieron", "hubiese",
    "hubieseis", "hubiesen", "hubieses", "hubimos", "hubiste", "hubisteis", "hubo",
    "la", "las", "le", "les", "lo", "los", "me", "mi", "mis", "mucho", "muchos", "muy",
    "más", "mí", "mía", "mías", "mío", "míos", "nada", "ni", "no", "nos", "nosotras",
    "nosotros", "nuestra", "nuestras", "nuestro", "nuestros", "o", "os", "otra", "otras",
    "otro", "otros", "para", "pero", "poco", "por", "porque", "que", "quien", "quienes",
    "qué", "se", "sea", "seamos", "sean", "seas", "sentid", "sentida", "sentidas",
    "sentido", "sentidos", "siente", "sintiendo", "sobre", "sois", "somos", "son",
    "soy", "su", "sus", "suya", "suyas", "suyo", "suyos", "sí", "también", "tanto",
    "te", "tendremos", "tendrá", "tendrán", "tendrás", "tendré", "tendréis", "tendría",
    "tendríais", "tendríamos", "tendrían", "tendrías", "tened", "tenemos", "tenga",
    "tengamos", "tengan", "tengas", "tengo", "tengáis", "tenida", "tenidas", "tenido",
    "tenidos", "teniendo", "tenéis", "tenía", "teníais", "teníamos", "tenían", "tenías",
    "ti", "tiene", "tienen", "tienes", "todo", "todos", "tu", "tus", "tuve", "tuviera",
    "tuvierais", "tuvieran", "tuvieras", "tuvieron", "tuviese", "tuvieseis", "tuviesen",
    "tuvieses", "tuvimos", "tuviste", "tuvisteis", "tuvo", "tuya", "tuyas", "tuyo",
    "tuyos", "tú", "un", "una", "unas", "uno", "unos", "vosotras", "vosotros", "vuestra",
    "vuestras", "vuestro", "vuestros", "y", "ya", "yo",
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "up", "about", "into", "over", "after", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "should", "can", "could", "may", "might", "must",
}


class EpisodicQueryAnalyzer:
    def __init__(self) -> None:
        pass

    def _has_family_hit(self, folded: str, stems: tuple[str, ...]) -> bool:
        return any(re.search(r"\b%s\w*" % stem, folded) for stem in stems)

    def _has_session_past_reference(self, folded: str) -> bool:
        # `la otra vez` is a standalone previous-session reference
        # (`la ultima vez` is not — it stays episode-level LAST_TIME).
        if re.search(r"\bla otra vez\b", folded):
            return True
        nouns = "|".join(_SESSION_NOUNS)
        past = "|".join(_PAST_MARKERS)
        if re.search(r"\b(?:%s)\w*\s+(?:%s)\w*" % (nouns, past), folded):
            return True
        if re.search(r"\b(?:%s)\w*\s+(?:%s)\w*" % (past, nouns), folded):
            return True
        return False

    def parse_temporal_constraint(
        self, text: str, reference_time: Optional[datetime] = None
    ) -> Optional[TemporalConstraint]:
        lower = text.lower()
        folded = _fold(text)

        # Hoy / Today
        if re.search(r"\b(hoy|today)\b", lower):
            return TemporalConstraint(constraint_type=TemporalConstraintType.TODAY)

        # Ayer / Yesterday
        if re.search(r"\b(ayer|yesterday)\b", lower):
            return TemporalConstraint(constraint_type=TemporalConstraintType.YESTERDAY)

        # La semana pasada / Last week
        if re.search(r"\b(la semana pasada|last week)\b", lower):
            return TemporalConstraint(constraint_type=TemporalConstraintType.LAST_WEEK)

        # Hace N semanas / N weeks ago
        m_weeks = re.search(r"\bhace\s+(\w+|\d+)\s+semanas?\b|\b(\w+|\d+)\s+weeks?\s+ago\b", lower)
        if m_weeks:
            raw_n = m_weeks.group(1) or m_weeks.group(2)
            n_val = _SPANISH_NUMBER_WORDS.get(raw_n)
            if n_val is None:
                try:
                    n_val = int(raw_n)
                except ValueError:
                    n_val = 1
            return TemporalConstraint(constraint_type=TemporalConstraintType.N_WEEKS_AGO, n_value=n_val)

        # Hace N días / N days ago
        m_days = re.search(r"\bhace\s+(\w+|\d+)\s+d[ií]as?\b|\b(\w+|\d+)\s+days?\s+ago\b", lower)
        if m_days:
            raw_n = m_days.group(1) or m_days.group(2)
            n_val = _SPANISH_NUMBER_WORDS.get(raw_n)
            if n_val is None:
                try:
                    n_val = int(raw_n)
                except ValueError:
                    n_val = 1
            return TemporalConstraint(constraint_type=TemporalConstraintType.N_DAYS_AGO, n_value=n_val)

        # La última vez / Last time / La otra vez
        if re.search(r"\b(la [uú]ltima vez|la otra vez|last time)\b", lower):
            return TemporalConstraint(constraint_type=TemporalConstraintType.LAST_TIME)

        # Absolute calendar day: `el 2 de septiembre`, `del dia 2 de
        # septiembre`, `2 de septiembre`. Year is always the reference year.
        m_abs = re.search(
            r"\b(?:el|del)?\s*(?:dia\s+)?(\d{1,2})\s+de\s+(%s)\b" % _MONTH_ALTERNATION,
            folded,
        )
        if m_abs:
            try:
                day = int(m_abs.group(1))
            except ValueError:
                day = 0
            month = _SPANISH_MONTHS.get(m_abs.group(2), 0)
            if 1 <= day <= 31 and month:
                return TemporalConstraint(
                    constraint_type=TemporalConstraintType.ABSOLUTE_DAY,
                    month=month,
                    day=day,
                )

        # Bare day of the reference month: `el dia 2`.
        m_day = re.search(r"\b(?:el|del)\s+dia\s+(\d{1,2})\b", folded)
        if m_day:
            try:
                day = int(m_day.group(1))
            except ValueError:
                day = 0
            if 1 <= day <= 31:
                return TemporalConstraint(
                    constraint_type=TemporalConstraintType.ABSOLUTE_DAY,
                    month=0,
                    day=day,
                )

        # Calendar month of the reference year: `en septiembre`.
        m_month = re.search(r"\ben\s+(%s)\b" % _MONTH_ALTERNATION, folded)
        if m_month:
            month = _SPANISH_MONTHS.get(m_month.group(1), 0)
            if month:
                return TemporalConstraint(
                    constraint_type=TemporalConstraintType.MONTH, month=month
                )

        return None

    def extract_lexical_anchors(self, text: str) -> list[str]:
        # Tokenize words (2+ chars so short content tokens like `ia` survive),
        # accent-folded so `audifonos` matches `audífonos` downstream.
        words = re.findall(r"\b[a-zA-ZáéíóúÁÉÍÓÚñÑ0-9_-]{2,}\b", _fold(text))
        anchors = [w for w in words if w not in _STOPWORDS]
        # De-duplicate while preserving order
        seen = set()
        out = []
        for w in anchors:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return out

    def _detect_scope(self, folded: str) -> RecallScope:
        if any(re.search(pat, folded) for pat in _PROFILE_PATTERNS):
            return RecallScope.PROFILE_SYNTHESIS
        nouns = "|".join(_SESSION_NOUNS)
        has_session_noun = re.search(r"\b(?:%s)\w*" % nouns, folded) is not None
        if self._has_session_past_reference(folded):
            return RecallScope.SESSION_RECALL
        if has_session_noun and (
            re.search(r"\b\d{1,2}\s+de\s+(?:%s)\b" % _MONTH_ALTERNATION, folded)
            or re.search(r"\ben\s+(?:%s)\b" % _MONTH_ALTERNATION, folded)
        ):
            # `las sesiones del 2 de septiembre` — session scope by date.
            return RecallScope.SESSION_RECALL
        return RecallScope.EPISODIC_TOPIC

    def _is_explicit_recall(self, text: str, folded: str) -> bool:
        if any(re.search(pat, text, flags=re.IGNORECASE) for pat in _EXPLICIT_PATTERNS):
            return True
        if self._has_family_hit(folded, _RECALL_VERB_STEMS):
            return True
        if self._has_family_hit(folded, _CONVERSATION_VERB_STEMS):
            return True
        if _TOCAR_TEMA_RE.search(folded):
            return True
        if self._has_family_hit(folded, _MEMORY_NOUNS):
            return True
        if self._has_session_past_reference(folded):
            return True
        return False

    def analyze(
        self,
        text: str,
        profile_id: str,
        reference_time: Optional[datetime] = None,
    ) -> EpisodicQuery:
        folded = _fold(text)
        temporal = self.parse_temporal_constraint(text, reference_time)
        lexical = self.extract_lexical_anchors(text)
        scope = self._detect_scope(folded)

        # Check explicit patterns (legacy list + verb families + cues)
        is_explicit = self._is_explicit_recall(text, folded)
        is_implicit = any(re.search(pat, text, flags=re.IGNORECASE) for pat in _IMPLICIT_PATTERNS)

        if is_explicit:
            intent = RecallIntent.EXPLICIT
        elif is_implicit or (temporal is not None and len(lexical) > 0):
            intent = RecallIntent.IMPLICIT
        else:
            intent = RecallIntent.NONE

        # Clean query text for embedding: strip recall prefix punctuation
        cleaned = re.sub(r"^[¿?¡!\s]+|[¿?¡!\s]+$", "", text).strip()

        return EpisodicQuery(
            raw_text=text,
            normalized_query=cleaned,
            recall_intent=intent,
            temporal_constraint=temporal,
            lexical_anchors=lexical,
            profile_id=profile_id,
            scope=scope,
        )
