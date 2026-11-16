"""compact-prompt-profile-20261009 T2: engine plumbing, [PROMPT_PROFILE] log, trace.

Real MotorVocalIA with a fake assembler that records the kwargs it receives.
The parameter size comes from the cached ``ollama.show`` response that
``_discover_model_ctx`` already owns (no new RPC on the turn path).
"""
from __future__ import annotations

import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.config import settings
from opencohost.core import llm_engine
from opencohost.core.engine import prompt_trace

SECRET = "zanahoriaconfidencial dime algo"


class FakeAssembler:
    def __init__(self):
        self.calls = []

    def assemble(self, contexto, source, **kwargs):
        self.calls.append({"contexto": contexto, "source": source, **kwargs})
        return MagicMock()


def _motor(monkeypatch, show=None, raises=False):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.ollama = MagicMock()
    m._current_profile_id = None
    m._prompt_assembler = FakeAssembler()
    m.discover_calls = []

    def _discover(model):
        m.discover_calls.append(model)
        if raises:
            raise RuntimeError("ollama down")
        if show is not None:
            m._ctx_show_cache = {model: show}
        return 4096

    monkeypatch.setattr(m, "_discover_model_ctx", _discover)
    monkeypatch.setattr(settings, "PROMPT_PROFILE", "auto")
    return m


def _build(m, model, source="direct", is_local=True):
    m._build_generation_request(
        SECRET,
        source,
        is_local=is_local,
        provider_cfg={},
        request_model=model,
        watchdog_timeout=None,
    )
    return m._prompt_assembler.calls[-1]


def _profile_logs(caplog):
    return [r.getMessage() for r in caplog.records if "[PROMPT_PROFILE]" in r.getMessage()]


SMALL = {"details": {"parameter_size": "0.8B"}}
LARGE = {"details": {"parameter_size": "8.0B"}}


@pytest.mark.parametrize("source", ["direct", "ptt", "chat", settings.OWNER_BUNDLE_SOURCE])
def test_small_model_gets_compact_on_every_source(monkeypatch, caplog, source):
    m = _motor(monkeypatch, show=SMALL)
    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert _build(m, "tiny", source)["prompt_profile"] == "compact"
    assert _profile_logs(caplog) == [
        "[PROMPT_PROFILE] profile=compact model_b=0.8 reason=auto_small layout=legacy"
    ]


def test_large_model_gets_full(monkeypatch, caplog):
    m = _motor(monkeypatch, show=LARGE)
    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert _build(m, "big")["prompt_profile"] == "full"
    assert _profile_logs(caplog) == ["[PROMPT_PROFILE] profile=full model_b=8 reason=auto_large layout=legacy"]


def test_name_fallback_without_details(monkeypatch):
    m = _motor(monkeypatch, show={})
    assert _build(m, "qwen3:1.7b")["prompt_profile"] == "compact"


def test_detection_failure_is_full_and_never_raises(monkeypatch, caplog):
    m = _motor(monkeypatch, raises=True)
    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert _build(m, "llama3")["prompt_profile"] == "full"
    assert _profile_logs(caplog) == [
        "[PROMPT_PROFILE] profile=full model_b=unknown reason=unknown_size layout=legacy"
    ]


def test_cloud_is_full_and_skips_lookup(monkeypatch, caplog):
    m = _motor(monkeypatch, show=SMALL)
    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert _build(m, "tiny:0.5b", is_local=False)["prompt_profile"] == "full"
    assert m.discover_calls == []
    assert _profile_logs(caplog) == ["[PROMPT_PROFILE] profile=full model_b=unknown reason=cloud layout=legacy"]


@pytest.mark.parametrize("override", ["full", "compact"])
def test_env_override(monkeypatch, caplog, override):
    m = _motor(monkeypatch, show=SMALL if override == "full" else LARGE)
    monkeypatch.setattr(settings, "PROMPT_PROFILE", override)
    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert _build(m, "x")["prompt_profile"] == override
    assert _profile_logs(caplog)[0].endswith("reason=env_override layout=legacy")


def test_size_cached_per_model(monkeypatch):
    m = _motor(monkeypatch, show=SMALL)
    _build(m, "tiny")
    _build(m, "tiny")
    assert m.discover_calls == ["tiny"]
    _build(m, "other")
    assert m.discover_calls == ["tiny", "other"]


def test_log_has_no_user_text(monkeypatch, caplog):
    m = _motor(monkeypatch, show=SMALL)
    caplog.set_level(logging.DEBUG)
    _build(m, "tiny")
    for line in _profile_logs(caplog):
        assert "zanahoria" not in line and "tiny" not in line


# --- trace ----------------------------------------------------------------------


def test_trace_line_records_profile(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    call = prompt_trace.begin(
        source="direct", model="m", is_local=True, messages=[], options={},
        profile="compact",
    )
    call.finish({"message": {"content": "ok"}})
    line = next(r.getMessage() for r in caplog.records if "[PROMPT_TRACE]" in r.getMessage())
    assert line.endswith(" profile=compact")


def test_trace_line_without_profile_unchanged(caplog):
    caplog.set_level(logging.INFO, logger="OpenCohost")
    call = prompt_trace.begin(source="direct", model="m", is_local=True, messages=[], options={})
    call.finish({"message": {"content": "ok"}})
    line = next(r.getMessage() for r in caplog.records if "[PROMPT_TRACE]" in r.getMessage())
    assert "profile=" not in line


def test_guardrail_retry_reuses_turn_profile(monkeypatch):
    """The retry resends the turn's own (compact) messages and options and
    records the same profile in its trace."""
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    seen = {}

    def _begin(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(llm_engine.prompt_trace, "begin", _begin)
    sent = {}

    def _chat(**kwargs):
        sent.update(kwargs)
        return {"message": {"content": "otra"}}

    monkeypatch.setattr(m, "_ollama_chat_with_watchdog", _chat)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out = m._retry_after_guard_block(
        messages=msgs,
        opciones_llm={"num_predict": 160},
        request_model="tiny",
        chat_timeout=5.0,
        provider_cfg={},
        is_local=True,
        guard_reason="x",
        prompt_profile="compact",
    )
    assert out == "otra"
    assert seen["source"] == "guardrail_retry"
    assert seen["profile"] == "compact"
    assert sent["options"]["num_predict"] == 160
    assert sent["messages"][0] == msgs[0]
