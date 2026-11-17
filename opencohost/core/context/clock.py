"""
opencohost/core/context/clock.py

clock-and-compact-polish-20261010: the ``clock`` tool.

Kira gets the owner machine's local date and time only on the turns that ask
for it. ``clock_rule`` is the deterministic rule of the ``clock`` decision
(explicit "qué hora es" / "what time is it" asks, Spanish and English);
``may_ask_time`` is the loose lexical pre-check that decides whether Laya is
asked at all, so a plain turn never adds a question to the decision call.

Pure and never raising; the text is never logged or kept.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from opencohost.core.vision.gate import _normalize, _rx

TIME = "time"
NO_TIME = "no_time"
# Owner voice sources, same as the decision's SOURCES entry.
CLOCK_SOURCES = frozenset({"direct", "ptt"})

# Our own tables: the date never depends on the OS locale (strftime %A/%B).
_WEEKDAYS = {
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}
_MONTHS = {
    "es": (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ),
    "en": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
}
# English like the other code-built system instructions; only the date itself
# follows the UI language.
CLOCK_LINE = (
    "CURRENT LOCAL TIME: {date}, {hhmm} (local time of the owner's machine). "
    "Use it only to answer the time/date question."
)

_RULE = _rx(
    r"\bque horas? (?:es|son|tienes)\b",
    r"\b(?:dime|dame|me dices|sabes|puedes decirme) la hora\b",
    r"\bhora (?:actual|exacta)\b",
    r"\bque dia (?:es|estamos|tenemos)\b",
    r"\ba (?:cuantos|que dia) estamos\b",
    r"\bque fecha\b",
    r"\bfecha de hoy\b",
    r"\b(?:en )?que ano (?:es|estamos)\b",
    r"\bwhat time is it\b",
    r"\bwhats the time\b", r"\bwhat is the time\b",
    r"\bwhat day is (?:it|today)\b",
    r"\btodays date\b",
    r"\bwhat(?:s| is) (?:the )?date\b",
    r"\bwhat year is it\b",
)
# Loose: any time/date word. Wider than the rule on purpose (Laya decides the
# rest); "ahora" never matches "hora".
_LOOSE = re.compile(
    r"\b(?:hora|horas|dia|fecha|hoy|ano|cuantos estamos|reloj"
    r"|time|date|day|today|year|clock)\b"
)


def _clean(text: Any) -> str:
    if not isinstance(text, str) or not text:
        return ""
    try:
        return _normalize(text)
    except Exception:
        return ""


def clock_rule(text: Any) -> str:
    """``time`` for an explicit time/date ask, else ``no_time``."""
    clean = _clean(text)
    return TIME if clean and _RULE.search(clean) else NO_TIME


def may_ask_time(text: Any) -> bool:
    """True when a loose time/date word is present (Laya is worth asking)."""
    clean = _clean(text)
    return bool(clean and _LOOSE.search(clean))


def _lang(lang: Optional[str]) -> str:
    return "en" if isinstance(lang, str) and lang.lower().startswith("en") else "es"


def format_date(now: datetime, lang: Optional[str] = "es") -> str:
    """The local date with its weekday ("martes 10 de noviembre de 2026")."""
    key = _lang(lang)
    weekday = _WEEKDAYS[key][now.weekday()]
    month = _MONTHS[key][now.month - 1]
    if key == "en":
        return f"{weekday}, {month} {now.day}, {now.year}"
    return f"{weekday} {now.day} de {month} de {now.year}"


def format_clock_line(now: datetime, lang: Optional[str] = "es") -> str:
    """The ``clock`` section text for ``now``: Spanish unless ``lang`` is en."""
    return CLOCK_LINE.format(date=format_date(now, lang), hhmm=f"{now.hour:02d}:{now.minute:02d}")
