"""kira-web-behavior-20261013 K1-K5: regressions from the 2026-10-13 live session."""
from __future__ import annotations

import logging

import pytest

from opencohost.config import web_fetch
from opencohost.config.validation import output_guard
from opencohost.core.self_knowledge.gate import SelfKnowledgeGate
from opencohost.core.web import prompt, trigger
from tests.test_decision_engine_wiring import _motor
from tests.test_web_lookup import Net

GATE = SelfKnowledgeGate()


@pytest.fixture
def web_on():
    web_fetch.set_enabled(True)
    yield
    web_fetch.set_enabled(False)


# --- K1: spoken source labels never trip no_suspicious_links ---------------------------


class _R:
    def __init__(self, domain):
        self.title, self.snippet, self.domain = "Titulo", "Dato util", domain


@pytest.mark.parametrize(
    "domain,label",
    [
        ("www.nvidia.com", "nvidia"),
        ("developer.nvidia.com", "nvidia"),
        ("en.wikipedia.org", "wikipedia"),
        ("www.bbc.co.uk", "bbc"),
        ("minecraft.wiki", "minecraft"),
        ("", "unknown"),
    ],
)
def test_source_label_is_a_spoken_site_name(domain, label):
    block = prompt.build_block([], [_R(domain)])
    assert f"[{label}] " in block
    assert "www" not in block.split("\n", 2)[-1]


def test_reply_built_from_labels_passes_the_link_guardrail():
    block = prompt.build_block([("www.nvidia.com", "texto")], [_R("www.nvidia.com")])
    label = block.split("[", 1)[1].split("]", 1)[0]
    allowed, reason = output_guard(f"Segun {label}, la nueva tarjeta sale en enero.")
    assert allowed is True, reason


@pytest.mark.parametrize("text", ["entra a www.nvidia.com ya", "mira https://nvidia.com/x"])
def test_link_guardrail_is_not_weakened(text):
    allowed, reason = output_guard(text)
    assert allowed is False and "no_suspicious_links" in reason


def test_instruction_asks_for_the_label_not_a_url():
    ins = prompt.WEB_RESULTS_INSTRUCTION
    assert "label" in ins and "Never say a URL" in ins


# --- K2: topic carry only for anaphoric follow-ups -------------------------------------


def _ddg_q(net):
    import httpx

    return [httpx.URL(u).params.get("q") for h, u in net.requests if "duckduckgo.com" in h]


