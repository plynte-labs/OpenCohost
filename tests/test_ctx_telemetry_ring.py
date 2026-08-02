"""Unit 2.3 (runtime_findings_batch_20260731 F10): per-request context
telemetry ring + the numeric-only ctx_pressure_high detail payload.

Backend-engine-only surface:
- opencohost/core/llm_engine.py: `_ctx_telemetry_ring` (bounded deque),
  `ctx_telemetry_snapshot()` (read-only accessor), `on_ctx_pressure_high`
  (optional payload callback).
- opencohost/api/engine_host.py: `_MOTOR_EVENT_DETAIL_FIELDS` whitelist +
  `_on_ctx_pressure_high` (records the numeric-only detail).

Fixture style: real `MotorVocalIA(queue.Queue(), ui_callback)` construction
(mirrors tests/test_llm_engine_model_trace.py::_make_motor) with LAST_MODEL_FILE
isolated to tmp_path, `ollama`/`pygame` mocked, `_ollama_chat_with_watchdog`
monkeypatched to a canned response -- mirrors tests/test_context_overflow_guardrail.py's
`_run` helper for the ctx_utilization/ctx_pressure_high call site.

Log capture: ctx_utilization/ctx_pressure_high are `logger.info`/`logger.warning`
calls (stdlib logging), NOT `self._log()` (the UI log_queue) -- caplog is the
correct capture mechanism here, same as test_context_overflow_guardrail.py's
own TestUtilizationObservability tests for this exact log line.
"""

import os
import queue
import sys
import threading
from unittest.mock import MagicMock

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from opencohost.core.llm_engine import MotorVocalIA
from opencohost.config.settings import CTX_TELEMETRY_RING_MAXLEN
import opencohost.api.engine_host as engine_host_mod


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_motor(tmp_path):
    """Real MotorVocalIA construction, LAST_MODEL_FILE isolated (mirrors
    test_llm_engine_model_trace.py::_make_motor)."""
    import opencohost.config.settings as settings

    original_last_model_file = settings.LAST_MODEL_FILE
    settings.LAST_MODEL_FILE = os.path.join(str(tmp_path), "last_model.json")
    try:
        motor = MotorVocalIA(queue.Queue(), lambda *a, **k: None)
    finally:
        settings.LAST_MODEL_FILE = original_last_model_file

    motor.ollama = MagicMock()  # never called: _ollama_chat_with_watchdog is monkeypatched per-test
    motor.pygame = MagicMock()
    motor._model_ctx_limit["llama3"] = 4096  # skip real ollama.show discovery
    return motor


class _Resp:
    """Stand-in for the Ollama ChatResponse (mirrors test_context_overflow_guardrail.py::_Resp)."""

    def __init__(self, content="ok", prompt_eval_count=0, prompt_eval_duration=0,
                 eval_duration=0, eval_count=0):
        self._message = {"content": content, "thinking": ""}
        self.prompt_eval_count = prompt_eval_count
        self.prompt_eval_duration = prompt_eval_duration
        self.eval_duration = eval_duration
        self.eval_count = eval_count

    def get(self, key, default=None):
        return self._message if key == "message" else default


@pytest.fixture(autouse=True)
def _allow_all_output(monkeypatch):
    import opencohost.core.llm_engine as le
    monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))


# ---------------------------------------------------------------------------
# Ring + accessor
# ---------------------------------------------------------------------------

