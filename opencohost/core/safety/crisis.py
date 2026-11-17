"""Crisis detection (guardrails-vulnerable-users S1 + contextual arbiter).

Pure, deterministic and dependency free at import time:
- ``has_explicit_crisis_stem(text)`` checks the bilingual (es + en) lexical
  crisis stems after Unicode/homoglyph/leetspeak/spacing/repeat normalization.
- ``detect_crisis(text, *, context=None, source="direct")`` evaluates ``text``
  through the contextual arbiter (preventing benign gaming death, gamer
  laughter hyperbole, 2nd-person abuse, and prompt-injection coercion bait from
  tripping a false crisis turn while guaranteeing serious amplifiers always
  escalate). It never stores or logs user text.
"""
from __future__ import annotations

import re
import unicodedata

_REPEAT_RE = re.compile(r"([^\W\d_])\1{2,}")
_VOWEL_DOUBLE_RE = re.compile(r"([aiou])\1+")
_SPACE_RE = re.compile(r"\s+")
_HAS_LATIN_RE = re.compile(r"[A-Za-z]")
_TOKEN_RE = re.compile(r"\S+")
# Collapse obfuscated single-letter runs separated by dots/dashes/underscores/stars
_SEP_SPACED_RE = re.compile(
    r"(?<![^\W\d_])(?:[^\W\d_][.\-_*]){2,}[^\W\d_](?![^\W\d_])"
)
# Collapse space-separated single letters (at least 3 letters, e.g. "k y s" or "m o r i r")
_SPACE_SPACED_RE = re.compile(
    r"(?<![^\W\d_])(?:[^\W\d_] ){2,}[^\W\d_](?![^\W\d_])"
)
_OBFUSCATION_HINT_RE = re.compile(r"[013457@$._*\-]|([aiou])\1|(?<![^\W\d_])[^\W\d_] [^\W\d_] [^\W\d_]")

_LEET_MAP = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})

# Cyrillic and Greek lookalike characters folded only when Latin text is present
_CONFUSABLES = str.maketrans({
    # Cyrillic
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "і": "i", "ѕ": "s", "ј": "j", "ԁ": "d", "һ": "h", "к": "k", "м": "m",
    "т": "t", "н": "h", "в": "b", "А": "A", "Е": "E", "О": "O", "Р": "P",
    "С": "C", "Х": "X", "У": "Y", "І": "I", "К": "K", "М": "M", "Т": "T",
    "Н": "H", "В": "B",
    # Greek
    "α": "a", "ε": "e", "ι": "i", "ο": "o", "ρ": "p", "τ": "t", "κ": "k",
    "ν": "v", "χ": "x", "υ": "y",
})


def _fold_base(text: str, *, force_confusables: bool = False) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    if force_confusables or _HAS_LATIN_RE.search(text):
        text = text.translate(_CONFUSABLES)
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.replace("'", "").replace("’", "").replace("`", "")


def _normalize(text: str) -> str:
    folded = _fold_base(text)
    folded = _REPEAT_RE.sub(r"\1", folded)
    return _SPACE_RE.sub(" ", folded).strip()


def _fix_leet_token(token: str) -> str:
    if any(ch.isalpha() for ch in token) and any(ch in "013457@$" for ch in token):
        return token.translate(_LEET_MAP)
    return token


def _normalize_obfuscated(text: str) -> str:
    folded = _fold_base(text, force_confusables=True)
    folded = _SEP_SPACED_RE.sub(
        lambda m: "".join(ch for ch in m.group(0) if ch.isalnum()), folded
    )
    # Process double-space-separated chunks so "q u i e r o   m o r i r" stays 2 words
    if "  " in folded:
        chunks = [
            _SPACE_SPACED_RE.sub(lambda m: m.group(0).replace(" ", ""), chunk)
            for chunk in re.split(r" {2,}", folded)
        ]
        folded = " ".join(chunks)
    else:
        folded = _SPACE_SPACED_RE.sub(lambda m: m.group(0).replace(" ", ""), folded)
    folded = _TOKEN_RE.sub(lambda m: _fix_leet_token(m.group(0)), folded)
    folded = _REPEAT_RE.sub(r"\1", folded)
    folded = _VOWEL_DOUBLE_RE.sub(r"\1", folded)
    return _SPACE_RE.sub(" ", folded).strip()


