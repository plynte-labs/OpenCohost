"""web-fetch-laya-20261009 T3: the web step in the owner turn.

Real MotorVocalIA with fake LLM transports (the decision-layer wiring
harness), a fake network (``httpx.MockTransport`` + fake resolver) and an
optional fake Laya backend. Pins:
- switch off: nothing is asked, fetched, logged or passed to any seam;
- switch on: only an owner turn whose ``web`` decision says ``search``
  fetches; the block and the instruction reach the prompt; failures become
  a short note; the turn is bounded by the hard budget;
- logs carry domains and counts only, never user text or URLs.
"""
from __future__ import annotations

import logging
from datetime import datetime
import threading
import time

import httpx
import pytest

from opencohost.config import decisions as dc
from opencohost.config import settings
from opencohost.config import web_fetch
from opencohost.core.decision import runtime as decision_runtime
from opencohost.core.engine.llm_engine_web import NO_WEB, SUBJECT_CARRY, WEB_BRIDGE_SUFFIX
from opencohost.core.web import prompt
from tests.test_decision_engine_wiring import FakeBackend, _fingerprint, _motor
from tests.test_web_lookup import LITE_NO_RESULTS, NO_RESULTS, Net

# B-W4: this module pins the legacy prompt layout (system-message placement, one-pair
# history deque, legacy goldens). The v2 placement is tested in tests/test_prompt_layout_v2.py.
pytestmark = pytest.mark.usefixtures("legacy_prompt_layout")

SECRET = "zanahoriaconfidencial"
ASK = f"busca en internet {SECRET} parche nuevo"
PLAIN = f"hola Kira {SECRET}"


def _web_logs(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("[WEB]")]


def _run(monkeypatch, text, *, net=None, source="direct", commit_history=True, motor=None, seen=None, **kw):
    if motor is None:
        motor, seen = _motor(monkeypatch)
    net = net if net is not None else Net()
    motor._web_fetcher_factory = net.fetcher
    motor._generar_dialogo(text, source=source, commit_history=commit_history, **kw)
    return motor, seen, net


def _system(seen, i=0):
    return seen["messages"][i][0]["content"]


def _user(seen, i=0):
    return seen["messages"][i][-1]["content"]


# --- switch off ------------------------------------------------------------------------


