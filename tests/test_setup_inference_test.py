"""POST /api/setup/inference-test (setup-assistant T2).

The HTTP layer (Ollama stream, cloud stream) is replaced with scripted fakes and
a fake clock, so no test touches the network or sleeps.
"""

import logging

import pytest
import requests
from fastapi.testclient import TestClient

from opencohost.core.engine import setup_inference
from opencohost.core.providers.cloud.cloud_llm_client import CloudLLMResponseError
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _final(load_ms=0.0, eval_count=20, eval_ms=1000.0):
    return {
        "done": True,
        "load_duration": int(load_ms * 1e6),
        "eval_count": eval_count,
        "eval_duration": int(eval_ms * 1e6),
    }


def _local_fake(clock, scripts, calls=None):
    """Each script: dict(first_ms, load_ms, eval_count, eval_ms, thinking)
    or an Exception raised when the stream is opened."""
    scripts = list(scripts)

    def stream(model, messages, options, timeout):
        if calls is not None:
            calls.append(
                {"model": model, "messages": messages, "options": options, "timeout": timeout}
            )
        script = scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        clock.t += script.get("first_ms", 100) / 1000.0
        key = "thinking" if script.get("thinking") else "content"
        yield {"message": {key: "Hi"}, "done": False}
        yield {"message": {"content": " there"}, "done": False}
        yield _final(
            script.get("load_ms", 0), script.get("eval_count", 20), script.get("eval_ms", 1000)
        )

    return stream


class _Motor:
    def __init__(self, generating=False, cfg=None):
        self.current_model = "qwen3:1.7b"
        self.llm_generating = generating
        self._provider_config = cfg or {"active_provider": "local", "profiles": {}}


class _Host:
    def __init__(self, **kw):
        self.motor = _Motor(**kw)


def _run(clock, scripts, runs=3, host=None, calls=None):
    return setup_inference.run_inference_test(
        host or _Host(), runs, local_stream=_local_fake(clock, scripts, calls), clock=clock
    )


def test_local_passed_reports_median_of_warm_runs_and_cold_load():
    clock = _Clock()
    out = _run(
        clock,
        [
            {"first_ms": 4000, "load_ms": 3500, "eval_count": 20, "eval_ms": 2000},  # cold
            {"first_ms": 300, "eval_count": 30, "eval_ms": 1000},
            {"first_ms": 100, "eval_count": 10, "eval_ms": 1000},
            {"first_ms": 200, "eval_count": 20, "eval_ms": 1000},
        ],
    )
    assert out["status"] == "passed" and out["error_code"] is None
    assert out["provider"] == "local" and out["model"] == "qwen3:1.7b"
    assert out["cold_load_ms"] == 3500
    assert out["metrics"]["first_token_ms"] == pytest.approx(200)
    assert out["metrics"]["tokens_per_second"] == pytest.approx(20)
    assert len(out["runs"]) == 3 and all(r["ok"] for r in out["runs"])
    assert [r["first_token_ms"] for r in out["runs"]] == pytest.approx([300, 100, 200])


def test_slow_when_first_token_over_2500():
    out = _run(_Clock(), [{}, {"first_ms": 3000}], runs=1)
    assert out["status"] == "slow"


def test_slow_when_tokens_per_second_under_8():
    out = _run(_Clock(), [{}, {"eval_count": 4, "eval_ms": 1000}], runs=1)
    assert out["status"] == "slow" and out["metrics"]["tokens_per_second"] == pytest.approx(4)


def test_thinking_chunk_counts_as_first_token():
    out = _run(_Clock(), [{}, {"first_ms": 150, "thinking": True}], runs=1)
    assert out["runs"][0]["first_token_ms"] == pytest.approx(150)


def test_missing_eval_metrics_are_null_never_zero():
    clock = _Clock()

    def stream(model, messages, options, timeout):
        clock.t += 0.1
        yield {"message": {"content": "x"}, "done": False}
        yield {"done": True}

    out = setup_inference.run_inference_test(_Host(), 1, local_stream=stream, clock=clock)
    run = out["runs"][0]
    assert run["tokens_per_second"] is None and run["load_ms"] is None
    assert out["metrics"]["tokens_per_second"] is None
    assert out["status"] == "passed"