# "no quiero ..." / "don't want to ..." flip the meaning of the wishes below.
_NEG_ES = r"(?<!\bno )(?<!\bnunca )(?<!\bni )(?<!\bjamas )"
_NEG_EN = r"(?<!\bnot )(?<!\bdont )(?<!\bnever )(?<!\bno )(?<!\bcant )"

_WANT_ES = r"(?:quiero|kiero|qiero|quisiera|kisiera|queria|ojala)"
_HYPERBOLE_ES = (
    r"(?!\s+de\s+(?:la\s+|el\s+)?(?:risa|verguenza|pena|hambre|sueno|calor|frio|envidia|"
    r"celos|miedo|aburrimiento|gusto|amor|felicidad|cansancio|sed|ternura|emocion|"
    r"vergüenza))"
)
_HYPERBOLE_EN = (
    r"(?!\s+(?:laughing|laughter|inside|of\s+(?:embarrassment|shame|laughter|boredom|cringe)|"
    r"from\s+(?:embarrassment|shame|laughing|laughter|cringe)))"
)

_ES = [
    # wishes: quiero morir / morirme / suicidarme / matarme
    _NEG_ES + _WANT_ES + r"\s+(?:morir|morirme)\b" + _HYPERBOLE_ES,
    _NEG_ES + _WANT_ES + r"\s+(?:matarme|suicidarme|quitarme\s+la\s+vida|desaparecer\s+para\s+siempre"
    r"|dejar\s+de\s+existir|hacerme\s+dano|lastimarme|cortarme(?:\s+las\s+venas)?)\b",
    _NEG_ES + r"\bme\s+(?:quiero|kiero|qiero|voy\s+a|podria|pienso|quisiera|gustaria)\s+(?:matar|suicidar|quitar\s+la\s+vida)\b",
    _NEG_ES + r"\bme\s+(?:quitare|quito)\s+la\s+vida\b",
    _NEG_ES + r"\bme\s+(?:quiero|kiero)\s+morir\b" + _HYPERBOLE_ES,
    r"\b(?:ojala)\s+(?:me\s+muriera|me\s+muera|no\s+despertar|no\s+despertara|estuviera\s+muert[oa])\b",
    r"\b(?:desearia|quisiera|me\s+gustaria)\s+(?:estar\s+muert[oa]|no\s+haber\s+nacido)\b",
    # statements of not wanting to live
    r"\b(?:ya\s+)?no\s+(?:quiero|kiero|puedo|aguanto|soporto)\s+(?:mas\s+)?"
    r"(?:seguir\s+)?(?:viviendo|vivir|existir|estar\s+vivo|estar\s+viva|con\s+esta\s+vida)\b",
    r"\bno\s+aguanto\s+(?:mas\s+)?(?:vivir|esta\s+vida|la\s+vida|seguir\s+viviendo)\b",
    r"\b(?:estoy\s+)?cansad[oa]\s+de\s+(?:vivir|la\s+vida|existir)\b",
    r"\bno\s+(?:le\s+)?(?:veo|encuentro)\s+(?:ningun\s+)?sentido\s+(?:a\s+)?(?:la\s+vida|vivir|seguir\s+viviendo)\b",
    r"\bno\s+tengo\s+(?:ganas|razones|motivos|ningun\s+motivo|ninguna\s+razon)\s+(?:de|para)\s+vivir\b",
    r"\bno\s+vale\s+la\s+pena\s+(?:vivir|seguir\s+viviendo|estar\s+vivo|estar\s+viva)\b",
    r"\bya\s+no\s+tengo\s+(?:razones|motivos)\s+para\s+(?:vivir|seguir)\b",
    # acts and plans
    r"\b(?:quitarme|quitarse)\s+la\s+vida\b",
    r"\b(?:acabar|terminar)\s+con\s+mi\s+(?:propia\s+)?vida\b",
    r"\b(?:me\s+voy\s+a|voy\s+a)\s+cortar(?:me)?\s+las\s+venas\b",
    r"\bcortar(?:me)?\s+las\s+venas\b",
    r"\b(?:pienso|pensando|pense|he\s+pensado|tengo\s+ganas)\s+(?:en|de)\s+(?:el\s+suicidio|suicidarme|matarme|morirme)\b",
    r"\bideas\s+suicidas\b",
    r"\b(?:estoy|me\s+siento)\s+suicida\b",
    r"\bme\s+(?:autolesiono|hago\s+dano|corto\s+para)\b",
    r"\bquiero\s+(?:hacerme\s+dano|autolesionarme|lastimarme)\b",
    # indirect: burden / better off without me
    r"\b(?:todos|todo\s+el\s+mundo|el\s+mundo|mi\s+familia|mis\s+padres|mis\s+amigos)\s+"
    r"(?:estarian|estaria|vivirian)\s+mejor\s+sin\s+mi\b",
    r"\b(?:seria|estaria)\s+mejor\s+(?:si\s+no\s+estuviera|si\s+yo\s+no\s+existiera|sin\s+mi)\b",
    r"\bno\s+quiero\s+despertar\b",
    r"\bnadie\s+me\s+echaria\s+de\s+menos\s+si\s+(?:muriera|desapareciera)\b",
]

