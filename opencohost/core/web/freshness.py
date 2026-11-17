"""Freshness of a web query (runtime-fixes-20261110 T2, D).

The search providers are called with the query text only. No recency parameter
is confirmed anywhere in this repository, so a time-sensitive query gets the
current year appended to its TEXT; a query that already names a year, and a
timeless one, are left alone. The year is the only thing added: no personal
data, nothing from the conversation.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Optional

_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_TIME_WORDS = re.compile(
    r"\b(?:"
    r"hoy|ahora|actual|actuales|actualmente|actualidad|ultimo|ultima|ultimos|ultimas|reciente|recientes|"
    r"recientemente|nuevo|nueva|nuevos|nuevas|novedades?|noticias?|esta semana|este mes|este ano|"
    r"ayer|anoche|manana|precio|precios|cotizacion|clima|tiempo en|resultados?|quien gano|marcador|"
    r"estreno|estrenos|lanzamiento|parche|version|"
    r"today|tonight|now|current|currently|latest|recent|recently|new|news|this week|this month|this year|"
    r"yesterday|last night|price|prices|weather|score|scores|results?|release|patch|update"
    r")\b"
)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def is_time_sensitive(query) -> bool:
    if not isinstance(query, str) or not query.strip():
        return False
    return bool(_TIME_WORDS.search(_fold(query)))


def with_year(query, now: Optional[datetime] = None) -> str:
    """``query`` plus the current year when it is time-sensitive and names no year."""
    if not isinstance(query, str):
        return ""
    base = query.strip()
    if not base or _YEAR.search(base) or not is_time_sensitive(base):
        return query if isinstance(query, str) else ""
    year = (now or datetime.now()).year
    return f"{base} {year}"