def test_prompt_is_fixed_thinking_off_and_capped():
    calls = []
    out = _run(_Clock(), [{}, {}], runs=1, calls=calls)
    assert out["status"] == "passed"
    assert len(calls) == 2  # one cold + one warm
    for call in calls:
        assert call["options"]["think"] is False
        assert call["options"]["num_predict"] <= 64
        assert [m["role"] for m in call["messages"]] == ["user"]
        assert call["timeout"] > 0


def test_runs_bounds():
    for bad in (0, 6):
        with pytest.raises(ValueError):
            setup_inference.run_inference_test(_Host(), bad)


@pytest.mark.parametrize(
    "exc, code",
    [
        (ConnectionError("Failed to connect to Ollama"), "ollama_unreachable"),
        (TimeoutError("slow"), "timeout"),
    ],
)
def test_local_error_mapping(exc, code):
    out = _run(_Clock(), [exc])
    assert out["status"] == "failed" and out["error_code"] == code
    assert out["metrics"] == {"load_ms": None, "first_token_ms": None, "tokens_per_second": None}
    assert out["runs"] == []


def test_model_not_found_maps_to_model_missing():
    class NotFound(Exception):
        status_code = 404

    out = _run(_Clock(), [NotFound("model not found")])
    assert out["status"] == "failed" and out["error_code"] == "model_missing"


def test_warm_run_failure_keeps_ok_runs_and_flags_failed_ones():
    out = _run(_Clock(), [{}, {"first_ms": 100}, ConnectionError("x"), {"first_ms": 300}], runs=3)
    assert [r["ok"] for r in out["runs"]] == [True, False, True]
    assert out["status"] == "passed" and out["error_code"] is None
    assert out["metrics"]["first_token_ms"] == pytest.approx(200)


def test_all_warm_runs_failing_is_failed():
    out = _run(_Clock(), [{}, ConnectionError("x"), ConnectionError("x")], runs=2)
    assert out["status"] == "failed" and out["error_code"] == "ollama_unreachable"


def test_busy_engine_is_refused_without_calling_the_model():
    calls = []
    out = _run(_Clock(), [], host=_Host(generating=True), calls=calls)
    assert out["status"] == "failed" and out["error_code"] == "busy" and calls == []


def test_overall_budget_stops_further_runs(monkeypatch):
    monkeypatch.setattr(setup_inference, "LOCAL_BUDGET_S", 5.0)
    out = _run(_Clock(), [{"first_ms": 6000}, {}], runs=1)
    assert out["status"] == "failed" and out["error_code"] == "timeout"


# -- cloud ------------------------------------------------------------------

_CLOUD_CFG = {
    "active_provider": "nim",
    "profiles": {"nim": {"base_url": "https://api.example/v1", "model": "meta/llama"}},
}


def _cloud_fake(clock, scripts, calls=None):
    scripts = list(scripts)

    def stream(base_url, api_key, model, messages, options, timeout):
        if calls is not None:
            calls.append(
                {"base_url": base_url, "api_key": api_key, "model": model, "options": options}
            )
        script = scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        clock.t += script.get("first_ms", 200) / 1000.0
        yield {"message": {"content": "Hi", "thinking": ""}, "done": False}
        clock.t += script.get("gen_ms", 1000) / 1000.0
        yield {"done": True, "usage": {"completion_tokens": script.get("tokens", 20)}}

    return stream


def _cloud_run(clock, scripts, runs=1, key="sk-secret", calls=None):
    host = _Host(cfg=_CLOUD_CFG)
    return setup_inference.run_inference_test(
        host,
        runs,
        cloud_stream=_cloud_fake(clock, scripts, calls),
        api_key_loader=lambda pid: key,
        clock=clock,
    )


