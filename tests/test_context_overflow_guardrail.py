"""Strict-TDD WIRING/integration tests for the context-overflow guardrail.

Track: context_overflow_guardrail_20260623 (design.md §4.5-§4.8).

These tests cover the THIN orchestration in llm_engine.py and app_shell/
motor_event_handlers (the pure budget logic is unit-tested in
tests/test_context_budget.py). All Ollama I/O is mocked; no real model or
network call is made.

PATCH CAVEAT (design §4.5): the reactive call-site does
``context_budget.trim_messages_reactive(...)`` and
``context_budget.is_overflow_signal(...)`` where ``context_budget`` is the
module imported into llm_engine. Patching
``opencohost.core.llm_engine.context_budget.<fn>`` patches the attribute on
the shared module object, which the call-site reads at call time — so the mock
IS observed. Patching an instance attribute would pass vacuously.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opencohost.core.llm_engine import MotorVocalIA


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirror tests/test_reasoning_token_budget.py::_make_motor)
# ---------------------------------------------------------------------------

def _make_motor(model="llama3"):
    """A MotorVocalIA with __init__ bypassed and only the attributes the
    source='chat', commit_history=False path of _generar_dialogo touches."""
    m = MotorVocalIA.__new__(MotorVocalIA)
    m._reasoning_model_cache = {}
    m._model_ctx_limit = {}
    m.current_model = model
    m._desired_model = model
    m._loaded_model = model
    m._current_profile_name = "default"
    m.use_system_role = True
    m.system_prompt = "sys"
    m._history_lock = threading.RLock()
    m.historial = []
    m._last_llm_failure = None
    m._log = lambda *a, **k: None
    m._mark_model_generation_success = lambda *a, **k: None
    m._resolve_chat_watchdog_timeout = lambda model: 30.0
    m.ui_callback = lambda *a, **k: None
    return m


class _Resp:
    """A stand-in for the Ollama ChatResponse: supports BOTH ``.get('message')``
    (used at the msg_obj extraction site) AND ``getattr(resp, 'prompt_eval_count')``
    (used by the overflow + utilization branches)."""

    def __init__(self, content="", thinking="", prompt_eval_count=0):
        self._message = {"content": content, "thinking": thinking}
        self.prompt_eval_count = prompt_eval_count

    def get(self, key, default=None):
        if key == "message":
            return self._message
        return default


def _show(**model_info):
    return {"model_info": model_info}


# ===========================================================================
# §4.2 — _discover_model_ctx (Layer 1) + RPC ownership
# ===========================================================================

class TestDiscoverModelCtx:
    def test_reads_llama_context_length(self, monkeypatch):
        monkeypatch.setattr("ollama.show", lambda model: _show(**{"llama.context_length": 32768}))
        m = _make_motor()
        assert m._discover_model_ctx("qwen3:4b") == 32768
        assert m._model_ctx_limit["qwen3:4b"] == 32768

    def test_fallback_on_exception(self, monkeypatch):
        def _raise(model):
            raise RuntimeError("network error")
        monkeypatch.setattr("ollama.show", _raise)
        m = _make_motor()
        from opencohost.config.settings import CTX_FALLBACK_DEFAULT
        assert m._discover_model_ctx("qwen3:4b") == CTX_FALLBACK_DEFAULT

    def test_result_is_cached_single_rpc(self, monkeypatch):
        calls = {"n": 0}

        def fake_show(model):
            calls["n"] += 1
            return _show(**{"llama.context_length": 4096})
        monkeypatch.setattr("ollama.show", fake_show)
        m = _make_motor()
        m._discover_model_ctx("llama3")
        m._discover_model_ctx("llama3")
        assert calls["n"] == 1

    def test_capabilities_and_ctx_share_one_rpc(self, monkeypatch):
        """RPC-ownership: capabilities check + ctx discovery cost ONE ollama.show."""
        calls = {"n": 0}

        def fake_show(model):
            calls["n"] += 1
            return {"capabilities": ["thinking"], "model_info": {"llama.context_length": 8192}}
        monkeypatch.setattr("ollama.show", fake_show)
        m = _make_motor()
        assert m._check_capabilities_reasoning("gemma4:12b") is True
        assert m._model_ctx_limit["gemma4:12b"] == 8192   # populated as a side effect
        assert calls["n"] == 1


# ===========================================================================
# §4.8 regression — reasoning cache unaffected by the refactor
# ===========================================================================

class TestReasoningCacheRegression:
    def test_reasoning_model_cache_unaffected(self, monkeypatch):
        monkeypatch.setattr("ollama.show", lambda model: {"capabilities": ["thinking"]})
        m = _make_motor()
        assert m._resolve_reasoning_classification("gemma4:12b") is True
        assert m._reasoning_model_cache["gemma4:12b"] is True

    def test_name_heuristic_skips_rpc(self, monkeypatch):
        calls = {"n": 0}

        def fake_show(model):
            calls["n"] += 1
            return {"capabilities": ["thinking"]}
        monkeypatch.setattr("ollama.show", fake_show)
        m = _make_motor()
        assert m._resolve_reasoning_classification("qwen3:4b") is True
        assert calls["n"] == 0   # name heuristic short-circuits the RPC


# ===========================================================================
# §4.5 — retry loop overflow branch integration
# ===========================================================================

class TestOverflowBranch:
    def _run(self, monkeypatch, chat_responses, *, model="llama3", ctx=4096):
        import opencohost.core.llm_engine as le
        monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
        m = _make_motor(model)
        m._model_ctx_limit[model] = ctx
        # Pre-seed so _discover_model_ctx never calls ollama.show.
        seq = list(chat_responses)

        def fake_chat(*, timeout, **kwargs):
            return seq.pop(0)
        m._ollama_chat_with_watchdog = fake_chat
        return m

    def test_fires_on_empty_plus_pec_signal(self, monkeypatch):
        m = self._run(monkeypatch, [
            _Resp(content="", prompt_eval_count=3900),     # ratio ~0.95 of 4096
            _Resp(content="real answer", prompt_eval_count=100),
        ])
        with patch("opencohost.core.llm_engine.context_budget.trim_messages_reactive") as mock_trim:
            mock_trim.return_value = 1
            result = m._generar_dialogo("hola", source="chat", commit_history=False)
        assert mock_trim.call_count == 1
        assert result == "real answer"

    def test_does_not_fire_when_pec_is_zero(self, monkeypatch):
        m = self._run(monkeypatch, [
            _Resp(content="", prompt_eval_count=0),
            _Resp(content="answer", prompt_eval_count=10),
        ])
        with patch("opencohost.core.llm_engine.context_budget.trim_messages_reactive") as mock_trim:
            m._generar_dialogo("hola", source="chat", commit_history=False)
        assert mock_trim.call_count == 0

    def test_does_not_fire_on_success(self, monkeypatch):
        m = self._run(monkeypatch, [
            _Resp(content="Hello", prompt_eval_count=3000),
        ])
        with patch("opencohost.core.llm_engine.context_budget.trim_messages_reactive") as mock_trim:
            m._generar_dialogo("hola", source="chat", commit_history=False)
        assert mock_trim.call_count == 0

    def test_fires_only_on_first_attempt(self, monkeypatch):
        m = self._run(monkeypatch, [
            _Resp(content="", prompt_eval_count=3900),
            _Resp(content="", prompt_eval_count=3900),
        ])
        with patch("opencohost.core.llm_engine.context_budget.trim_messages_reactive") as mock_trim:
            mock_trim.return_value = 1
            m._generar_dialogo("hola", source="chat", commit_history=False)
        assert mock_trim.call_count == 1   # intento==0 guard prevents a second trim

    def test_does_not_mutate_self_historial(self, monkeypatch):
        m = self._run(monkeypatch, [
            _Resp(content="", prompt_eval_count=3900),
            _Resp(content="answer", prompt_eval_count=10),
        ])
        m.historial = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "older"}]
        before = list(m.historial)
        m._generar_dialogo("hola", source="chat", commit_history=False)
        assert m.historial == before

    def test_wins_over_reasoning_branch_on_dual_signal(self, monkeypatch):
        """Overflow branch fires BEFORE the reasoning (empty+thinking) branch."""
        m = self._run(monkeypatch, [
            _Resp(content="", thinking="some thinking", prompt_eval_count=3900),
            _Resp(content="answer", prompt_eval_count=10),
        ])
        captured = []
        real_chat = m._ollama_chat_with_watchdog

        def spy_chat(*, timeout, **kwargs):
            captured.append(dict(kwargs.get("options", {})))
            return real_chat(timeout=timeout, **kwargs)
        m._ollama_chat_with_watchdog = spy_chat
        with patch("opencohost.core.llm_engine.context_budget.trim_messages_reactive") as mock_trim:
            mock_trim.return_value = 1
            m._generar_dialogo("hola", source="chat", commit_history=False)
        assert mock_trim.call_count == 1
        # The reasoning branch (which pops num_predict) did NOT fire first:
        # num_predict is still present on the second attempt's options.
        assert "num_predict" in captured[1]

    def test_fires_at_exact_boundary(self, monkeypatch):
        # int(4096 * 0.95) == 3891 — the >= int-threshold boundary must fire.
        m = self._run(monkeypatch, [
            _Resp(content="", prompt_eval_count=int(4096 * 0.95)),
            _Resp(content="answer", prompt_eval_count=10),
        ])
        with patch("opencohost.core.llm_engine.context_budget.trim_messages_reactive") as mock_trim:
            mock_trim.return_value = 1
            m._generar_dialogo("hola", source="chat", commit_history=False)
        assert mock_trim.call_count == 1

    def test_does_not_fire_one_below_boundary(self, monkeypatch):
        m = self._run(monkeypatch, [
            _Resp(content="", prompt_eval_count=int(4096 * 0.95) - 1),
            _Resp(content="answer", prompt_eval_count=10),
        ])
        with patch("opencohost.core.llm_engine.context_budget.trim_messages_reactive") as mock_trim:
            m._generar_dialogo("hola", source="chat", commit_history=False)
        assert mock_trim.call_count == 0


# ===========================================================================
# §4.6 — utilization observability (Layer 4)
# ===========================================================================

class TestUtilizationObservability:
    def _run(self, monkeypatch, resp, *, model="llama3", ctx=4096, ui=None):
        import opencohost.core.llm_engine as le
        monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
        m = _make_motor(model)
        m._model_ctx_limit[model] = ctx
        if ui is not None:
            m.ui_callback = ui
        m._ollama_chat_with_watchdog = lambda *, timeout, **kwargs: resp
        return m

    def test_utilization_logged_after_successful_response(self, monkeypatch, caplog):
        m = self._run(monkeypatch, _Resp(content="hi", prompt_eval_count=2000))
        with caplog.at_level("INFO"):
            m._generar_dialogo("hola", source="chat", commit_history=False)
        assert any("ctx_utilization" in r.message for r in caplog.records)

    def test_pressure_high_callback_above_threshold(self, monkeypatch):
        seen = []
        m = self._run(monkeypatch, _Resp(content="hi", prompt_eval_count=3400), ui=lambda s: seen.append(s))
        m._generar_dialogo("hola", source="chat", commit_history=False)
        assert "ctx_pressure_high" in seen

    def test_pressure_high_callback_not_emitted_below_threshold(self, monkeypatch):
        seen = []
        m = self._run(monkeypatch, _Resp(content="hi", prompt_eval_count=2000), ui=lambda s: seen.append(s))
        m._generar_dialogo("hola", source="chat", commit_history=False)
        assert "ctx_pressure_high" not in seen

    def test_utilization_skipped_when_pec_is_zero(self, monkeypatch, caplog):
        seen = []
        m = self._run(monkeypatch, _Resp(content="hi", prompt_eval_count=0), ui=lambda s: seen.append(s))
        with caplog.at_level("INFO"):
            m._generar_dialogo("hola", source="chat", commit_history=False)
        assert not any("ctx_utilization" in r.message for r in caplog.records)
        assert "ctx_pressure_high" not in seen


# ===========================================================================
# §4.7 — ctx_pressure_high handler wiring
# ===========================================================================

class TestCtxPressureHighHandler:
    def test_registered_via_status_to_handler(self):
        from opencohost.ui.motor_event_handlers import STATUS_TO_HANDLER
        assert STATUS_TO_HANDLER.get("ctx_pressure_high") == "on_ctx_pressure_high"

    def test_module_function_exists_and_logs_warning(self, caplog):
        from opencohost.ui import motor_event_handlers as meh
        with caplog.at_level("WARNING"):
            meh.on_ctx_pressure_high()
        assert any("ctx_pressure_high" in r.message for r in caplog.records)

    def test_shell_delegate_exists(self):
        from opencohost.ui import app_shell
        assert callable(getattr(app_shell.VocalAIApp, "_on_ctx_pressure_high", None))

    def test_dispatch_via_handle_motor_event(self):
        """_handle_motor_event('ctx_pressure_high') routes to the delegate exactly once."""
        from opencohost.ui import app_shell
        app = object.__new__(app_shell.VocalAIApp)
        calls = {"n": 0}
        app._on_ctx_pressure_high = lambda: calls.__setitem__("n", calls["n"] + 1)
        app._handle_motor_event("ctx_pressure_high")
        assert calls["n"] == 1


# ===========================================================================
# §4.8 — regression guards
# ===========================================================================

class TestRegressionGuards:
    def _run_dialogo(self, monkeypatch, *, model, ctx_seed=None, resp=None):
        import opencohost.core.llm_engine as le
        monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
        m = _make_motor(model)
        if ctx_seed is not None:
            m._model_ctx_limit[model] = ctx_seed
        captured = []

        def fake_chat(*, timeout, **kwargs):
            captured.append(dict(kwargs.get("options", {})))
            return resp if resp is not None else _Resp(content="ok", prompt_eval_count=10)
        m._ollama_chat_with_watchdog = fake_chat
        m._generar_dialogo("hola", source="chat", commit_history=False)
        return captured

    def test_num_ctx_in_opciones_uses_discovered_value(self, monkeypatch):
        captured = self._run_dialogo(monkeypatch, model="qwen3:4b", ctx_seed=32768)
        assert captured[0]["num_ctx"] == 32768

    def test_gemma_still_pops_num_ctx(self, monkeypatch):
        captured = self._run_dialogo(monkeypatch, model="gemma4:e4b", ctx_seed=8192)
        assert "num_ctx" not in captured[0]

    def test_chat_uses_finite_keep_alive(self, monkeypatch):
        """The chat generation call must pass the finite LLM_KEEP_ALIVE, not the
        legacy -1 that pins the model in RAM for the whole session
        (ram_llm_hardening_20260626 Phase 0 / A1)."""
        import opencohost.core.llm_engine as le
        monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
        m = _make_motor("qwen3:4b")
        captured = {}

        def fake_chat(*, timeout, **kwargs):
            captured.update(kwargs)
            return _Resp(content="ok", prompt_eval_count=10)

        m._ollama_chat_with_watchdog = fake_chat
        m._generar_dialogo("hola", source="chat", commit_history=False)
        assert captured["keep_alive"] == le.LLM_KEEP_ALIVE

    def test_existing_retry_reasoning_branch_still_works(self, monkeypatch):
        """Empty + thinking + num_predict present → pop num_predict + retry (Layer 2 self-heal).
        This must still fire when there is NO overflow signal (pec below threshold)."""
        import opencohost.core.llm_engine as le
        monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
        monkeypatch.setattr("ollama.show", lambda model: {"capabilities": []})
        m = _make_motor("gemma4:12b")
        m._model_ctx_limit["gemma4:12b"] = 8192
        captured = []
        seq = [
            _Resp(content="", thinking="reasoning", prompt_eval_count=100),  # below threshold
            _Resp(content="the answer", prompt_eval_count=120),
        ]

        def fake_chat(*, timeout, **kwargs):
            captured.append(dict(kwargs.get("options", {})))
            return seq.pop(0)
        m._ollama_chat_with_watchdog = fake_chat
        result = m._generar_dialogo("hola", source="chat", commit_history=False)
        assert result == "the answer"
        assert "num_predict" in captured[0]
        assert "num_predict" not in captured[1]   # self-heal removed the cap

    def test_discover_ctx_populated_for_name_heuristic_models(self, monkeypatch):
        """Layer-1 fix: _generar_dialogo discovers ctx even when the name heuristic
        short-circuits _check_capabilities_reasoning (qwen3 never hits ollama.show
        via the reasoning path, but the budget-gate call still populates the cache)."""
        import opencohost.core.llm_engine as le
        monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
        monkeypatch.setattr("ollama.show", lambda model: _show(**{"llama.context_length": 32768}))
        m = _make_motor("qwen3:4b")
        m._ollama_chat_with_watchdog = lambda *, timeout, **kwargs: _Resp(content="ok", prompt_eval_count=10)
        m._generar_dialogo("hola", source="chat", commit_history=False)
        assert m._model_ctx_limit["qwen3:4b"] == 32768

    def test_end_to_end_layer1_layer2_layer3(self, monkeypatch):
        """Full integration: discover ctx (L1) → char-budget trim (L2) → correct num_ctx.
        Build an oversized messages list so the budget gate evicts at least one pair."""
        import opencohost.core.llm_engine as le
        monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
        m = _make_motor("llama3")
        m._model_ctx_limit["llama3"] = 4096   # budget = (4096-768)*3.5 = 11620 chars
        # History far exceeding the budget so eviction must happen.
        big = "x" * 4000
        m.historial = [
            {"role": "user", "content": big},
            {"role": "assistant", "content": big},
            {"role": "user", "content": big},
            {"role": "assistant", "content": big},
        ]
        captured = {}

        def fake_chat(*, timeout, **kwargs):
            captured["messages"] = list(kwargs.get("messages", []))
            captured["options"] = dict(kwargs.get("options", {}))
            return _Resp(content="ok", prompt_eval_count=10)
        m._ollama_chat_with_watchdog = fake_chat
        m._generar_dialogo("hola", source="chat", commit_history=False)
        # (a) original list = system + 4 history + user = 6; eviction shrinks it.
        assert len(captured["messages"]) < 6
        # (c) num_ctx matches the discovered limit.
        assert captured["options"]["num_ctx"] == 4096


# ===========================================================================
# H1 — prompt_eval_count=None does not crash _generar_dialogo
# ===========================================================================

class TestPecNoneDoesNotCrash:
    """Regression tests for H1: Ollama KV-cache-served responses set
    prompt_eval_count=None (the field exists but its value is None).
    The engine must NOT raise TypeError and must return the real content."""

    def _make_none_pec_resp(self, content="hello world"):
        """_Resp with prompt_eval_count attribute set to None (not 0)."""
        r = _Resp(content=content, prompt_eval_count=0)
        # Overwrite the attribute to None to simulate the KV-cache case.
        r.prompt_eval_count = None
        return r

    def test_none_pec_returns_content_not_empty_string(self, monkeypatch):
        # H1: _generar_dialogo must return the actual content, NOT "".
        import opencohost.core.llm_engine as le
        monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
        m = _make_motor()
        m._model_ctx_limit["llama3"] = 4096
        resp = self._make_none_pec_resp("real content from kv cache")
        m._ollama_chat_with_watchdog = lambda *, timeout, **kwargs: resp
        result = m._generar_dialogo("hola", source="chat", commit_history=False)
        # Must not be empty and must not raise.
        assert result == "real content from kv cache"

    def test_none_pec_does_not_raise(self, monkeypatch):
        # H1: no TypeError must escape from _generar_dialogo when pec is None.
        import opencohost.core.llm_engine as le
        monkeypatch.setattr(le, "output_guard", lambda dialogo, source="chat": (True, ""))
        m = _make_motor()
        m._model_ctx_limit["llama3"] = 4096
        resp = self._make_none_pec_resp("valid reply")
        m._ollama_chat_with_watchdog = lambda *, timeout, **kwargs: resp
        # Must not raise any exception.
        try:
            m._generar_dialogo("hola", source="chat", commit_history=False)
        except TypeError as e:
            pytest.fail(f"TypeError raised with pec=None: {e}")
