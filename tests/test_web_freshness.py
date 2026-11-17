"""runtime-fixes-20261110 T2 (D): web answers carry today's date.

Route taken for the search provider: no recency parameter is confirmed by the
repository (nothing in ``search_api.py`` or its tests uses one), so a
time-sensitive query gets the current year appended to its text.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

from opencohost.config import web_fetch
from opencohost.core.web import freshness
from tests.test_decision_engine_wiring import _motor
from tests.test_web_lookup import Net

NOW = datetime(2026, 11, 10, 9, 30)


@pytest.mark.parametrize(
    "query",
    [
        "parche nuevo de elden ring",
        "ultimas noticias de nvidia",
        "precio de la rtx 5090 hoy",
        "who won the game last night",
        "latest nvidia driver",
        "clima en madrid esta semana",
        "current bitcoin price",
        "resultados de la liga",
    ],
)
def test_time_sensitive_queries_get_the_year(query):
    assert freshness.is_time_sensitive(query) is True
    assert freshness.with_year(query, NOW) == f"{query} 2026"


@pytest.mark.parametrize(
    "query",
    [
        "que es un transistor",
        "historia de roma",
        "how does a transformer work",
        "elden ring lore",
    ],
)
def test_timeless_queries_are_untouched(query):
    assert freshness.is_time_sensitive(query) is False
    assert freshness.with_year(query, NOW) == query


def test_a_query_that_already_names_a_year_is_untouched():
    assert freshness.with_year("parche nuevo 2025", NOW) == "parche nuevo 2025"
    assert freshness.with_year("latest drivers 2026", NOW) == "latest drivers 2026"


def test_with_year_never_raises_and_adds_no_personal_data():
    assert freshness.with_year("", NOW) == ""
    assert freshness.with_year(None, NOW) == ""
    assert freshness.with_year("noticias hoy", None).split()[-1].isdigit()


@pytest.fixture
def web_on():
    web_fetch.set_enabled(True)
    yield
    web_fetch.set_enabled(False)


def _run(monkeypatch, text):
    motor, seen = _motor(monkeypatch)
    net = Net()
    motor._web_fetcher_factory = net.fetcher
    motor._generar_dialogo(text, source="direct", commit_history=True)
    return motor, seen, net


def test_the_provider_query_carries_the_year_for_a_time_sensitive_search(monkeypatch, web_on):
    import httpx

    _, _, net = _run(monkeypatch, "busca en internet el parche nuevo de zanahoria")
    q = httpx.URL(net.requests[0][1]).params.get("q")
    assert q.endswith(str(datetime.now().year))


def test_a_timeless_search_query_is_sent_as_is(monkeypatch, web_on):
    import httpx

    _, _, net = _run(monkeypatch, "busca en internet que es un transistor")
    assert httpx.URL(net.requests[0][1]).params.get("q") == "que es un transistor"


def test_a_web_turn_prompt_carries_today(monkeypatch, web_on):
    from opencohost.core.context.clock import _MONTHS, _WEEKDAYS

    _, seen, _ = _run(monkeypatch, "busca en internet el parche nuevo de zanahoria")
    prompt_text = "\n".join(str(m.get("content", "")) for m in seen["messages"][0])
    now = datetime.now()
    assert str(now.year) in prompt_text
    assert _WEEKDAYS["es"][now.weekday()] in prompt_text or _WEEKDAYS["en"][now.weekday()] in prompt_text
    assert _MONTHS["es"][now.month - 1] in prompt_text or _MONTHS["en"][now.month - 1] in prompt_text


def test_a_plain_turn_has_no_date_line(monkeypatch, web_on):
    from opencohost.i18n import active as i18n_active

    _, seen, _ = _run(monkeypatch, "hola Kira que tal")
    marker = i18n_active.web_date_line().split("{")[0].strip()[:20]
    prompt_text = "\n".join(str(m.get("content", "")) for m in seen["messages"][0])
    assert marker not in prompt_text


def test_the_date_line_is_not_duplicated_when_a_clock_line_is_present():
    from opencohost.core.context import prompt_assembler as pa

    line = pa.web_date_line_for(datetime(2026, 11, 10, 9, 30), "es", clock_present=True)
    assert line == ""
    assert "2026" in pa.web_date_line_for(datetime(2026, 11, 10, 9, 30), "es", clock_present=False)
    assert "Tuesday" in pa.web_date_line_for(datetime(2026, 11, 10, 9, 30), "en", clock_present=False)
    assert "martes" in pa.web_date_line_for(datetime(2026, 11, 10, 9, 30), "es", clock_present=False)