_EN = [
    r"\b" + _NEG_EN + r"(?:want|wan{1,2}a|wish|need|going|gon{1,2}a|plan{1,2}(?:ing)?|ready|thinking\s+(?:of|about))\s+(?:to\s+)?"
    r"(?:die|dying)\b" + _HYPERBOLE_EN,
    r"\bkil{1,2}(?:ing)?\s+my\s*self\b",
    r"\b(?:end|ending|take|taking)\s+(?:my\s+(?:own\s+)?life|it\s+al{1,2})\b",
    r"\bcom{1,2}it{1,2}(?:ing)?\s+suicide\b",
    r"\b(?:thinking|thought|thoughts)\s+(?:about|of)\s+suicide\b",
    r"\b(?:i\s*am|im|feeling|feel|so)\s+suicidal\b",
    r"\bsuicidal\s+(?:thoughts|ideas|ideation)\b",
    r"\b(?:hurt|harm|cut{1,2}|unalive)(?:ing)?\s+my\s*self\b",
    r"\bself\s*-?\s*harm(?:ing)?\b",
    r"\bslit(?:ting)?\s+my\s+wrists?\b",
    r"\bdont\s+want\s+to\s+(?:live|be\s+alive|exist|wake\s+up|be\s+here\s+anymore)\b",
    r"\b(?:dont|do\s+not)\s+want\s+to\s+live\s+(?:anymore|any\s+more|like\s+this)\b",
    r"\b(?:no|dont\s+have\s+any)\s+reason\s+to\s+(?:live|go\s+on|keep\s+going)\b",
    r"\bnothing\s+(?:left\s+)?to\s+live\s+for\b",
    r"\b(?:i\s+)?wish\s+(?:i\s+(?:was|were)\s+(?:dead|gone)|i\s+(?:had\s+)?never\s+been\s+born|i\s+wasnt\s+born)\b",
    r"\bbet{1,2}er\s+of{1,2}\s+(?:dead|without\s+me|if\s+i\s+(?:was|were)\s+(?:gone|dead))\b",
    r"\b(?:cant|can\s+not|can{1,2}ot)\s+(?:go\s+on|keep\s+going)\s+(?:living|like\s+this)\b",
    r"\bwant\s+to\s+disappear\s+forever\b",
]

_PATTERNS = [re.compile(p) for p in (*_ES, *_EN)]

# Fast single-pass ASCII stem precheck so non-crisis long inputs reject in O(n) C scan
_FAST_STEM_RE = re.compile(
    r"morir|muri|muer|matar|suicid|viv|vid|exist|despert|nacid|dano|lastim|cort|"
    r"autoles|sin mi|seguir|estuvier|desaparec|venas|die|dyin|kil|lif|liv|it al|"
    r"mysel|harm|aliv|wake up|anymor|dead|gon|born|without me|disappear|wrist"
)


def _matches_normalized(norm: str) -> bool:
    if not norm or not _FAST_STEM_RE.search(norm):
        return False
    return any(p.search(norm) for p in _PATTERNS)


def has_explicit_crisis_stem(text: str) -> bool:
    """Pure lexical check for explicit self-harm or suicidal ideation stems (es/en)."""
    if not isinstance(text, str) or not text.strip():
        return False
    norm = _normalize(text)
    if _matches_normalized(norm):
        return True
    if _OBFUSCATION_HINT_RE.search(text) or any(ord(ch) > 127 for ch in text):
        obf = _normalize_obfuscated(text)
        if obf != norm and _matches_normalized(obf):
            return True
    return False


def detect_crisis(
    text: str,
    *,
    context: object = None,
    source: str = "direct",
) -> bool:
    """True when ``text`` warrants CRISIS_INTERVENTION under contextual assessment."""
    if not isinstance(text, str) or not text.strip():
        return False
    from opencohost.core.safety.contextual import assess_crisis_flag

    return assess_crisis_flag(text, context=context, source=source)