def test_switch_off_is_the_base_path(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert web_fetch.is_enabled() is False
    motor, seen, net = _run(monkeypatch, ASK)
    assert net.requests == []
    assert _web_logs(caplog) == []
    assert all(not ({"web_block", "web_note"} & set(k)) for k in seen["builds"])
    assert motor._run_web_step(ASK, "direct") is NO_WEB


def test_switch_off_never_asks_laya_about_web(monkeypatch):
    backend = FakeBackend()
    decision_runtime.install_backend(backend)
    _run(monkeypatch, ASK)
    assert backend.calls == [(ASK, ("vision", "memory", "reply_length"))]


# --- switch on --------------------------------------------------------------------------


@pytest.fixture
def web_on():
    web_fetch.set_enabled(True)
    yield
    web_fetch.set_enabled(False)


def test_explicit_request_fetches_and_reaches_the_prompt(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, seen, net = _run(monkeypatch, ASK)
    hosts = [h for h, _ in net.requests]
    assert hosts[0] == "html.duckduckgo.com" and len(hosts) == 3
    user = _user(seen)
    assert prompt.WEB_BLOCK_HEADER in user
    assert user.index(prompt.WEB_BLOCK_FOOTER) < user.index(SECRET)  # block before the question
    assert prompt.WEB_RESULTS_INSTRUCTION in _system(seen)
    assert prompt.WEB_FAILED_NOTE not in _system(seen)
    (line,) = _web_logs(caplog)
    assert line.startswith("[WEB] decision=search ")
    for part in ("results=3", "kept_links=2", "chunks=2", "kept_chunks=2", "outcome=ok", "fetch_ms=", "filter_ms="):
        assert part in line
    assert "domains=news.example.com,wiki.example.org" in line
    assert "reason=- " in line and "engine=html " in line
    assert SECRET not in caplog.text and "parche" not in caplog.text
    assert "https://" not in line and "?" not in line


def test_query_sent_is_the_stripped_request_only(monkeypatch, web_on):
    wrapped = settings_ptt_wrap(ASK)
    _, _, net = _run(monkeypatch, wrapped, source="ptt")
    _, url = net.requests[0]
    # T2 (freshness): "nuevo" is time-sensitive, so the year is appended.
    assert httpx.URL(url).params.get("q") == f"{SECRET} parche nuevo {datetime.now().year}"


def settings_ptt_wrap(text):
    from opencohost.core import llm_engine

    return llm_engine.i18n_active.ptt_wrapper().format(text=text)


def test_plain_turn_with_switch_on_does_not_fetch(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    _, seen, net = _run(monkeypatch, PLAIN)
    assert net.requests == []
    assert all(not ({"web_block", "web_note"} & set(k)) for k in seen["builds"])
    (line,) = _web_logs(caplog)
    assert line.startswith("[WEB] decision=skip ") and "outcome=skipped" in line


def test_plain_turn_prompt_is_unchanged_by_the_switch(monkeypatch, web_on):
    on = _fingerprint(*_run(monkeypatch, PLAIN)[:2])
    web_fetch.set_enabled(False)
    off = _fingerprint(*_run(monkeypatch, PLAIN)[:2])
    assert on["messages"] == off["messages"]


def test_non_owner_sources_never_fetch(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    for source in ("chat", "kira-agenda", settings.OWNER_BUNDLE_SOURCE):
        _, _, net = _run(monkeypatch, ASK, source=source)
        assert net.requests == []
    assert _web_logs(caplog) == []


def test_search_failure_gives_the_short_note(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    net = Net(fail={"html.duckduckgo.com": httpx.ConnectError})
    _, seen, _ = _run(monkeypatch, ASK, net=net)
    assert prompt.WEB_FAILED_NOTE in _system(seen)
    assert prompt.WEB_BLOCK_HEADER not in _user(seen)
    assert prompt.WEB_RESULTS_INSTRUCTION not in _system(seen)
    (line,) = _web_logs(caplog)
    assert "outcome=error" in line
    # html failed (connect error), lite (404 here) was tried once; the first reason is logged
    assert "reason=other" in line and "engine=lite" in line


def test_empty_search_gives_the_empty_note(monkeypatch, web_on):
    _, seen, _ = _run(monkeypatch, ASK, net=Net(search_html=NO_RESULTS, lite_html=LITE_NO_RESULTS))
    assert prompt.WEB_EMPTY_NOTE in _system(seen)


def test_hard_budget_bounds_the_turn(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    monkeypatch.setattr(settings, "WEB_FETCH_BUDGET_S", 0.3)
    release = threading.Event()

    class Stuck:
        def fetch(self, url, deadline=None):
            release.wait(5)
            raise RuntimeError("late")

    motor, seen = _motor(monkeypatch)
    motor._web_fetcher_factory = Stuck
    started = time.monotonic()
    motor._generar_dialogo(ASK, source="direct", commit_history=True)
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 2.0
    assert prompt.WEB_TIMEOUT_NOTE in _system(seen)
    assert "outcome=timeout" in _web_logs(caplog)[0]


def test_laya_on_may_trigger_an_implicit_search(monkeypatch, web_on):
    backend = FakeBackend(answers={"vision": "no_look", "memory": "recall", "reply_length": "medium", "web": "search"})
    decision_runtime.install_backend(backend)
    dc.set_decision("web", mode="on")
    _, seen, net = _run(monkeypatch, "quién ganó el partido de ayer")
    assert backend.calls[0][1] == ("vision", "memory", "reply_length", "web")
    assert net.requests and net.requests[0][0] == "html.duckduckgo.com"


def test_laya_shadow_never_triggers_a_search(monkeypatch, web_on):
    # The web decision now boots ``on``; shadow is set explicitly.
    from opencohost.config import decisions as decisions_cfg

    decisions_cfg.set_decision("web", mode="shadow")
    backend = FakeBackend(answers={"vision": "no_look", "memory": "recall", "reply_length": "medium", "web": "search"})
    decision_runtime.install_backend(backend)
    _, _, net = _run(monkeypatch, "quién ganó el partido de ayer")
    assert net.requests == []


def test_pregen_lookup_is_reused_by_the_foreground_turn(monkeypatch, web_on):
    motor, seen = _motor(monkeypatch)
    net = Net()
    _run(monkeypatch, ASK, net=net, motor=motor, seen=seen, commit_history=False, allow_vision=True)
    _run(monkeypatch, ASK, net=net, motor=motor, seen=seen, commit_history=True)
    assert [h for h, _ in net.requests].count("html.duckduckgo.com") == 1
    assert prompt.WEB_BLOCK_HEADER in _user(seen, 1)


# --- follow-up subject (web-lookup-polish-20261010 T2) -------------------------------


def _owner_history(motor, text, source="ptt"):
    content = settings_ptt_wrap(text) if source == "ptt" else text
    motor.historial.append({"role": "user", "content": content, "source": source, "private": False})
    motor.historial.append({"role": "assistant", "content": "Ni idea.", "source": source, "private": False})


def test_bare_followup_searches_the_previous_owner_question(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, seen = _motor(monkeypatch)
    motor._web_ledger_add(f"Cuándo sale {SECRET}", search_intent=False)
    net = Net()
    motor._web_fetcher_factory = net.fetcher
    look = motor._run_web_step("busca en la web", "direct")
    assert look.block
    _, url = net.requests[0]
    assert httpx.URL(url).params.get("q") == f"Cuándo sale {SECRET}"
    assert all("chat" not in u and "dijo" not in u for _, u in net.requests)
    (line,) = _web_logs(caplog)
    assert "subject=followup" in line
    assert SECRET not in caplog.text


def test_followup_without_a_previous_owner_turn_is_empty(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, seen = _motor(monkeypatch)
    motor.historial.append({"role": "user", "content": "algo del chat", "source": "chat", "private": False})
    net = Net()
    motor._web_fetcher_factory = net.fetcher
    look = motor._run_web_step("busca en la web", "direct")
    assert net.requests == []
    assert look.note == prompt.WEB_NO_SUBJECT_NOTE
    (line,) = _web_logs(caplog)
    assert "outcome=empty" in line and "subject=followup" in line


def test_direct_subject_is_logged(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, seen = _motor(monkeypatch)
    _owner_history(motor, "otra pregunta vieja")
    net = Net()
    motor._web_fetcher_factory = net.fetcher
    motor._run_web_step(ASK, "direct")
    assert httpx.URL(net.requests[0][1]).params.get("q") == f"{SECRET} parche nuevo {datetime.now().year}"
    (line,) = _web_logs(caplog)
    assert "subject=direct" in line


# --- failure honesty (web-lookup-polish-20261010 T4) ----------------------------------


class _Raises:
    def __init__(self, code):
        self.code = code

    def fetch(self, url, deadline=None):
        from opencohost.core.web.fetch import WebFetchError

        raise WebFetchError(self.code)


@pytest.mark.parametrize(
    "factory,outcome,note",
    [
        (lambda: _Raises("error"), "error", prompt.WEB_FAILED_NOTE),
        (lambda: _Raises("timeout"), "timeout", prompt.WEB_TIMEOUT_NOTE),
        (lambda: _Raises("blocked"), "blocked", prompt.WEB_BLOCKED_NOTE),
        (lambda: Net(search_html=NO_RESULTS, lite_html=LITE_NO_RESULTS).fetcher(), "empty", prompt.WEB_EMPTY_NOTE),
    ],
    ids=["error", "timeout", "blocked", "empty"],
)
def test_every_failure_outcome_carries_the_honesty_note(monkeypatch, caplog, web_on, factory, outcome, note):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = factory
    look = motor._run_web_step(ASK, "direct")
    assert look.block == "" and look.note == note
    assert "Do not answer the factual question from memory" in look.note
    assert f"outcome={outcome}" in _web_logs(caplog)[0]


class FakeRouter:
    def __init__(self):
        self.submitted = []

    def submit(self, text, source, priority, **kw):
        self.submitted.append((text, source))
        return type("Job", (), {"state": None})()

    def discard_job(self, job):
        pass


def test_bridge_line_on_foreground_only(monkeypatch, web_on):
    motor, seen = _motor(monkeypatch)
    router = FakeRouter()
    motor._speech_router_enabled = True
    motor._ensure_router = lambda: router
    monkeypatch.setattr(motor, "_cloud_attempt_loop", lambda setup, **k: type("O", (), {
        "early_return": "ok", "stream": None, "failure_reason": None})())
    _run(monkeypatch, ASK, motor=motor, commit_history=False, allow_vision=False)
    assert router.submitted == []
    _run(monkeypatch, "busca en internet otra cosa", motor=motor, commit_history=True)
    assert router.submitted == [("Déjame buscarlo.", "direct" + WEB_BRIDGE_SUFFIX)]
    router.submitted.clear()
    _run(monkeypatch, "search the web for the Zelda trailer", motor=motor, commit_history=True)
    assert router.submitted == [("Let me look that up.", "direct" + WEB_BRIDGE_SUFFIX)]


# --- follow-up topic carry (web-trigger-vocab-20261010 T3) ---------------------------


def _ddg_queries(net):
    return [httpx.URL(u).params.get("q") for h, u in net.requests if "duckduckgo.com" in h]


def test_followup_topic_carry_combines_queries(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, seen = _motor(monkeypatch)
    net = Net()
    motor._web_fetcher_factory = net.fetcher
    # Turn 1: previous search runs with query "openai problemas del milenio"
    t1 = "buscar en la web openai problemas del milenio"
    look1 = motor._run_web_step(t1, "direct")
    assert look1.block
    assert _ddg_queries(net)[0] == "openai problemas del milenio"

    # Turn 2: under 45s, an anaphoric follow-up ("eso") with <= 3 content words (K2)
    t2 = "busca en la web mas sobre eso"
    look2 = motor._run_web_step(t2, "direct")
    assert look2.block
    carried_q = _ddg_queries(net)[1]
    assert carried_q == "mas sobre eso openai problemas milenio"
    lines = _web_logs(caplog)
    assert any("subject=carry" in line for line in lines)


def test_followup_topic_carry_does_not_carry_when_long_query(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, seen = _motor(monkeypatch)
    net = Net()
    motor._web_fetcher_factory = net.fetcher
    # Turn 1
    motor._run_web_step("buscar en la web openai problemas del milenio", "direct")

    # Turn 2: query has > 3 content words (matematicos, expertos, fisica, cuantica -> 4 content words)
    t2 = "busca en la web que opinan los matematicos expertos sobre fisica cuantica"
    motor._run_web_step(t2, "direct")
    q = _ddg_queries(net)[1]
    assert "openai" not in q
    assert "problemas" not in q
    lines = _web_logs(caplog)
    # The second search log must be subject=direct
    assert lines[-1].startswith("[WEB] decision=search") and "subject=direct" in lines[-1]


def test_followup_topic_carry_does_not_carry_when_stale(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, seen = _motor(monkeypatch)
    net = Net()
    motor._web_fetcher_factory = net.fetcher

    # Turn 1
    motor._run_web_step("buscar en la web openai problemas del milenio", "direct")
    assert _ddg_queries(net)[0] == "openai problemas del milenio"

    # Make the previous search stale (> 45s)
    prev_q, prev_at = motor._last_web_search
    motor._last_web_search = (prev_q, prev_at - 125.0)
    if motor._web_look_cache is not None:
        key, look, at = motor._web_look_cache
        motor._web_look_cache = (key, look, at - 125.0)

    # Turn 2
    t2 = "busca en la web mas sobre eso"
    motor._run_web_step(t2, "direct")
    q = _ddg_queries(net)[1]
    assert q == "mas sobre eso"
    assert "openai" not in q
    lines = _web_logs(caplog)
    assert lines[-1].startswith("[WEB] decision=search") and "subject=direct" in lines[-1]


# --- honesty & explicit notices (web-trigger-vocab-20261010 T4) -----------------------


def test_anti_bot_error_gives_blocked_note(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, _ = _motor(monkeypatch)
    from tests.test_web_lookup import ANOMALY
    net = Net(search_html=ANOMALY, lite_html=ANOMALY)
    motor._web_fetcher_factory = net.fetcher
    look = motor._run_web_step(ASK, "direct")
    assert look.block == ""
    assert look.note == prompt.WEB_BLOCKED_NOTE
    line = _web_logs(caplog)[0]
    assert "outcome=error" in line and "reason=anti_bot" in line


def test_no_search_with_web_words_gives_not_searched_note(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, seen = _motor(monkeypatch)
    net = Net()
    motor._web_fetcher_factory = net.fetcher

    phrases = [
        "la web de mi proyecto no carga",
        "el internet esta caido",
        "mi busqueda no dio resultados",
        "quiero buscar mis llaves",
        "estoy navegando en linea",
        "no puedo entrar a google",
    ]
    for phrase in phrases:
        look = motor._run_web_step(phrase, "direct")
        assert look.block == ""
        assert look.note == prompt.WEB_NOT_SEARCHED_NOTE, f"failed for: {phrase}"
    assert net.requests == []


def test_plain_chat_without_web_words_returns_no_web(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    look = motor._run_web_step("hola Kira como estas", "direct")
    assert look is NO_WEB


def test_switch_off_with_web_words_returns_no_web(monkeypatch):
    assert web_fetch.is_enabled() is False
    motor, _ = _motor(monkeypatch)
    look = motor._run_web_step("la web de mi proyecto no carga", "direct")
    assert look is NO_WEB


# --- follow-up subject ledger (web-query-followup-and-retry T3) -------------------------

import datetime as _dt  # noqa: E402

from opencohost.core.engine import llm_engine_web  # noqa: E402

_LONG_ASK = (
    "Quiero que busques un modelo de inteligencia artificial en la web. "
    "Que sea lo suficientemente capaz. Como para realizar documentos. Complejos."
)
_BASE_Q = "modelo de inteligencia artificial capaz de realizar documentos complejos"


def _fresh(monkeypatch):
    motor, seen = _motor(monkeypatch)
    net = Net()
    motor._web_fetcher_factory = net.fetcher
    clock = {"t": 0.0}  # an offset on the real clock: the lookup thread keeps real time
    monkeypatch.setattr(llm_engine_web, "_monotonic", lambda: time.monotonic() + clock["t"])
    return motor, net, clock


def test_refinement_chain_then_bare_command_searches_the_original_subject(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, net, clock = _fresh(monkeypatch)
    year = _dt.date.today().year
    motor._run_web_step(_LONG_ASK, "direct")
    clock["t"] += 20
    motor._run_web_step("Puedes buscar opciones que sean más recientes este año.", "direct")
    clock["t"] += 30
    look = motor._run_web_step("Ok, realiza la búsqueda en la web.", "direct")
    assert look.block
    qs = _ddg_queries(net)
    assert qs[0] == _BASE_Q
    assert qs[1] == f"{_BASE_Q} recientes {year}"  # the refinement merged with its base
    assert qs[2] == f"{_BASE_Q} recientes {year}"
    lines = _web_logs(caplog)
    assert "subject=direct" in lines[0] and "subject=refine" in lines[1] and "subject=followup" in lines[2]
    assert "modelo" not in caplog.text and "inteligencia" not in caplog.text


def test_bare_command_searches_the_latest_plain_question(monkeypatch, web_on):
    motor, net, clock = _fresh(monkeypatch)
    motor._run_web_step("¿Cuándo sale Silksong?", "direct")  # not a search: no fetch
    assert net.requests == []
    clock["t"] += 10
    assert motor._run_web_step("sí, búscalo", "direct").block
    assert _ddg_queries(net) == ["Cuándo sale Silksong"]


def test_bare_command_with_an_empty_ledger_fetches_nothing(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, net, _ = _fresh(monkeypatch)
    look = motor._run_web_step("búscalo", "direct")
    assert net.requests == []
    assert look.note == prompt.WEB_NO_SUBJECT_NOTE
    (line,) = _web_logs(caplog)
    assert "subject=followup" in line and "outcome=empty" in line


def test_bare_command_after_the_subject_ttl_fetches_nothing(monkeypatch, web_on):
    motor, net, clock = _fresh(monkeypatch)
    motor._run_web_step("¿Cuándo sale Silksong?", "direct")
    clock["t"] += 601
    assert motor._run_web_step("búscalo", "direct").note == prompt.WEB_NO_SUBJECT_NOTE
    assert net.requests == []


def test_ledger_is_kept_only_for_owner_turns_and_never_for_a_switch_off_motor(monkeypatch):
    motor, net, _ = _fresh(monkeypatch)
    assert motor._run_web_step("¿Cuándo sale Silksong?", "direct") is NO_WEB  # switch off
    assert not getattr(motor, "_owner_web_turns", None)
    web_fetch.set_enabled(True)
    try:
        motor._run_web_step("¿Cuándo sale Silksong?", "chat")
        assert not getattr(motor, "_owner_web_turns", None)
        assert motor._run_web_step("búscalo", "direct").note == prompt.WEB_NO_SUBJECT_NOTE
    finally:
        web_fetch.set_enabled(False)


# --- hard-cap timeout names the stage it stalled in (web-query-followup-and-retry T4) ---


def test_hard_cap_timeout_logs_the_phase_and_normal_lookups_do_not(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    monkeypatch.setattr(settings, "WEB_FETCH_BUDGET_S", 0.3)
    release = threading.Event()

    class Stuck:
        def fetch(self, url, deadline=None):
            release.wait(5)
            raise RuntimeError("late")

    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Stuck
    motor._run_web_step(ASK, "direct")
    release.set()
    (line,) = _web_logs(caplog)
    assert "outcome=timeout" in line and "phase=search_ddg" in line
    assert SECRET not in caplog.text

    caplog.clear()
    monkeypatch.setattr(settings, "WEB_FETCH_BUDGET_S", 8.5)
    motor2, _ = _motor(monkeypatch)
    motor2._web_fetcher_factory = Net().fetcher
    motor2._run_web_step(ASK, "direct")
    (ok_line,) = _web_logs(caplog)
    assert "outcome=ok" in ok_line and "phase=" not in ok_line


# --- judge round 1 (F1, F2, F3, F9, F10) -----------------------------------------------

import itertools  # noqa: E402

from opencohost.core.web.lookup import OUTCOME_OK, WebLookupResult  # noqa: E402
from tests import test_web_subject as _ws  # noqa: E402


def _recording(motor):
    seen: list = []

    def fake(question, query):
        seen.append(query)
        return WebLookupResult(OUTCOME_OK, block="WEB RESULTS")

    motor._web_lookup = fake
    return seen


def test_refinement_after_a_chat_turn_never_sends_the_chat_turn(monkeypatch, web_on):
    motor, net, clock = _fresh(monkeypatch)
    seen = _recording(motor)
    motor._run_web_step("Mi hermana Laura se casa en Toledo el sábado", "direct")
    clock["t"] += 5
    motor._run_web_step("busca en internet el clima de hoy", "direct")
    assert seen == [f"el clima de hoy {datetime.now().year}"]


def test_refinement_without_a_search_base_stays_a_plain_query(monkeypatch, web_on):
    motor, net, clock = _fresh(monkeypatch)
    seen = _recording(motor)
    motor._run_web_step("cuando sale silksong", "direct")
    clock["t"] += 5
    motor._run_web_step("Puedes buscar opciones más recientes este año.", "direct")
    assert len(seen) == 1 and "silksong" not in seen[0].lower()


def test_lone_bare_command_on_a_stale_plain_turn_fetches_nothing(monkeypatch, web_on):
    motor, net, clock = _fresh(monkeypatch)
    seen = _recording(motor)
    motor._run_web_step("¿Cuándo sale Silksong?", "direct")
    clock["t"] += 60
    look = motor._run_web_step("búscalo", "direct")
    assert seen == [] and look.note == prompt.WEB_NO_SUBJECT_NOTE


def test_search_intent_subject_keeps_the_long_ttl_for_a_bare_command(monkeypatch, web_on):
    motor, net, clock = _fresh(monkeypatch)
    seen = _recording(motor)
    motor._run_web_step("busca en internet la fecha de lanzamiento de silksong", "direct")
    clock["t"] += 400
    motor._run_web_step("búscalo", "direct")
    assert seen[1] == seen[0]


def test_every_bare_alternative_and_tail_resolves_the_subject_not_junk(monkeypatch, web_on):
    motor, net, clock = _fresh(monkeypatch)
    seen = _recording(motor)
    motor._run_web_step("busca en internet la fecha de lanzamiento de silksong", "direct")
    subject = seen[0]
    for a, t in itertools.product(_ws._ALTERNATIVES, _ws._TAILS):
        text = f"{a} {t}".strip()
        motor._web_look_cache = None
        clock["t"] += 1
        motor._run_web_step(text, "direct")
        assert seen[-1] == subject, text


def test_no_subject_note_is_not_a_failure_and_not_a_search():
    assert prompt.WEB_NO_SUBJECT_NOTE not in prompt.failure_notes()
    assert "no search" in prompt.WEB_NO_SUBJECT_NOTE.lower() and "ask" in prompt.WEB_NO_SUBJECT_NOTE.lower()
    from opencohost.core.context import prompt_assembler

    assert prompt_assembler.web_flags(prompt.WEB_NO_SUBJECT_NOTE, "") == (False, False)
    assert prompt_assembler.web_flags(prompt.WEB_TIMEOUT_NOTE, "") == (True, True)
    assert prompt_assembler.web_flags(prompt.WEB_NOT_SEARCHED_NOTE, "") == (False, False)
    assert prompt_assembler.web_flags("", "WEB RESULTS") == (True, False)


def test_golden_red_row_goes_through_the_engine_path(monkeypatch, web_on):
    motor, net, clock = _fresh(monkeypatch)
    seen = _recording(motor)
    motor._run_web_step("y busca en la red películas de 2026", "direct")
    assert seen == ["películas de 2026"]


def test_english_bridge_line_in_english_profile_does_not_speak_spanish(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._current_profile_locale = "en"
    router = FakeRouter()
    motor._speech_router_enabled = True
    motor._ensure_router = lambda: router
    monkeypatch.setattr(motor, "_cloud_attempt_loop", lambda setup, **k: type("O", (), {
        "early_return": "ok", "stream": None, "failure_reason": None})())
    _run(monkeypatch, "search online for anthropic models", motor=motor, commit_history=True)
    assert router.submitted == [("Let me look that up.", "direct" + WEB_BRIDGE_SUFFIX)]
    router.submitted.clear()
    _run(monkeypatch, "search for me on google openai", motor=motor, commit_history=True)
    assert router.submitted == [("Let me look that up.", "direct" + WEB_BRIDGE_SUFFIX)]
    router.submitted.clear()
    _run(monkeypatch, "google it", motor=motor, commit_history=True)
    assert router.submitted == [("Let me look that up.", "direct" + WEB_BRIDGE_SUFFIX)]


def test_english_profile_not_searched_and_non_search_guards(monkeypatch, web_on):
    motor, net, _ = _fresh(monkeypatch)
    motor._current_profile_locale = "en"
    for phrase in ("she is on the web", "browser settings"):
        look = motor._run_web_step(phrase, "direct")
        assert look.block == ""
        assert look.note == prompt.WEB_NOT_SEARCHED_NOTE_EN
        assert "busca en internet" not in look.note
    for phrase in ("i searched my memory", "quick look at the code"):
        assert motor._run_web_step(phrase, "direct") is NO_WEB
    assert net.requests == []


def test_english_bare_followup_and_anaphora_carry(monkeypatch, web_on):
    motor, net, clock = _fresh(monkeypatch)
    motor._current_profile_locale = "en"
    motor._run_web_step("when is silksong releasing", "direct")
    assert net.requests == []
    clock["t"] += 5
    look_bare = motor._run_web_step("look it up", "direct")
    assert look_bare.block
    assert _ddg_queries(net)[0] == "when is silksong releasing"

    clock["t"] += 5
    motor._run_web_step("search on google openai millennium problems", "direct")
    clock["t"] += 5
    look_carry = motor._run_web_step("search on web more about that", "direct")
    assert look_carry.block
    carried = _ddg_queries(net)[-1]
    assert "openai" in carried and "millennium" in carried

    clock["t"] += 5
    look_and = motor._run_web_step("and in Europe? search online", "direct")
    assert look_and.block
    carried_and = _ddg_queries(net)[-1]
    assert "openai" in carried_and

