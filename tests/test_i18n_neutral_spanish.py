"""Neutral Spanish contract for LLM-facing prompts (owner decision neutral_spanish_20260809).

Every Spanish string sent to the model as instruction must use tuteo, never
voseo, because the local model mirrors the register of the prompt. English
prompts must contain no Spanish. Detector patterns that match model OUTPUT
(guardrails, injection markers, repetition stopwords) are deliberately not
scanned here: they must keep matching voseo.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from opencohost.i18n import active

ROOT = Path(__file__).resolve().parents[1] / "opencohost"

# Voseo imperatives / presents (accent-folded comparison is NOT used: the accent is the marker).
_VOSEO_FORMS = {
    "respondé", "opiná", "seguí", "evitá", "podés", "tenés", "sos", "rondá", "sostené",
    "cortá", "generá", "desarrollá", "conectá", "dejá", "ignorá", "hablá", "decí",
    "soná", "integrá", "ajustá", "hacé", "entrá", "sumá", "apuntá", "respetá",
    "proponé", "pensá", "usá", "priorizá", "explicá", "acompañá", "sabés", "vos",
    "querés", "mirá", "recordás", "acordás", "escribí", "contá", "firmá",
}
# Any -ás/-és/-ís present form is voseo unless it is a plain tuteo word.
_TUTEO_AS = {
    "más", "además", "después", "también", "estás", "país", "interés", "través",
    "demás", "atrás", "detrás", "jamás", "quizás", "inglés", "francés", "adiós",
    "entonces", "según", "compás", "anís",
}
_WORD = re.compile(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]+")
_AS_FORM = re.compile(r"^[a-zñ]+(?:ás|és|ís)$")


def _voseo_in(text: str) -> list[str]:
    hits = []
    for raw in _WORD.findall(text):
        w = raw.lower()
        if w in _VOSEO_FORMS or (_AS_FORM.match(w) and w not in _TUTEO_AS):
            hits.append(raw)
    return hits


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings(v)


def _manifest_llm(code: str) -> list[str]:
    path = ROOT / "locales" / code / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(_strings(data["llm"]))


def _legacy_constants() -> list[tuple[str, str]]:
    out = []
    for name in dir(active):
        if name.startswith("LEGACY_"):
            for s in _strings(getattr(active, name)):
                out.append((name, s))
    return out


def _literals(relpath: str) -> list[str]:
    tree = ast.parse((ROOT / relpath).read_text(encoding="utf-8"))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def test_es_manifest_llm_strings_use_tuteo():
    bad = {s[:60]: _voseo_in(s) for s in _manifest_llm("es") if _voseo_in(s)}
    assert not bad, bad


def test_legacy_es_constants_use_tuteo():
    bad = {n: _voseo_in(s) for n, s in _legacy_constants() if _voseo_in(s)}
    assert not bad, bad


@pytest.mark.parametrize(
    "relpath",
    [
        "core/editorial/active_cards.py",
        "core/editorial/editorial_cards.py",
        "core/engine/llm_engine_agenda.py",
        "core/profiles/cohost_profiles.py",
        "smart_aggregator/kira_agenda_controller.py",
    ],
)
def test_code_resident_llm_literals_use_tuteo(relpath):
    bad = {s[:60]: _voseo_in(s) for s in _literals(relpath) if _voseo_in(s)}
    assert not bad, bad


_SPANISH_ONLY = re.compile(
    r"[áéíóúñ¿¡]|\b(?:el|los|las|una|que|para|con|del|pero|sin|tema|usted|eres|puedes)\b",
    re.IGNORECASE,
)


def test_en_manifest_llm_strings_have_no_spanish():
    # Format placeholders such as {tema} are engine identifiers, not prose.
    bad = {}
    for s in _manifest_llm("en"):
        prose = re.sub(r"\{[a-z_]+\}", " ", s)
        if _SPANISH_ONLY.search(prose):
            bad[s[:60]] = _SPANISH_ONLY.findall(prose)
    assert not bad, bad