def test_new_subject_is_not_carried(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, _ = _motor(monkeypatch)
    net = Net()
    motor._web_fetcher_factory = net.fetcher
    motor._run_web_step("busca en internet nvidia drivers nuevos", "direct")
    motor._run_web_step("busca en internet que es LiveAudio", "direct")
    q = _ddg_q(net)[1]
    assert "nvidia" not in q.lower() and "drivers" not in q.lower()
    assert any("[WEB] carry decision=fresh" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("text", ["busca en internet mas sobre eso", "y en Mexico busca en internet cuanto cuesta"])
def test_anaphoric_followup_is_carried_and_logged(monkeypatch, caplog, web_on, text):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, _ = _motor(monkeypatch)
    net = Net()
    motor._web_fetcher_factory = net.fetcher
    motor._run_web_step("busca en internet nvidia drivers nuevos", "direct")
    motor._run_web_step(text, "direct")
    assert "nvidia" in _ddg_q(net)[1].lower()
    logs = [r.getMessage() for r in caplog.records if "[WEB] carry" in r.getMessage()]
    assert any("decision=carry" in m for m in logs)
    assert all("nvidia" not in m.lower() for m in logs)


def test_failed_lookup_records_no_carry_subject(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    net = Net(search_html="<html></html>")
    motor._web_fetcher_factory = net.fetcher
    look = motor._run_web_step("busca en internet nvidia drivers nuevos", "direct")
    assert not look.block
    assert motor._last_web_search is None
    assert motor._previous_search() is None


def test_carry_ttl_is_45_seconds():
    from opencohost.core.engine import llm_engine_web as w

    assert w.WEB_CARRY_TTL_S == 45.0


# --- K3: trigger misses ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "podras buscarme si hay algo nuevo de nvidia en la web",
        "¿Podrás buscarme si hay algo de LiveAudio en la web?",
        "busca si nvidia tiene drivers nuevos",
        "buscar si nvidia tiene drivers nuevos",
        "búscame cuando sale el parche",
        "busca en google drivers de nvidia",
        "busca en google",
        "puedes buscarme en internet el precio de la 5090",
    ],
)
def test_trigger_catches_listed_misses(text):
    assert trigger.web_rule(text) == "search"


@pytest.mark.parametrize(
    "text",
    [
        "busca la llave en el cofre",
        "busca si hay enemigos cerca de la puerta",  # in-game action, no web medium
        "podras buscar tu camino",
        "buscarme un tema que me guste en el juego",
        "busca esa nave",
    ],
)
def test_trigger_keeps_non_queries_out(text):
    assert trigger.web_rule(text) == "no_search"


@pytest.mark.parametrize(
    "text",
    [
        "buscar modelos de inteligencia artificial que son tendencia en octubre de dos mil veintiséis",
        "pues buscar qué agentes de inteligencia artificial es lo que recomiendan actualmente y para qué propósito",
        "búscame las últimas novedades de nvidia",
        "busca los mejores juegos de 2026",
        "bueno busca qué hay de nuevo en IA",
    ],
)
def test_trigger_bare_search_with_information_topic_cue(text):
    assert trigger.web_rule(text) == "search"


@pytest.mark.parametrize(
    "text",
    [
        "busca la espada en el cofre",
        "busca a tu amigo en el mapa",
        "pues busca la llave del cofre",
    ],
)
def test_trigger_game_object_search_stays_out(text):
    assert trigger.web_rule(text) == "no_search"


def test_trigger_query_for_bare_search_drops_command_and_filler():
    q = trigger.search_query(
        "pues buscar qué agentes de inteligencia artificial es lo que recomiendan actualmente"
    )
    assert q.lower().startswith("qué agentes de inteligencia artificial")
    assert "pues" not in q.lower() and "buscar" not in q.lower()
    q2 = trigger.search_query(
        "buscar modelos de inteligencia artificial que son tendencia en octubre de dos mil veintiséis"
    )
    assert q2.lower().startswith("modelos de inteligencia artificial")
    assert "buscar" not in q2.lower()


def test_trigger_query_strips_the_new_commands():
    assert trigger.search_query("busca en google drivers de nvidia") == "drivers de nvidia"
    q = trigger.search_query("podras buscarme si hay algo nuevo de nvidia en la web")
    assert "nvidia" in q and "podras" not in q.lower() and "buscarme" not in q.lower()


# --- K4: self-knowledge gate -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "quien es creador de opencohost",
        "¿quién es el creador de OpenCohost?",
        "quién hizo opencohost",
        "dónde está tu código",
        "cual es tu repositorio",
        "el repositorio de opencohost",
        "where is the repository",
    ],
)
def test_gate_detects_creator_intent(text):
    assert "creator" in GATE.detect(text)


@pytest.mark.parametrize(
    "text",
    [
        "alguien indexo el repositorio del dueño y no me parece bien",
        "subi mi codigo fuente a github ayer",
        "que opinas de que usen el repo de otro",
    ],
)
def test_gate_ignores_bare_repo_words(text):
    assert "creator" not in GATE.detect(text)


# --- K5: no-search note ----------------------------------------------------------------


def test_not_searched_note_does_not_deny_the_ability():
    note = prompt.WEB_NOT_SEARCHED_NOTE
    assert "No web search" in note
    assert "can search the web" in note
    assert "did not search this time" in note
    assert "Never say you cannot search" in note
    assert "busca en internet" in note
    assert "invent" in note


def test_explicit_ask_without_search_selects_the_note(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    look = motor._run_web_step("la web de mi proyecto no carga", "direct")
    assert look.note == prompt.WEB_NOT_SEARCHED_NOTE