class TestCtxTelemetryRing:
    def test_ring_entry_matches_ctx_utilization_log_line(self, tmp_path, caplog):
        m = _make_motor(tmp_path)
        m._ollama_chat_with_watchdog = lambda *, timeout, **kw: _Resp(
            content="hi", prompt_eval_count=3400, prompt_eval_duration=1_000_000_000,
            eval_duration=2_000_000_000, eval_count=128,
        )
        with caplog.at_level("INFO"):
            m._generar_dialogo("hola", source="chat", commit_history=False)

        log_line = next(r.message for r in caplog.records if "ctx_utilization" in r.message)
        latest = m.ctx_telemetry_snapshot()["latest"]
        assert latest is not None
        assert f"prompt_eval_count={latest['prompt_eval_count']}" in log_line
        assert f"native_ctx={latest['native_ctx']}" in log_line
        assert f"effective_ctx={latest['effective_ctx']}" in log_line
        assert f"ratio={latest['ratio']:.3f}" in log_line
        assert f"prefill_ms={latest['prefill_ms']:.0f}" in log_line
        assert f"decode_ms={latest['decode_ms']:.0f}" in log_line
        assert f"eval_count={latest['eval_count']}" in log_line
        assert latest["source"] == "chat"
        assert latest["model"] == "llama3"
        assert latest["provider"] == "local"
        assert latest["evicted_pairs"] == 0
        assert isinstance(latest["request_id"], str) and latest["request_id"]
        assert isinstance(latest["timestamp"], float)

    def test_ring_bounded_at_maxlen(self, tmp_path):
        m = _make_motor(tmp_path)
        m._ollama_chat_with_watchdog = lambda *, timeout, **kw: _Resp(content="hi", prompt_eval_count=2000)
        for _ in range(CTX_TELEMETRY_RING_MAXLEN + 5):
            m._generar_dialogo("hola", source="chat", commit_history=False)
        assert len(m.ctx_telemetry_snapshot()["ring"]) == CTX_TELEMETRY_RING_MAXLEN

    def test_concurrent_generations_get_distinct_ring_entries_and_sources(self, tmp_path):
        m = _make_motor(tmp_path)
        m._ollama_chat_with_watchdog = lambda *, timeout, **kw: _Resp(content="ok", prompt_eval_count=2500)

        def run(src):
            m._generar_dialogo("hola", source=src, commit_history=False)

        t1 = threading.Thread(target=run, args=("chat",))
        t2 = threading.Thread(target=run, args=("kira-agenda",))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        ring = m.ctx_telemetry_snapshot()["ring"]
        assert len(ring) == 2
        assert {e["source"] for e in ring} == {"chat", "kira-agenda"}
        assert len({e["request_id"] for e in ring}) == 2

    def test_accessor_filter_excludes_pregen_sources(self, tmp_path):
        m = _make_motor(tmp_path)
        m._ollama_chat_with_watchdog = lambda *, timeout, **kw: _Resp(content="ok", prompt_eval_count=2500)
        m._generar_dialogo("hola", source="kira-agenda", commit_history=False)
        m._generar_dialogo("hola", source="direct", commit_history=False)

        filtered = m.ctx_telemetry_snapshot(sources=("direct", "chat"))
        assert filtered["latest"]["source"] == "direct"
        # `ring` stays the full unfiltered history regardless of the filter.
        assert len(filtered["ring"]) == 2

    def test_failure_path_appends_nothing(self, tmp_path):
        m = _make_motor(tmp_path)

        def boom(*, timeout, **kw):
            raise ConnectionError("simulated transport failure")
        m._ollama_chat_with_watchdog = boom

        result = m._generar_dialogo("hola", source="chat", commit_history=False)
        assert result == ""
        assert m.ctx_telemetry_snapshot()["ring"] == []
        assert m.ctx_telemetry_snapshot()["latest"] is None


class TestCtxPressureHighPayload:
    def test_fires_with_numeric_payload_above_threshold(self, tmp_path):
        m = _make_motor(tmp_path)
        m._ollama_chat_with_watchdog = lambda *, timeout, **kw: _Resp(content="hi", prompt_eval_count=3400)
        captured = []
        m.on_ctx_pressure_high = lambda payload: captured.append(payload)

        m._generar_dialogo("hola", source="chat", commit_history=False)

        assert len(captured) == 1
        payload = captured[0]
        assert set(payload.keys()) == {"ratio", "effective_ctx", "native_ctx", "evicted_pairs"}
        assert all(isinstance(v, (int, float)) for v in payload.values())
        assert payload["ratio"] >= 0.80

    def test_not_fired_below_threshold(self, tmp_path):
        m = _make_motor(tmp_path)
        m._ollama_chat_with_watchdog = lambda *, timeout, **kw: _Resp(content="hi", prompt_eval_count=2000)
        captured = []
        m.on_ctx_pressure_high = lambda payload: captured.append(payload)

        m._generar_dialogo("hola", source="chat", commit_history=False)

        assert captured == []

    def test_ui_callback_still_fires_status_only_unchanged(self, tmp_path):
        """CTK's ui_callback stays single-arg -- ctx_pressure_high must keep
        firing through it with NO second argument (existing contract, pinned
        independently by tests/test_context_overflow_guardrail.py)."""
        m = _make_motor(tmp_path)
        m._ollama_chat_with_watchdog = lambda *, timeout, **kw: _Resp(content="hi", prompt_eval_count=3400)
        seen = []
        m.ui_callback = lambda status: seen.append(status)  # single-arg, like CTK's _on_motor_event

        m._generar_dialogo("hola", source="chat", commit_history=False)

        assert "ctx_pressure_high" in seen

    def test_none_callback_is_a_safe_noop(self, tmp_path):
        """on_ctx_pressure_high defaults to None (CTK never wires it) -- must
        not raise."""
        m = _make_motor(tmp_path)
        m._ollama_chat_with_watchdog = lambda *, timeout, **kw: _Resp(content="hi", prompt_eval_count=3400)
        assert m.on_ctx_pressure_high is None
        m._generar_dialogo("hola", source="chat", commit_history=False)  # must not raise