def test_cloud_passed_uses_active_profile_and_has_null_load():
    calls = []
    out = _cloud_run(_Clock(), [{}, {"first_ms": 400, "gen_ms": 1000, "tokens": 20}], calls=calls)
    assert out["provider"] == "cloud" and out["model"] == "meta/llama"
    assert out["status"] == "passed"
    assert out["metrics"]["load_ms"] is None
    assert out["metrics"]["first_token_ms"] == pytest.approx(400)
    assert out["metrics"]["tokens_per_second"] == pytest.approx(20)
    assert calls[0]["base_url"] == "https://api.example/v1"
    assert calls[0]["options"]["num_predict"] <= 64
    assert "sk-secret" not in repr(out)


def test_cloud_without_usage_has_null_tps():
    clock = _Clock()

    def stream(base_url, api_key, model, messages, options, timeout):
        clock.t += 0.1
        yield {"message": {"content": "Hi", "thinking": ""}, "done": False}
        yield {"done": True, "usage": {}}

    out = setup_inference.run_inference_test(
        _Host(cfg=_CLOUD_CFG), 1, cloud_stream=stream, api_key_loader=lambda p: "k", clock=clock
    )
    assert out["metrics"]["tokens_per_second"] is None


def test_cloud_missing_key_is_cloud_auth_without_calling():
    calls = []
    out = _cloud_run(_Clock(), [], key="", calls=calls)
    assert out["status"] == "failed" and out["error_code"] == "cloud_auth" and calls == []


def test_cloud_401_is_cloud_auth_and_other_http_is_cloud_error():
    auth = CloudLLMResponseError("cloud chat HTTP 401: Unauthorized", status_code=401)
    assert _cloud_run(_Clock(), [auth])["error_code"] == "cloud_auth"
    server = CloudLLMResponseError("cloud chat HTTP 500: boom", status_code=500)
    assert _cloud_run(_Clock(), [server])["error_code"] == "cloud_error"


def test_cloud_timeout_and_connection_errors():
    assert _cloud_run(_Clock(), [requests.exceptions.Timeout()])["error_code"] == "timeout"
    assert (
        _cloud_run(_Clock(), [requests.exceptions.ConnectionError()])["error_code"]
        == "cloud_error"
    )


@pytest.mark.parametrize(
    "status,code",
    [(401, "cloud_auth"), (403, "cloud_auth"), (429, "cloud_rate_limit"), (402, "cloud_rate_limit"),
     (400, "cloud_bad_request"), (404, "cloud_bad_request"), (422, "cloud_bad_request"),
     (500, "cloud_error"), (503, "cloud_error")],
)
def test_cloud_http_status_classification(status, code):
    exc = CloudLLMResponseError(f"cloud chat HTTP {status}", status_code=status)
    out = _cloud_run(_Clock(), [exc])
    assert out["error_code"] == code and out["http_status"] == status


def test_cloud_failure_returns_sanitized_bounded_provider_message():
    body = '{"error": {"code": 400, "message": "Invalid key sk-secret in request", "status": "INVALID_ARGUMENT"}}'
    exc = CloudLLMResponseError("cloud chat HTTP 400", status_code=400, body_excerpt=body)
    out = _cloud_run(_Clock(), [exc])
    assert out["http_status"] == 400
    assert "Invalid key" in out["provider_message"]
    assert "sk-secret" not in repr(out) and len(out["provider_message"]) <= 200


def test_non_http_failures_have_null_status_and_message():
    out = _cloud_run(_Clock(), [requests.exceptions.Timeout()])
    assert out["http_status"] is None and out["provider_message"] is None
    ok = _cloud_run(_Clock(), [{}, {}])
    assert ok["http_status"] is None and ok["provider_message"] is None


def test_cloud_failure_log_line_has_status_and_message_but_no_key(caplog):
    exc = CloudLLMResponseError(
        "cloud chat HTTP 429", status_code=429, body_excerpt='{"error": {"message": "quota hit sk-secret"}}'
    )
    with caplog.at_level(logging.INFO):
        _cloud_run(_Clock(), [exc])
    line = [r.getMessage() for r in caplog.records if "[SETUP] inference_test" in r.getMessage()][0]
    assert "http_status=429" in line and "quota hit" in line and "sk-secret" not in line


