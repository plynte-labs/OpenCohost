"""runtime-fixes-20261110 T5: code-built source attribution.

A web turn carries the real sources (host + https URL) with the reply, in memory,
to the desktop API only. The model's own trailing "Fuente: ..." line is removed.
No network: fetchers are ``SafeFetcher`` over ``httpx.MockTransport``.
"""
from __future__ import annotations

import logging
import queue
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from opencohost.api.engine_host import ChatReplySink
from opencohost.api.models import ChatLastReplyResponse
from opencohost.config import web_fetch
from opencohost.core import llm_engine
from opencohost.core.web import lookup, sources
from tests.test_decision_engine_wiring import _motor
from tests.test_link_read import ASK as LINK_ASK, HOST as LINK_HOST, PAGE as LINK_PAGE, URL as LINK_URL
from tests.test_web_lookup import Laya, Net

ASK = "busca en internet zanahoriaconfidencial parche nuevo"


@pytest.fixture
def web_on():
    web_fetch.set_enabled(True)
    yield
    web_fetch.set_enabled(False)


# --- the pure helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    "value, host",
    [
        ("https://www.Example.COM/a/b?x=1#f", "example.com"),
        ("https://user:pw@news.example.org:8443/x", "news.example.org"),
        ("WWW.Example.com", "example.com"),
        ("sub.example.net", "sub.example.net"),
        ("https://example.com./x", "example.com"),
        ("", ""),
        (None, ""),
    ],
)
def test_bare_host(value, host):
    assert sources.bare_host(value) == host


def test_make_source_keeps_the_https_url_and_a_bare_host():
    assert sources.make_source("https://www.example.com/a?id=1#top") == (
        "example.com",
        "https://www.example.com/a?id=1#top",
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a",
        "ftp://example.com/a",
        "javascript:alert(1)",
        "file:///C:/x",
        "//example.com/a",
        "example.com/a",
        "https://user:pw@example.com/a",
        "https://example.com/a b",
        "https://example.com/\x00",
        "https:///nohost",
        "https://example.com:notaport/",
        "",
        None,
        42,
    ],
)
def test_make_source_refuses_everything_that_is_not_plain_https(url):
    assert sources.make_source(url) is None


def test_collect_dedupes_by_host_keeps_order_and_caps():
    urls = [
        "https://a.com/1",
        "http://b.com/x",  # dropped, not https
        "https://www.a.com/2",  # same host as the first
        "https://c.com/3",
        "https://d.com/4",
        "https://e.com/5",
    ]
    assert sources.collect(urls) == (
        ("a.com", "https://a.com/1"),
        ("c.com", "https://c.com/3"),
        ("d.com", "https://d.com/4"),
    )
    assert sources.MAX_SOURCES == 3


def test_merge_and_wire_shape_and_log_hosts():
    a = (("a.com", "https://a.com/1"),)
    b = (("a.com", "https://a.com/9"), ("b.com", "https://b.com/2"))
    merged = sources.merge(a, b)
    assert merged == (("a.com", "https://a.com/1"), ("b.com", "https://b.com/2"))
    assert sources.as_dicts(merged) == [
        {"host": "a.com", "url": "https://a.com/1"},
        {"host": "b.com", "url": "https://b.com/2"},
    ]
    assert sources.hosts_for_log(merged) == "a.com,b.com"
    assert sources.hosts_for_log(()) == "-"
    assert "https" not in sources.hosts_for_log(merged)


# --- the model's own source line ------------------------------------------------


@pytest.mark.parametrize(
    "text, kept",
    [
        ("Hay parche el viernes.\nFuente: wikipedia", "Hay parche el viernes."),
        ("Hay parche el viernes.\nFuentes: nvidia, wikipedia.", "Hay parche el viernes."),
        ("The patch ships Friday.\nSource: wikipedia", "The patch ships Friday."),
        ("The patch ships Friday.\nSources: nvidia.com and more", "The patch ships Friday."),
        ("The patch ships Friday.\n**Source:** nvidia.com", "The patch ships Friday."),
        ("The patch ships Friday.\n[Source: nvidia]", "The patch ships Friday."),
        ("The patch ships Friday.\n(Fuente: nvidia)", "The patch ships Friday."),
        ("The patch ships Friday.\n_Fuente: nvidia_\n", "The patch ships Friday."),
        ("The patch ships Friday.\n\nSource: a\n\n", "The patch ships Friday."),
        ("Hay parche el viernes. Fuente: wikipedia.", "Hay parche el viernes."),
        ("The patch ships Friday. [Source: nvidia.com]", "The patch ships Friday."),
        ("Hay parche.\nFuentes:\n- nvidia\n- wikipedia", "Hay parche."),
        ("Hay parche.\nFuentes:\n1. nvidia\n2. wikipedia", "Hay parche."),
        ("Uno.\nDos.\nFUENTE: nvidia", "Uno.\nDos."),
        ("Hay parche. Fuente: a. Fuente: b.", "Hay parche."),
    ],
)
def test_a_trailing_source_attribution_is_removed(text, kept):
    assert sources.strip_trailing_source(text) == kept