# ---------------------------------------------------------------------------
# engine_host.py: detail schema + privacy gate
# ---------------------------------------------------------------------------

from types import SimpleNamespace


class TestEngineHostCtxPressureDetail:
    def _stub(self):
        return SimpleNamespace(event_log=engine_host_mod.EventLogSink())

    def test_records_exactly_the_whitelisted_numeric_keys(self):
        stub = self._stub()
        payload = {"ratio": 0.83, "effective_ctx": 4096, "native_ctx": 8192, "evicted_pairs": 2}

        engine_host_mod.EngineHost._on_ctx_pressure_high(stub, payload)

        events = stub.event_log.since(0)["events"]
        assert len(events) == 1
        assert events[0]["action"] == "ctx_pressure_high"
        assert events[0]["detail"] == payload

    def test_drops_non_numeric_value_keeps_others(self):
        stub = self._stub()
        payload = {"ratio": "high", "effective_ctx": 4096, "native_ctx": 8192, "evicted_pairs": 0}

        engine_host_mod.EngineHost._on_ctx_pressure_high(stub, payload)

        detail = stub.event_log.since(0)["events"][0]["detail"]
        assert "ratio" not in detail
        assert detail == {"effective_ctx": 4096, "native_ctx": 8192, "evicted_pairs": 0}

    def test_bool_values_are_not_numeric(self):
        stub = self._stub()
        payload = {"ratio": True, "effective_ctx": 4096, "native_ctx": 8192, "evicted_pairs": 0}

        engine_host_mod.EngineHost._on_ctx_pressure_high(stub, payload)

        detail = stub.event_log.since(0)["events"][0]["detail"]
        assert "ratio" not in detail

    def test_all_non_numeric_yields_none_detail(self):
        stub = self._stub()
        payload = {"ratio": "x", "effective_ctx": "y", "native_ctx": "z", "evicted_pairs": "w"}

        engine_host_mod.EngineHost._on_ctx_pressure_high(stub, payload)

        assert stub.event_log.since(0)["events"][0]["detail"] is None

    def test_unwhitelisted_keys_are_ignored(self):
        stub = self._stub()
        payload = {
            "ratio": 0.9, "effective_ctx": 4096, "native_ctx": 8192, "evicted_pairs": 1,
            "raw_dialogue": "this must never leak",
        }

        engine_host_mod.EngineHost._on_ctx_pressure_high(stub, payload)

        detail = stub.event_log.since(0)["events"][0]["detail"]
        assert "raw_dialogue" not in detail
        assert set(detail.keys()) == {"ratio", "effective_ctx", "native_ctx", "evicted_pairs"}

    def test_generic_record_motor_event_path_skips_ctx_pressure_high(self):
        """The generic whitelist pass-through must NOT also record a bare
        detail=None row for ctx_pressure_high -- it is recorded exactly once,
        by _on_ctx_pressure_high, with real detail."""
        stub = self._stub()

        engine_host_mod.EngineHost._record_motor_event(stub, "ctx_pressure_high")

        assert stub.event_log.since(0)["events"] == []

    def test_other_whitelisted_statuses_still_get_none_detail(self):
        stub = self._stub()

        engine_host_mod.EngineHost._record_motor_event(stub, "ready")

        events = stub.event_log.since(0)["events"]
        assert len(events) == 1
        assert events[0]["detail"] is None
