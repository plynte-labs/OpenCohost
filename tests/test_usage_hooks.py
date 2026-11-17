"""T2: ledger record hooks (usage-activity-ledger-20261109)."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from opencohost.config import settings
from opencohost.core.engine import prompt_trace
from opencohost.core.usage import ledger

SECRET = "SECRET-CHAT-TEXT-QUOKKA"


@pytest.fixture(autouse=True)
def _reset():
    ledger._reset_for_tests()
    prompt_trace._reset_for_tests()
    yield
    ledger._reset_for_tests()


def _usage_rows():
    if not __import__("os").path.exists(settings.USAGE_LEDGER_DB):
        return []
    con = sqlite3.connect(settings.USAGE_LEDGER_DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("select * from usage_daily order by kind, model")]
    finally:
        con.close()


def _dump_db_text():
    con = sqlite3.connect(settings.USAGE_LEDGER_DB)
    try:
        return json.dumps([list(r) for t in ("usage_daily", "decision_daily", "meta")
                           for r in con.execute(f"select * from {t}")])
    finally:
        con.close()


# -- record_response ---------------------------------------------------------

def test_record_response_local_dict_sums_durations():
    resp = {"prompt_eval_count": 100, "eval_count": 20, "load_duration": 1_000_000_000,
            "prompt_eval_duration": 500_000_000, "eval_duration": 2_000_000_000}
    ledger.record_response(resp, model="m", is_local=True, kind="vision", profile_id="p")
    [row] = _usage_rows()
    assert (row["prompt_tokens"], row["completion_tokens"], row["compute_ms"]) == (100, 20, 3500)
    assert row["kind"] == "vision" and row["provider"] == "local" and row["profile_id"] == "p"


def test_record_response_local_object():
    resp = SimpleNamespace(prompt_eval_count=7, eval_count=3, eval_duration=1_000_000,
                           load_duration=None, prompt_eval_duration=None)
    ledger.record_response(resp, model="m", is_local=True, kind="topic_scout")
    [row] = _usage_rows()
    assert (row["prompt_tokens"], row["completion_tokens"], row["compute_ms"]) == (7, 3, 1)


def test_record_response_cloud_reads_usage_and_wall_clock():
    resp = {"message": {"content": SECRET}, "usage": {"prompt_tokens": 40, "completion_tokens": 9}}
    ledger.record_response(resp, model="c", is_local=False, kind="turn", source="chat", wall_ms=1234)
    [row] = _usage_rows()
    assert (row["prompt_tokens"], row["completion_tokens"], row["compute_ms"]) == (40, 9, 1234)
    assert row["provider"] == "cloud"


def test_record_response_missing_counters_is_safe():
    ledger.record_response(None, model="m", is_local=True, kind="memoria_judge", wall_ms=50)
    [row] = _usage_rows()
    assert (row["calls"], row["prompt_tokens"], row["compute_ms"]) == (1, 0, 50)


# -- prompt_trace ------------------------------------------------------------

def _begin(source="direct", is_local=True, messages=None):
    return prompt_trace.begin(
        source=source, model="gemma", is_local=is_local,
        messages=messages or [{"role": "system", "content": "s" * 400}, {"role": "user", "content": SECRET}],
        options={},
    )


def test_trace_finish_records_turn_with_durations():
    call = _begin()
    call.finish({"message": {"content": SECRET}, "prompt_eval_count": 120, "eval_count": 30,
                 "load_duration": 10_000_000, "prompt_eval_duration": 40_000_000,
                 "eval_duration": 50_000_000})
    [row] = _usage_rows()
    assert (row["kind"], row["source"], row["model"], row["provider"]) == ("turn", "direct", "gemma", "local")
    assert (row["prompt_tokens"], row["completion_tokens"], row["compute_ms"]) == (120, 30, 100)
    assert row["failures"] == 0
    assert SECRET not in _dump_db_text()


def test_trace_finish_cloud_uses_usage_and_wall_clock(monkeypatch):
    clock = iter([100.0, 100.75])
    monkeypatch.setattr(prompt_trace.time, "monotonic", lambda: next(clock))
    call = _begin(source="chat", is_local=False)
    call.finish({"message": {"content": "x"}, "usage": {"prompt_tokens": 55, "completion_tokens": 11}})
    [row] = _usage_rows()
    assert (row["provider"], row["source"]) == ("cloud", "chat")
    assert (row["prompt_tokens"], row["completion_tokens"], row["compute_ms"]) == (55, 11, 750)


def test_trace_fail_records_failure():
    call = _begin()
    call.fail(RuntimeError(SECRET))
    [row] = _usage_rows()
    assert (row["calls"], row["failures"], row["prompt_tokens"]) == (1, 1, 0)
    assert SECRET not in _dump_db_text()


def test_trace_guardrail_retry_kind_and_agenda_source():
    _begin(source="guardrail_retry").finish({"prompt_eval_count": 1, "eval_count": 1})
    _begin(source="kira-agenda-stop").finish({"prompt_eval_count": 1, "eval_count": 1})
    kinds = {(r["kind"], r["source"]) for r in _usage_rows()}
    assert kinds == {("guardrail_retry", "other"), ("turn", "agenda")}


def test_trace_records_estimated_cache_reuse_as_integer():
    msgs = [{"role": "user", "content": "a" * 400}]
    _begin(messages=msgs).finish({"prompt_eval_count": 100, "eval_count": 1})  # cold: best ratio 0.25
    _begin(messages=msgs).finish({"prompt_eval_count": 40, "eval_count": 1})   # warm: ~60 reused
    [row] = _usage_rows()
    assert row["cache_est_reused"] == 60


def test_trace_cloud_has_no_cache_estimate():
    _begin(is_local=False).finish({"usage": {"prompt_tokens": 5, "completion_tokens": 1}})
    assert _usage_rows()[0]["cache_est_reused"] == 0


def test_trace_survives_ledger_failure(monkeypatch):
    def boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(ledger, "record_call", boom)
    _begin().finish({"prompt_eval_count": 1, "eval_count": 1})  # must not raise
    _begin().fail(ValueError("x"))


def test_trace_respects_kill_switch(monkeypatch):
    monkeypatch.setattr(settings, "USAGE_LEDGER_ENABLED", False)
    _begin().finish({"prompt_eval_count": 1, "eval_count": 1})
    assert _usage_rows() == []


def test_trace_uses_registered_profile_id():
    ledger.set_profile_provider(lambda: "kira-profile")
    _begin().finish({"prompt_eval_count": 1, "eval_count": 1})
    assert _usage_rows()[0]["profile_id"] == "kira-profile"


def test_unknown_profile_is_empty_string():
    _begin().finish({"prompt_eval_count": 1, "eval_count": 1})
    assert _usage_rows()[0]["profile_id"] == ""


# -- decision bus ------------------------------------------------------------

def test_decision_resolve_records_enums_once():
    from opencohost.config import decisions as dc
    from opencohost.core.decision import DecisionBus

    dc.set_decision("vision", mode="on")

    class Backend:
        def available(self):
            return True

        def ask(self, state, questions, timeout_s):
            return {"ok": True, "answers": {"vision": {"choice": "look", "answer_confidence": 0.99}}}

    turn = DecisionBus(Backend()).decide(SECRET, "direct", ids=("vision",))
    turn.resolve("vision", "no_look")
    turn.resolve("vision", "no_look")  # cached: no recount
    con = sqlite3.connect(settings.USAGE_LEDGER_DB)
    rows = con.execute("select decision_id,mode,acted_by,status,count from decision_daily").fetchall()
    con.close()
    assert rows == [("vision", "on", "laya", "ok", 1)]
    assert "look" not in _dump_db_text().replace("no_look", "")  # values never stored
    assert SECRET not in _dump_db_text()


def test_decision_resolve_survives_ledger_failure(monkeypatch):
    from opencohost.config import decisions as dc
    from opencohost.core.decision import DecisionBus

    dc.set_decision("vision", mode="on")

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ledger, "record_decision", boom)

    class Backend:
        def available(self):
            return True

        def ask(self, state, questions, timeout_s):
            return {"ok": True, "answers": {"vision": {"choice": "look", "answer_confidence": 0.99}}}

    out = DecisionBus(Backend()).decide("x", "direct", ids=("vision",)).resolve("vision", "no_look")
    assert out is not None


# -- vision describer --------------------------------------------------------

def test_vision_describer_records_vision_call():
    from opencohost.core.vision.describer import OllamaVisionDescriber

    body = {"message": {"role": "assistant", "content": SECRET}, "done": True,
            "prompt_eval_count": 300, "eval_count": 40, "load_duration": 5_000_000,
            "prompt_eval_duration": 20_000_000, "eval_duration": 30_000_000}
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)))
    d = OllamaVisionDescriber(client=client, lang="en", model="qwen-vl")
    d.describe(b"\xff\xd8\xff\xe0jpeg\xff\xd9", "what is this?", timeout_s=5.0)
    [row] = _usage_rows()
    assert (row["kind"], row["model"], row["provider"]) == ("vision", "qwen-vl", "local")
    assert (row["prompt_tokens"], row["completion_tokens"], row["compute_ms"]) == (300, 40, 55)
    assert SECRET not in _dump_db_text() and "what is this" not in _dump_db_text()


def test_vision_describer_failure_records_nothing():
    from opencohost.core.vision.describer import OllamaVisionDescriber, VisionDescribeError

    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    d = OllamaVisionDescriber(client=client, lang="en", model="qwen-vl")
    with pytest.raises(VisionDescribeError):
        d.describe(b"\xff\xd8\xff\xe0jpeg\xff\xd9", "q", timeout_s=5.0)
    assert _usage_rows() == []


# -- web search ----------------------------------------------------------------

def test_web_lookup_execution_counts_one_call_no_tokens(monkeypatch):
    from opencohost.core.engine import llm_engine_web as web
    from opencohost.core.web.lookup import OUTCOME_OK, WebLookupResult

    monkeypatch.setattr(web, "run_lookup", lambda *a, **k: WebLookupResult(OUTCOME_OK, block="b"))
    fake = SimpleNamespace(_web_fetcher_factory=lambda: object(), _current_profile_id="kira")
    web.WebTurnMixin._web_lookup(fake, f"question {SECRET}", f"query {SECRET}")
    [row] = _usage_rows()
    assert (row["kind"], row["calls"], row["prompt_tokens"], row["completion_tokens"]) == ("web_search", 1, 0, 0)
    assert row["profile_id"] == "kira"
    assert SECRET not in _dump_db_text()


def test_web_lookup_cache_hit_is_not_counted(monkeypatch):
    """A cached WebLook returns before ``_web_lookup``; only real executions count."""
    import inspect

    from opencohost.core.engine import llm_engine_web as web

    src = inspect.getsource(web.WebTurnMixin)
    assert src.index("return cached[1]") < src.index("self._web_lookup(question, ")
    assert "record_web_search" in inspect.getsource(web.WebTurnMixin._web_lookup)


# -- topic scout / memoria judge ----------------------------------------------

def test_scout_records_topic_scout_call(monkeypatch):
    from tests.test_topic_scout import _make_scout_motor

    def chat(**kwargs):
        return {"message": {"content": "Tema uno\nTema dos"}, "prompt_eval_count": 80,
                "eval_count": 12, "eval_duration": 4_000_000_000}

    motor, _fake = _make_scout_motor(monkeypatch, chat_impl=chat)
    motor._current_profile_id = "kira"
    motor.scout_digest()
    rows = [r for r in _usage_rows() if r["kind"] == "topic_scout"]
    assert len(rows) == 1
    assert (rows[0]["model"], rows[0]["prompt_tokens"], rows[0]["completion_tokens"],
            rows[0]["compute_ms"], rows[0]["profile_id"]) == ("m", 80, 12, 4000, "kira")
    assert "Tema" not in _dump_db_text()


def test_memoria_judge_records_memoria_judge_call(monkeypatch):
    from tests.test_topic_scout import _make_scout_motor

    motor, _fake = _make_scout_motor(monkeypatch, chat_impl=lambda **k: {})
    motor._current_profile_id = "kira"
    resp = {"message": {"content": "{}"}, "prompt_eval_count": 200, "eval_count": 30,
            "eval_duration": 1_000_000_000}
    motor._ollama_chat_with_watchdog = lambda **kw: resp
    try:
        motor._run_promotion_judge([{"content": SECRET}], chat_callable=lambda **k: resp)
    except Exception:
        pass  # parsing the stub reply is not under test; the call is
    rows = [r for r in _usage_rows() if r["kind"] == "memoria_judge"]
    assert len(rows) == 1 and rows[0]["prompt_tokens"] == 200 and rows[0]["profile_id"] == "kira"
    assert SECRET not in _dump_db_text()


# -- owner reflection -----------------------------------------------------------

def test_reflection_records_final_done_chunk_counters(monkeypatch, tmp_path):
    from tests.owner_reflection_support import (
        FakeHandle, fact_json, feed, install_stream, make_motor, run_job,
    )

    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    reply = fact_json(("Q?", "A.", (1,)))
    lines = [
        json.dumps({"message": {"role": "assistant", "content": reply}, "done": False}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": 500, "eval_count": 70, "load_duration": 0,
                    "prompt_eval_duration": 300_000_000, "eval_duration": 700_000_000}),
    ]
    install_stream(monkeypatch, motor, handle=FakeHandle(lines))
    run_job(motor)
    rows = [r for r in _usage_rows() if r["kind"] == "reflection"]
    assert len(rows) == 1
    assert (rows[0]["prompt_tokens"], rows[0]["completion_tokens"], rows[0]["compute_ms"]) == (500, 70, 1000)
    assert rows[0]["profile_id"] == "profile-1" and rows[0]["model"] == "m"
    assert "owner statement" not in _dump_db_text()


# --- cloud calls are booked under the cloud model, not the local one --------

_CLOUD_CFG = {"active_provider": "gemini",
              "profiles": {"gemini": {"model": "gemini-3.5-flash"}}}


def test_call_model_name_local_keeps_the_request_model():
    assert ledger.call_model_name("gemma4:e4b", is_local=True, provider_cfg=_CLOUD_CFG) == "gemma4:e4b"


def test_call_model_name_cloud_uses_the_active_profile_model():
    assert (ledger.call_model_name("gemma4:e4b", is_local=False, provider_cfg=_CLOUD_CFG)
            == "gemini-3.5-flash")


@pytest.mark.parametrize("cfg", [None, {}, {"active_provider": "gemini"},
                                 {"active_provider": "gemini", "profiles": {"gemini": {}}},
                                 {"active_provider": "gemini", "profiles": {"gemini": "bad"}},
                                 "not-a-dict"])
def test_call_model_name_cloud_without_a_profile_model_falls_back(cfg):
    assert ledger.call_model_name("gemma4:e4b", is_local=False, provider_cfg=cfg) == "gemma4:e4b"