@pytest.mark.parametrize(
    "text",
    [
        # es: the word as a plain noun, mid-reply or final, never a label.
        "La fuente de energía es el sol.",
        "Dijo que la fuente es confiable y que el parche sale el viernes.",
        "Fuentes de energía renovable hay muchas, y el viento es una.",
        "Esa fuente de información es buena. Y el parche sale el viernes.",
        # en
        "Source code is open and the patch ships Friday.",
        "The source of the leak is unknown.",
        "Sources say the patch ships Friday.",
        "The source: a leak, said the patch ships Friday. More news soon.",
        # a label mid-reply that is followed by more reply is not a trailing line
        "Fuente: wikipedia. Y además el parche sale el viernes.",
        "Hay parche. Fuente: wikipedia. El viernes sale.",
        "Source: wikipedia.\nThe patch ships Friday.",
        # the whole reply would vanish: kept as is
        "Fuente: wikipedia",
        "Source: nvidia.com",
        # empty / not text
        "",
        "   ",
    ],
)
def test_ordinary_sentences_and_non_trailing_labels_are_never_touched(text):
    assert sources.strip_trailing_source(text) == text


def test_strip_is_safe_for_non_strings():
    assert sources.strip_trailing_source(None) is None


@pytest.mark.parametrize(
    "line, expected",
    [
        ("Fuente: wikipedia", True),
        ("Fuentes: a, b", True),
        ("Source: x", True),
        ("**Sources:** x", True),
        ("[Fuente: x]", True),
        ("La fuente de energía es el sol.", False),
        ("Source code is open.", False),
        ("Hay parche.", False),
        ("", False),
    ],
)
def test_is_source_line(line, expected):
    assert sources.is_source_line(line) is expected


# --- the lookup keeps the URLs of what went into the prompt -----------------------


def _run_lookup(net=None, backend=None):
    net = net or Net()
    result = lookup.run_lookup(
        "busca en internet patch date", "patch date", fetcher=net.fetcher(), backend=backend, deadline=None
    )
    return result, net


def test_lookup_sources_are_the_prompt_sources_in_prompt_order():
    result, _ = _run_lookup(backend=Laya({"Shop C": 0.01, "Wiki B": 0.9, "News A": 0.8, "boss": 0.95, "Friday": 0.7}))
    assert result.outcome == "ok"
    # The block lists Wiki B before News A; the shop result was never kept.
    assert result.sources == (
        ("wiki.example.org", "https://wiki.example.org/b"),
        ("news.example.com", "https://news.example.com/a?id=1"),
    )


def test_lookup_without_results_has_no_sources():
    net = Net(search_html="<html><body></body></html>")
    result, _ = _run_lookup(net)
    assert result.outcome != "ok"
    assert result.sources == ()


def test_lookup_drops_a_result_that_is_not_https():
    html = (
        '<html><body>'
        '<div class="result"><a class="result__a" href="http://plain.example.com/a">Plain</a>'
        '<a class="result__snippet">Snippet plain about the patch</a></div>'
        '<div class="result"><a class="result__a" href="https://secure.example.org/b">Secure</a>'
        '<a class="result__snippet">Snippet secure about the patch</a></div>'
        "</body></html>"
    )
    net = Net(search_html=html, pages={"secure.example.org": "<p>" + "palabra " * 30 + "</p>"})
    result, _ = _run_lookup(net, backend=Laya())
    assert [h for h, _ in result.sources] == ["secure.example.org"]
    assert all(u.startswith("https://") for _, u in result.sources)


# --- the reply carries them (engine wiring) ---------------------------------------


