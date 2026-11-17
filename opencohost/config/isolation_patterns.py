"""S4 (guardrails-vulnerable-users): isolation / secrecy / dependency patterns.

A lonely or young listener must never hear "you only need me", a request for
secrecy, or that friends, family, a therapist or a doctor are useless. The
output guard enforces this as the locked rule ``no_isolation_pressure``
(always on, no owner toggle).

Phrasing is second-person / first-person and stays inside one sentence, so a
game tip ("you only need two potions") or a recipe "secret" does not match.
"""
from __future__ import annotations

import re

RULE_ID = "no_isolation_pressure"

ISOLATION_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        # es: exclusivity
        r"\b(?:solo|s[oó]lo|solamente)\s+me\s+necesitas\s+a\s+m[ií]\b",
        r"\bme\s+necesitas\s+(?:solo|s[oó]lo|solamente)\s+a\s+m[ií]\b",
        r"\bno\s+necesitas\s+a\s+nadie\s+m[aá]s\b",
        r"\bno\s+necesitas\s+(?:a\s+)?(?:tus\s+)?(?:amig[oa]s|familia|padres|pap[aá]s|hermanos)\b",
        r"\bsoy\s+(?:la|el|lo)\s+[uú]nic[oa]\s+(?:que|persona)\b[^.!?\n]{0,30}\b(?:entiende|quiere|necesitas|importa)\b",
        r"\bno\s+(?:quiero\s+que\s+)?hables\s+con\s+nadie\s+m[aá]s\b",
        r"\b(?:tus\s+)?(?:amig[oa]s|padres|pap[aá]s|familia)\s+no\s+(?:te\s+)?(?:entienden|quieren|necesitan|importan|sirven)\b",
        # es: secrecy
        r"\bno\s+(?:se\s+lo\s+|le\s+|les\s+)?(?:cuentes|digas|menciones)\s+(?:a\s+)?(?:nadie|tus\s+(?:padres|pap[aá]s)|tu\s+familia)\b",
        r"\b(?:esto|esto\s+es|es)\s+nuestro\s+secreto\b",
        r"\bes\s+un\s+secreto\s+entre\b",
        r"\bsecreto\s+entre\s+(?:t[uú]\s+y\s+yo|nosotros)\b",
        r"\bque\s+quede\s+entre\s+(?:t[uú]\s+y\s+yo|nosotros)\b",
        # es: discouraging help
        r"\b(?:los\s+|las\s+)?(?:psic[oó]log[oa]s|terapeutas|psiquiatras|terapias?|m[eé]dicos)\s+no\s+(?:sirven|ayudan|funcionan|te\s+entienden|son\s+necesarios)\b",
        r"\bno\s+necesitas\s+(?:a\s+)?(?:un\s+|una\s+|ning[uú]n\s+)?(?:psic[oó]log[oa]|terapeuta|psiquiatra|terapia|ayuda\s+profesional)\b",
        # en: exclusivity
        r"\byou\s+(?:only|just)\s+need\s+me\b",
        r"\bi(?:['’]m|\s+am)\s+(?:the\s+only\s+one\s+(?:who|that)|all\s+you\s+(?:need|have))\b",
        r"\byou\s+(?:don['’]?t|do\s+not)\s+need\s+(?:anyone|anybody)\s+else\b",
        r"\byou\s+(?:don['’]?t|do\s+not)\s+need\s+(?:your\s+)?(?:friends|family|parents)\b",
        r"\b(?:don['’]?t|do\s+not)\s+(?:talk|speak)\s+to\s+(?:anyone|anybody)\s+else\b",
        r"\b(?:your\s+)?(?:friends|family|parents)\s+(?:don['’]?t|do\s+not)\s+(?:really\s+)?(?:understand|care\s+about|love)\s+you\b",
        # en: secrecy
        r"\b(?:don['’]?t|do\s+not)\s+tell\s+(?:your\s+)?(?:parents|mom|dad|mother|father|family|anyone|anybody|friends)\b",
        r"\bour\s+(?:little\s+)?secret\b",
        r"\bthis\s+is\s+(?:a\s+)?(?:little\s+)?secret\s+between\b",
        r"\bjust\s+between\s+(?:you\s+and\s+me|us)\b",
        # en: discouraging help
        r"\b(?:therapists?|psychologists?|psychiatrists?|therapy|counsel(?:or|ing)s?|doctors?)\s+(?:don['’]?t|do\s+not|won['’]?t|never)\s+(?:help|work|understand|care)\b",
        r"\byou\s+(?:don['’]?t|do\s+not)\s+need\s+(?:a\s+|any\s+)?(?:therapist|therapy|psychologist|psychiatrist|counselor|professional\s+help)\b",
    )
]


def violates(text: str) -> bool:
    """True when ``text`` pressures for exclusivity or secrecy, or discourages
    friends, family or professional help."""
    return any(p.search(text) for p in ISOLATION_PATTERNS)
