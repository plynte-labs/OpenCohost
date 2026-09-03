"""
Episodic Query Analyzer for Memory v5.

Analyzes current user requests to derive:
- Recall intent: EXPLICIT, IMPLICIT, or NONE
- Relative temporal constraints (today, yesterday, last week, N days/weeks ago, etc.)
- Extracted lexical anchors
- Normalized query text suitable for embedding
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Optional


class RecallIntent(str, Enum):
    EXPLICIT = "EXPLICIT"
    IMPLICIT = "IMPLICIT"
    NONE = "NONE"


class TemporalConstraintType(str, Enum):
    TODAY = "TODAY"
    YESTERDAY = "YESTERDAY"
    LAST_WEEK = "LAST_WEEK"
    N_DAYS_AGO = "N_DAYS_AGO"
    N_WEEKS_AGO = "N_WEEKS_AGO"
    LAST_TIME = "LAST_TIME"


@dataclass(frozen=True)
class TemporalConstraint:
    constraint_type: TemporalConstraintType
    n_value: int = 0

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

        return True


@dataclass(frozen=True)
class EpisodicQuery:
    raw_text: str
    normalized_query: str
    recall_intent: RecallIntent
    temporal_constraint: Optional[TemporalConstraint]
    lexical_anchors: list[str]
    profile_id: str


_EXPLICIT_PATTERNS = [
    r"(?i)\b(te acuerdas|recuerdas|te acord[aá]s)\b",
    r"(?i)\bqu[eé] hab[ií]amos (dicho|hablado|decidido|visto|concluido)\b",
    r"(?i)\bde qu[eé] hablamos\b",
    r"(?i)\bte coment[eé]\b",
    r"(?i)\bhablamos (de|sobre)\b",
    r"(?i)\bdo you remember\b",
    r"(?i)\bwhat did we (say|talk|discuss|decide)\b",
    r"(?i)\bdid i mention\b",
]

_IMPLICIT_PATTERNS = [
    r"(?i)\bvolv[ií] a (probar|ver|checar|intentar)\b",
    r"(?i)\bsigue (igual|fallando|con el mismo|sin)\b",
    r"(?i)\blo que (vimos|platicamos|comentamos|hablamos|decidimos)\b",
    r"(?i)\baquello que\b",
    r"(?i)\baquella conversaci[oó]n\b",
    r"(?i)\bla [uú]ltima vez\b",
    r"(?i)\bcomo te dec[ií]a\b",
    r"(?i)\bcomo comentamos\b",
]

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

    def parse_temporal_constraint(self, text: str) -> Optional[TemporalConstraint]:
        lower = text.lower()

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

        # La última vez / Last time
        if re.search(r"\b(la [uú]ltima vez|last time)\b", lower):
            return TemporalConstraint(constraint_type=TemporalConstraintType.LAST_TIME)

        return None

    def extract_lexical_anchors(self, text: str) -> list[str]:
        # Tokenize words
        words = re.findall(r"\b[a-zA-ZáéíóúÁÉÍÓÚñÑ0-9_-]{3,}\b", text.lower())
        anchors = [w for w in words if w not in _STOPWORDS]
        # De-duplicate while preserving order
        seen = set()
        out = []
        for w in anchors:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return out

    def analyze(
        self,
        text: str,
        profile_id: str,
        reference_time: Optional[datetime] = None,
    ) -> EpisodicQuery:
        temporal = self.parse_temporal_constraint(text)
        lexical = self.extract_lexical_anchors(text)

        # Check explicit patterns
        is_explicit = any(re.search(pat, text) for pat in _EXPLICIT_PATTERNS)
        is_implicit = any(re.search(pat, text) for pat in _IMPLICIT_PATTERNS)

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
        )