def test_a_search_turn_returns_the_sources_with_the_reply(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    result = motor._generar_dialogo(ASK, source="direct", commit_history=True)
    assert result.web_searched is True
    assert 1 <= len(result.web_sources) <= 3
    for host, url in result.web_sources:
        assert url.startswith("https://") and host == sources.bare_host(url)
    assert len({h for h, _ in result.web_sources}) == len(result.web_sources)


def test_a_plain_turn_has_no_sources(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    result = motor._generar_dialogo("hola Kira zanahoriaconfidencial", source="direct", commit_history=True)
    assert result.web_sources == ()


def test_a_failed_lookup_has_no_sources(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net(search_html="<html><body></body></html>").fetcher
    result = motor._generar_dialogo(ASK, source="direct", commit_history=True)
    assert result.web_searched is True
    assert result.web_sources == ()


def test_web_off_has_no_sources(monkeypatch):
    web_fetch.set_enabled(False)
    motor, _ = _motor(monkeypatch)
    result = motor._generar_dialogo(ASK, source="direct", commit_history=True)
    assert result.web_sources == ()


def test_a_read_link_is_the_one_source(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net(pages={LINK_HOST: LINK_PAGE}).fetcher
    result = motor._generar_dialogo(LINK_ASK, source="direct", commit_history=True, origin="desktop")
    assert result.web_sources == ((LINK_HOST, LINK_URL),)


def test_a_remote_turn_with_a_link_has_no_sources(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net(pages={LINK_HOST: LINK_PAGE}).fetcher
    result = motor._generar_dialogo(LINK_ASK, source="direct", commit_history=True, origin="remote")
    assert result.web_sources == ()


def test_a_non_https_link_is_not_a_source(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net(pages={LINK_HOST: LINK_PAGE}).fetcher
    result = motor._generar_dialogo(
        f"Kira, que dice http://{LINK_HOST}/x?id=1 sobre el parche?",
        source="direct", commit_history=True, origin="desktop",
    )
    assert result.web_sources == ()


# --- the emit seam and the sink ---------------------------------------------------


def test_emit_dialogue_sends_sources_only_when_there_are_some():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    seen: list[dict] = []
    motor.dialogue_callback = lambda text, source, **kw: seen.append(kw) or 1
    src = (("a.com", "https://a.com/1"),)
    motor._emit_dialogue("t", "direct", web_searched=True, web_sources=src)
    motor._emit_dialogue("t", "direct", web_searched=True)
    motor._emit_dialogue("t", "direct", queue_wait_ms=5, web_searched=True, web_sources=src)
    assert seen[0] == {"web_searched": True, "web_sources": [{"host": "a.com", "url": "https://a.com/1"}]}
    assert seen[1] == {"web_searched": True}
    assert seen[2]["web_sources"] == [{"host": "a.com", "url": "https://a.com/1"}]


def test_sink_keeps_sources_in_memory_and_the_stream_tail_is_updated():
    sink = ChatReplySink()
    sink.record_stream_chunk("s1", "Hola.", ["Hola."], "direct", web_searched=True)
    turn = sink.record(
        "Hola.", "direct", web_searched=True, web_sources=[{"host": "a.com", "url": "https://a.com/1"}]
    )
    last = sink.last()
    assert last["turn_id"] == turn
    assert last["web_sources"] == [{"host": "a.com", "url": "https://a.com/1"}]
    sink.record("Otra.", "direct")
    assert sink.last().get("web_sources") in (None, [])


def test_sink_rejects_a_non_https_or_malformed_source_defensively():
    sink = ChatReplySink()
    sink.record(
        "t", "direct", web_searched=True,
        web_sources=[
            {"host": "a.com", "url": "http://a.com/1"},
            {"host": "x", "url": 5},
            "nope",
            {"host": "ok.com", "url": "https://ok.com/1"},
        ],
    )
    assert sink.last()["web_sources"] == [{"host": "ok.com", "url": "https://ok.com/1"}]


def test_last_reply_response_model_carries_the_sources():
    reply = ChatLastReplyResponse(
        text="t", source="kira", turn_id=1, ts=1.0,
        web_searched=True, web_sources=[{"host": "a.com", "url": "https://a.com/1"}],
    )
    assert reply.model_dump()["web_sources"] == [{"host": "a.com", "url": "https://a.com/1"}]
    assert ChatLastReplyResponse(text=None, source=None, turn_id=0, ts=None).web_sources is None


def test_the_desktop_api_returns_the_sources():
    import opencohost.api.main as main_mod
    from tests.test_api_phase1 import FakeHost

    main_mod._host_active = False
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])
    try:
        with TestClient(app) as client:
            sink = app.state.host.chat_sink
            turn = sink.record(
                "Hay parche.", "direct", web_searched=True,
                web_sources=[{"host": "a.com", "url": "https://a.com/1"}],
            )
            body = client.get("/api/chat/last-reply").json()
            assert body["web_sources"] == [{"host": "a.com", "url": "https://a.com/1"}]
            assert client.get("/api/chat/last-reply", params={"turn_id": turn}).json()["web_sources"]
    finally:
        main_mod._host_active = False


# --- the remote gateway never gets them -------------------------------------------


def test_the_gateway_reply_to_the_phone_has_no_sources():
    from tests.test_remote_gateway_turns import Rig

    rig = Rig()
    _, headers = rig.pair()
    after = rig.turn(headers).json()["after"]
    turn_id = rig.host.chat_sink.record(
        "Hay parche el viernes.", "direct", web_searched=True,
        web_sources=[{"host": "a.com", "url": "https://a.com/1"}],
    )
    body = rig.reply(headers, after).json()
    assert body == {"status": "ready", "text": "Hay parche el viernes.", "turn_id": turn_id}
    assert "a.com" not in str(body) and "source" not in str(body).replace("status", "")


def test_the_gateway_module_never_reads_the_sources_field():
    import inspect

    from opencohost.gateway import turns

    assert "web_sources" not in inspect.getsource(turns)


# --- never persisted, never logged beyond count and bare hosts ----------------------


def test_sources_are_not_in_history_or_logs_and_the_log_has_count_and_hosts(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    result = motor._generar_dialogo(ASK, source="direct", commit_history=True)
    assert result.web_sources
    urls = [u for _, u in result.web_sources]
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    for url in urls:
        assert url not in log_text
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[WEB_SOURCES]")]
    assert len(lines) == 1
    assert f"count={len(urls)}" in lines[0]
    assert all(h in lines[0] for h, _ in result.web_sources)
    assert "https" not in lines[0] and "/" not in lines[0].split("hosts=")[1]
    for message in motor.historial:
        for url in urls:
            assert url not in str(message.get("content", ""))


def test_a_turn_without_sources_logs_no_sources_line(monkeypatch, caplog, web_on):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    motor._generar_dialogo("hola Kira zanahoriaconfidencial", source="direct", commit_history=True)
    assert not [r for r in caplog.records if r.getMessage().startswith("[WEB_SOURCES]")]


def test_the_generation_result_is_never_the_text_that_is_committed():
    from opencohost.core.engine.generation_orchestrator import GenerationResult

    text = GenerationResult("Hola", web_searched=True, web_sources=(("a.com", "https://a.com/1"),))
    assert str(text) == "Hola" and text == "Hola"
    assert "a.com" not in str(text)


# --- the trailing source line, buffered and streamed ---------------------------------


def _answer(motor, content):
    motor._ollama_chat_with_watchdog = lambda **kw: {"message": {"content": content}}


def test_buffered_web_turn_drops_the_trailing_source_line(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    _answer(motor, "Hay parche el viernes.\nFuente: wikipedia")
    result = motor._generar_dialogo(ASK, source="direct", commit_history=True)
    assert str(result) == "Hay parche el viernes."
    assert "Fuente" not in str(motor.historial[-1]["content"])


def test_buffered_web_turn_drops_an_english_final_source_sentence(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    _answer(motor, "The patch ships Friday. Source: wikipedia.")
    result = motor._generar_dialogo(ASK, source="direct", commit_history=True)
    assert str(result) == "The patch ships Friday."


def test_buffered_web_turn_keeps_a_plain_sentence_about_a_source(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    _answer(motor, "La fuente de energía es el sol y el parche sale el viernes.")
    result = motor._generar_dialogo(ASK, source="direct", commit_history=True)
    assert str(result) == "La fuente de energía es el sol y el parche sale el viernes."


def test_a_turn_without_web_context_is_never_stripped(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    _answer(motor, "Eso es un dato.\nFuente: mi memoria")
    result = motor._generar_dialogo("hola Kira zanahoriaconfidencial", source="direct", commit_history=True)
    assert str(result) == "Eso es un dato.\nFuente: mi memoria"


# Streamed path -----------------------------------------------------------------------


class _Msg:
    def __init__(self, content=""):
        self.content = content
        self.thinking = None


class _Chunk:
    def __init__(self, content="", done=False):
        self.message = _Msg(content)
        self.done = done

    def get(self, key, default=None):
        return getattr(self, key, default)


class _Job:
    job_id = 1
    chunks: list = []


class _Router:
    def __init__(self):
        self.spoken = []

    def submit_streaming(self, source, priority, **kwargs):
        return _Job()

    def append_chunks(self, job, chunks):
        self.spoken.extend(chunks)
        return True

    def seal(self, job):
        pass


@pytest.fixture
def streaming_motor(monkeypatch, web_on):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    router = _Router()
    m._ensure_router = lambda: router
    m._speech_router = router
    m._router = router
    m._emitted = []
    m._emit_dialogue = lambda text, source, **kw: m._emitted.append((str(text), kw))
    m._speak_or_submit = MagicMock()
    m._web_fetcher_factory = Net().fetcher
    return m


def _arm(motor, texts):
    chunks = [_Chunk(t) for t in texts] + [_Chunk("", done=True)]

    def _stream(*, timeout, **kw):
        def _gen():
            yield from chunks

        return _gen()

    motor._ollama_chat_streaming = _stream


def test_streamed_web_turn_never_speaks_the_trailing_source_sentence(streaming_motor):
    m = streaming_motor
    _arm(m, ["Hay parche el viernes. ", "Fuente: wikipedia."])
    result = m._generar_dialogo(ASK, source="direct", commit_history=True)
    spoken = " ".join(m._router.spoken)
    assert "parche" in spoken
    assert "Fuente" not in spoken and "wikipedia" not in spoken
    assert str(result) == "Hay parche el viernes."


def test_streamed_web_turn_speaks_a_label_sentence_when_more_reply_follows(streaming_motor):
    m = streaming_motor
    _arm(m, ["Hay parche el viernes. ", "Fuente: wikipedia. ", "Y ademas trae un jefe nuevo."])
    m._generar_dialogo(ASK, source="direct", commit_history=True)
    spoken = " ".join(m._router.spoken)
    assert "wikipedia" in spoken and "jefe nuevo" in spoken


def test_streamed_turn_without_web_context_speaks_everything(monkeypatch, streaming_motor):
    m = streaming_motor
    _arm(m, ["Eso es un dato. ", "Fuente: mi memoria."])
    m._generar_dialogo("hola Kira zanahoriaconfidencial", source="direct", commit_history=True)
    assert "memoria" in " ".join(m._router.spoken)


# --- the prompt tells the model not to write a source line ------------------------


def _bundle_value(code, key):
    import yaml

    with open(f"opencohost/locales/{code}/manifest.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["llm"]["scaffolding"][key]


def _prompt_text(seen):
    return "\n".join(str(m.get("content", "")) for m in seen["messages"][0])


def test_the_no_source_line_exists_in_both_bundles_and_the_builtin():
    from opencohost.i18n import active as i18n_active

    es = _bundle_value("es", "web_no_source_line")
    en = _bundle_value("en", "web_no_source_line")
    assert es.strip() and en.strip() and es != en
    assert "Fuente" in es and "fuentes" in es.lower()
    assert "Source" in en and "sources" in en.lower()
    assert i18n_active.LEGACY_WEB_NO_SOURCE_LINE == i18n_active.web_no_source_line() or i18n_active.web_no_source_line() in (es, en)
    # Spanish copy is neutral: no voseo.
    assert not any(word in es.lower() for word in ("escribís", "tenés", "podés", "vos "))


def test_a_search_turn_with_results_carries_the_no_source_line(monkeypatch, web_on):
    from opencohost.i18n import active as i18n_active

    motor, seen = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    motor._generar_dialogo(ASK, source="direct", commit_history=True)
    assert i18n_active.web_no_source_line() in _prompt_text(seen)


def test_a_read_link_turn_carries_the_no_source_line(monkeypatch, web_on):
    from opencohost.i18n import active as i18n_active

    motor, seen = _motor(monkeypatch)
    motor._web_fetcher_factory = Net(pages={LINK_HOST: LINK_PAGE}).fetcher
    motor._generar_dialogo(LINK_ASK, source="direct", commit_history=True, origin="desktop")
    assert i18n_active.web_no_source_line() in _prompt_text(seen)


def test_plain_and_failed_lookup_turns_do_not_carry_it(monkeypatch, web_on):
    from opencohost.i18n import active as i18n_active

    motor, seen = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    motor._generar_dialogo("hola Kira zanahoriaconfidencial", source="direct", commit_history=True)
    motor2, seen2 = _motor(monkeypatch)
    motor2._web_fetcher_factory = Net(search_html="<html><body></body></html>").fetcher
    motor2._generar_dialogo(ASK, source="direct", commit_history=True)
    line = i18n_active.web_no_source_line()
    assert line not in _prompt_text(seen)
    assert line not in _prompt_text(seen2)