def test_cloud_stream_receives_the_active_preset_shaping():
    seen = {}

    def stream(base_url, api_key, model, messages, options, timeout, **kw):
        seen.update(kw)
        raise CloudLLMResponseError("x", status_code=400)
        yield  # pragma: no cover

    cfg = {
        "active_provider": "g",
        "profiles": {"g": {"base_url": "https://g/v1", "model": "m", "preset": "nvidia_nim"}},
    }
    setup_inference.run_inference_test(
        _Host(cfg=cfg), 1, cloud_stream=stream, api_key_loader=lambda p: "k", clock=_Clock()
    )
    assert seen["reasoning_style"] == "nim_kwargs"
    assert seen["drop_params"] == [] and seen["extra_headers"] == {}


def test_real_cloud_stream_forwards_preset_kwargs(monkeypatch):
    from opencohost.core.providers.cloud import cloud_llm_client

    got = {}
    monkeypatch.setattr(cloud_llm_client, "stream_chat_completion", lambda *a, **k: got.update(k) or iter(()))
    setup_inference._cloud_stream(
        "u", "k", "m", [], {}, 1, reasoning_style="none", drop_params=["p"], extra_headers={"h": "v"}
    )
    assert got == {"reasoning_style": "none", "drop_params": ["p"], "extra_headers": {"h": "v"}}


# -- endpoint ---------------------------------------------------------------


@pytest.fixture
def _client(monkeypatch):
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    clock = _Clock()
    monkeypatch.setattr(setup_inference, "_default_clock", clock)
    monkeypatch.setattr(
        setup_inference,
        "_local_stream",
        _local_fake(clock, [{}, {"first_ms": 120}, {"first_ms": 140}]),
    )
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)
    with TestClient(app) as client:
        yield client
    main_mod._host_active = False


def test_endpoint_shape_and_single_log_line(_client, caplog):
    with caplog.at_level(logging.INFO):
        resp = _client.post("/api/setup/inference-test", json={"runs": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "status", "provider", "model", "metrics", "runs", "error_code", "cold_load_ms",
        "http_status", "provider_message",
    }
    assert body["status"] == "passed" and body["provider"] == "local"
    assert len(body["runs"]) == 2
    assert set(body["metrics"]) == {"load_ms", "first_token_ms", "tokens_per_second"}
    lines = [r.getMessage() for r in caplog.records if "[SETUP] inference_test" in r.getMessage()]
    assert len(lines) == 1 and "status=passed" in lines[0] and "provider=local" in lines[0]
    # Only the "OpenCohost" logger reaches the session log file (live 2026-10-08: the line was lost).
    names = [r.name for r in caplog.records if "[SETUP] inference_test" in r.getMessage()]
    assert names == ["OpenCohost"]


def test_endpoint_default_runs_is_three(monkeypatch):
    seen = {}

    def fake(host, runs, **kw):
        seen["runs"] = runs
        return setup_inference.build_result("passed", "local", "m", [], None, None)

    monkeypatch.setattr(setup_inference, "run_inference_test", fake)
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)
    with TestClient(app) as client:
        assert client.post("/api/setup/inference-test", json={}).status_code == 200
    main_mod._host_active = False
    assert seen["runs"] == 3


@pytest.mark.parametrize("runs", [0, 6, "x"])
def test_endpoint_rejects_bad_runs(_client, runs):
    assert _client.post("/api/setup/inference-test", json={"runs": runs}).status_code == 422


def test_endpoint_does_not_touch_engine_config(_client):
    motor = _client.app.state.host.motor
    before_model = motor.current_model
    before_cfg = dict(motor._provider_config)
    _client.post("/api/setup/inference-test", json={"runs": 1})
    assert motor.current_model == before_model and motor._provider_config == before_cfg


def test_endpoint_busy_when_engine_generating(_client):
    _client.app.state.host.motor._llm_generating = True
    body = _client.post("/api/setup/inference-test", json={"runs": 1}).json()
    assert body["status"] == "failed" and body["error_code"] == "busy"
