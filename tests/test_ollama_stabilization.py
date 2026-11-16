"""S5 (release-050-stabilization): Ollama discovery, version and startup model.

* one shared executable finder (includes %ProgramFiles%\\Ollama);
* a content-free INFO line when the eager wake finds no executable;
* the real Ollama version from GET /api/version, and a warning below the minimum;
* the startup model is re-resolved once Ollama is ready (the user's saved or an
  installed model wins over a DEFAULT_MODEL picked while Ollama was down);
* bounded ``ollama.list()`` calls.
Synthetic data only.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import time
from unittest.mock import MagicMock

import pytest

from opencohost.config import settings
from opencohost.core import ollama_locator
from opencohost.core.engine import llm_readiness

# --- shared finder ----------------------------------------------------------


def _fake_fs(monkeypatch, existing):
    monkeypatch.setattr(ollama_locator.shutil, "which", lambda name: None)
    monkeypatch.setattr(ollama_locator.os.path, "isfile", lambda p: os.path.normpath(p) in {os.path.normpath(e) for e in existing})


def test_finder_checks_program_files(monkeypatch):
    pf = os.path.join("C:\\", "Program Files")
    exe = os.path.join(pf, "Ollama", "ollama.exe")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("ProgramFiles", pf)
    _fake_fs(monkeypatch, [exe])
    assert ollama_locator.find_ollama_executable() == exe
    assert ollama_locator.locate_ollama_binary() == (exe, False)


def test_finder_prefers_path_then_local_appdata_then_program_files(monkeypatch):
    local = os.path.join("C:\\", "Users", "x", "AppData", "Local")
    pf = os.path.join("C:\\", "Program Files")
    a = os.path.join(local, "Programs", "Ollama", "ollama.exe")
    b = os.path.join(pf, "Ollama", "ollama.exe")
    monkeypatch.setenv("LOCALAPPDATA", local)
    monkeypatch.setenv("ProgramFiles", pf)
    _fake_fs(monkeypatch, [a, b])
    assert ollama_locator.find_ollama_executable() == a
    monkeypatch.setattr(ollama_locator.shutil, "which", lambda name: "C:/bin/ollama.exe")
    assert ollama_locator.locate_ollama_binary() == ("C:/bin/ollama.exe", True)


def test_finder_returns_none_when_nothing_is_installed(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    _fake_fs(monkeypatch, [])
    assert ollama_locator.find_ollama_executable() is None
    assert ollama_locator.locate_ollama_binary() == (None, False)


def test_readiness_detector_uses_the_shared_finder(monkeypatch):
    monkeypatch.setattr(ollama_locator, "locate_ollama_binary", lambda: ("X:/ollama.exe", False))
    assert llm_readiness._detect_ollama_binary() == ("X:/ollama.exe", False)


def test_engine_host_finder_is_the_shared_finder(monkeypatch):
    from opencohost.api import engine_host as engine_host_mod

    monkeypatch.setattr(ollama_locator, "find_ollama_executable", lambda: "Y:/ollama.exe")
    assert engine_host_mod._find_ollama_executable() == "Y:/ollama.exe"


# --- eager wake log ---------------------------------------------------------


def test_eager_wake_logs_a_content_free_line_when_no_executable(tmp_path, monkeypatch, caplog):
    from tests.test_ollama_autowake import _make_host

    host, engine_host_mod = _make_host(tmp_path, monkeypatch)
    monkeypatch.setattr(engine_host_mod, "_find_ollama_executable", lambda: None)
    with caplog.at_level(logging.INFO, logger=engine_host_mod._logger.name):
        host._wake_ollama_eager()
    lines = [r.getMessage() for r in caplog.records if "ollama" in r.getMessage().lower()]
    assert any("executable not found" in m.lower() for m in lines)
    assert all("\\" not in m and "/" not in m for m in lines)  # no paths


# --- version ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("0.30.11", (0, 30, 11)), ("v0.5.1", (0, 5, 1)), ("0.9.0-rc2", (0, 9, 0)), ("1.2", (1, 2)), ("", None), ("abc", None), (None, None)],
)
def test_parse_version(raw, expected):
    assert llm_readiness.parse_ollama_version(raw) == expected


def test_version_assessment_flags_old_builds_only():
    minimum = ".".join(str(n) for n in llm_readiness.MIN_OLLAMA_VERSION)
    assert llm_readiness.ollama_version_supported("0.30.11") is True
    assert llm_readiness.ollama_version_supported(minimum) is True
    assert llm_readiness.ollama_version_supported("0.1.0") is False
    assert llm_readiness.ollama_version_supported(None) is None
    assert llm_readiness.ollama_version_supported("weird") is None


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_version_reads_api_version(monkeypatch):
    seen = {}

    def _urlopen(req, timeout=None):
        seen["url"] = req if isinstance(req, str) else req.full_url
        seen["timeout"] = timeout
        return _Resp(json.dumps({"version": "0.30.11"}).encode())

    monkeypatch.setattr(llm_readiness.urllib.request, "urlopen", _urlopen)
    assert llm_readiness._fetch_ollama_version(timeout=1.5) == "0.30.11"
    assert seen["url"].endswith("/api/version") and seen["timeout"] == 1.5


def test_fetch_version_failure_is_none(monkeypatch):
    def _boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(llm_readiness.urllib.request, "urlopen", _boom)
    assert llm_readiness._fetch_ollama_version() is None


def test_ping_reports_the_real_version_not_the_literal_available(monkeypatch):
    class _Client:
        def __init__(self, timeout=None):
            pass

        def list(self):
            return MagicMock(models=[MagicMock(model="gemma4:e4b")])

    import ollama

    monkeypatch.setattr(ollama, "Client", _Client)
    monkeypatch.setattr(llm_readiness, "_fetch_ollama_version", lambda timeout=1.5: "0.30.11")
    reachable, version, tags = llm_readiness._ping_ollama()
    assert reachable is True and version == "0.30.11" and tags == ["gemma4:e4b"]


def test_ping_version_unknown_is_none_never_available(monkeypatch):
    class _Client:
        def __init__(self, timeout=None):
            pass

        def list(self):
            return MagicMock(models=[])

    import ollama

    monkeypatch.setattr(ollama, "Client", _Client)
    monkeypatch.setattr(llm_readiness, "_fetch_ollama_version", lambda timeout=1.5: None)
    assert llm_readiness._ping_ollama()[1] is None


def _readiness_host():
    from tests.test_api_phase1 import FakeHost

    host = FakeHost()
    host.motor._provider_config = {"active_provider": "local"}
    host.motor.current_model = "gemma4:e4b"
    host.motor._simulated_readiness = False  # exercise the real probe path
    return host


def test_readiness_exposes_version_support_and_warns_once_when_old(monkeypatch, caplog):
    monkeypatch.setattr(llm_readiness, "_ping_ollama", lambda: (True, "0.1.0", ["gemma4:e4b"]))
    monkeypatch.setattr(llm_readiness, "_detect_ollama_binary", lambda: ("o", True))
    monkeypatch.setattr(llm_readiness, "_old_version_warned", False, raising=False)
    host = _readiness_host()
    with caplog.at_level(logging.WARNING, logger=llm_readiness.logger.name):
        res = llm_readiness.resolve_llm_readiness(host)
        llm_readiness.resolve_llm_readiness(host)
    assert res.ollama["version"] == "0.1.0"
    assert res.ollama["version_supported"] is False
    assert res.ollama["min_version"] == ".".join(str(n) for n in llm_readiness.MIN_OLLAMA_VERSION)
    assert res.can_chat is True  # a warning, never a block
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1


def test_readiness_recent_version_is_supported_and_silent(monkeypatch, caplog):
    monkeypatch.setattr(llm_readiness, "_ping_ollama", lambda: (True, "0.30.11", ["gemma4:e4b"]))
    monkeypatch.setattr(llm_readiness, "_detect_ollama_binary", lambda: ("o", True))
    with caplog.at_level(logging.WARNING, logger=llm_readiness.logger.name):
        res = llm_readiness.resolve_llm_readiness(_readiness_host())
    assert res.ollama["version_supported"] is True
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# --- startup model re-resolution --------------------------------------------


def _saved_model(tmp_path, monkeypatch, tag):
    path = tmp_path / "last_model.json"
    path.write_text(json.dumps({"model": tag, "source": "test"}), encoding="utf-8")
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(path))


def test_unreachable_ollama_keeps_a_saved_catalog_model_unverified(tmp_path, monkeypatch):
    _saved_model(tmp_path, monkeypatch, "qwen3:4b")
    monkeypatch.setattr(settings, "_discover_installed_model_tags", lambda: settings._UnreachableTags())
    assert settings.resolve_startup_model() == ("qwen3:4b", "saved_unverified")


def test_unreachable_ollama_without_a_saved_model_uses_default(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setattr(settings, "_discover_installed_model_tags", lambda: settings._UnreachableTags())
    assert settings.resolve_startup_model() == (settings.DEFAULT_MODEL, "default")


def test_reachable_ollama_with_no_models_is_not_treated_as_unreachable(tmp_path, monkeypatch):
    _saved_model(tmp_path, monkeypatch, "bespoke:9b")
    assert settings.resolve_startup_model(installed_model_tags=set()) == (
        settings.DEFAULT_MODEL,
        "invalid_saved_fallback",
    )


def _motor(monkeypatch, tmp_path):
    import opencohost.core.llm_engine as llm_engine

    _saved_model(tmp_path, monkeypatch, "bespoke:9b")
    monkeypatch.setattr(settings, "_discover_installed_model_tags", lambda: settings._UnreachableTags())
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor._prepare_model = lambda m: True
    motor._refresh_max_turns_cap = lambda: None
    return motor


def _list(*tags):
    return MagicMock(models=[MagicMock(model=t) for t in tags])


def test_model_is_re_resolved_once_ollama_is_ready(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    assert motor.current_model == settings.DEFAULT_MODEL  # picked while Ollama was down
    motor.ollama.list.return_value = _list("bespoke:9b", "qwen3:4b")
    assert motor._check_ollama_service() is True
    assert motor.current_model == "bespoke:9b"
    assert motor._desired_model == "bespoke:9b"
    assert motor._model_source == "saved_runtime"


def test_re_resolution_never_picks_an_uninstalled_default(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor.ollama.list.return_value = _list("qwen3:4b")  # saved + default are both absent
    motor._check_ollama_service()
    assert motor.current_model == "qwen3:4b"
    assert motor._model_source == "installed_fallback"


def test_re_resolution_runs_only_once(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor.ollama.list.return_value = _list("bespoke:9b")
    motor._check_ollama_service()
    motor.current_model = "qwen3:4b"
    motor._desired_model = "qwen3:4b"  # the user switched afterwards
    motor._check_ollama_service()
    assert motor.current_model == "qwen3:4b"


def test_re_resolution_respects_a_model_the_user_already_chose(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    motor._pending_model_switch = "qwen3:4b"
    motor.ollama.list.return_value = _list("bespoke:9b", "qwen3:4b")
    motor._check_ollama_service()
    assert motor.current_model != "bespoke:9b" or motor._pending_model_switch is None


def test_re_resolution_with_an_empty_listing_changes_nothing(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    before = motor.current_model
    motor.ollama.list.return_value = _list()
    motor._check_ollama_service()
    assert motor.current_model == before


# --- bounded list() ---------------------------------------------------------


def test_check_ollama_service_does_not_hang_on_a_stuck_list(monkeypatch, tmp_path):
    import opencohost.core.engine.llm_engine_models as models_mod

    motor = _motor(monkeypatch, tmp_path)
    monkeypatch.setattr(models_mod, "OLLAMA_LIST_TIMEOUT_S", 0.2, raising=False)
    monkeypatch.setattr(settings, "OLLAMA_LIST_TIMEOUT_S", 0.2, raising=False)
    motor.ollama.list.side_effect = lambda: time.sleep(3)
    start = time.monotonic()
    assert motor._check_ollama_service(notify_unavailable=False) is False
    assert time.monotonic() - start < 1.5
    assert motor.is_ready is False


def test_discovery_uses_a_client_with_a_timeout(monkeypatch):
    import ollama

    seen = {}

    class _Client:
        def __init__(self, timeout=None, **_kw):
            seen["timeout"] = timeout

        def list(self):
            return MagicMock(models=[MagicMock(model="qwen3:4b")])

    monkeypatch.setattr(ollama, "Client", _Client)
    tags = settings._discover_installed_model_tags()
    assert tags == {"qwen3:4b"} and not isinstance(tags, settings._UnreachableTags)
    assert seen["timeout"] == settings.OLLAMA_LIST_TIMEOUT_S


def test_discovery_failure_is_an_empty_unreachable_set(monkeypatch):
    import ollama

    class _Client:
        def __init__(self, timeout=None, **_kw):
            pass

        def list(self):
            raise RuntimeError("down")

    monkeypatch.setattr(ollama, "Client", _Client)
    tags = settings._discover_installed_model_tags()
    assert tags == set() and isinstance(tags, settings._UnreachableTags)


# --- readiness recovery (engine-readiness-recovery-20261111) -----------------


def _down(motor):
    motor.ollama.list.side_effect = RuntimeError("down")
    motor.startup_check_done = True
    motor.is_ready = False
    motor.ui_callback = MagicMock()


def _up(motor, *tags):
    motor.ollama.list.side_effect = None
    motor.ollama.list.return_value = _list(*tags)


def _log_text(motor):
    lines = []
    while not motor.log_queue.empty():
        lines.append(motor.log_queue.get_nowait())
    return "\n".join(lines)


def test_reprobe_tick_waits_one_backoff_step_before_the_first_probe(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    _down(motor)
    motor._ollama_reprobe_tick()
    assert motor.ollama.list.call_count == 0  # only scheduled
    assert motor._reprobe_next_at is not None


def test_reprobe_tick_recovers_and_reresolves_the_saved_model(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    _down(motor)
    motor._ollama_reprobe_tick()
    motor._reprobe_next_at = 0.0
    motor._ollama_reprobe_tick()  # still down
    assert motor.is_ready is False
    _up(motor, "bespoke:9b", "qwen3:4b")
    motor._reprobe_next_at = 0.0
    motor._ollama_reprobe_tick()
    assert motor.is_ready is True
    assert motor.current_model == "bespoke:9b"
    assert motor._reprobe_delay_s == 2.0  # backoff reset


def test_reprobe_backoff_doubles_and_caps_at_thirty_seconds(monkeypatch, tmp_path):
    import types

    import opencohost.core.engine.llm_engine_models as models_mod

    motor = _motor(monkeypatch, tmp_path)
    _down(motor)
    clock = {"t": 1000.0}
    monkeypatch.setattr(models_mod, "time", types.SimpleNamespace(monotonic=lambda: clock["t"], sleep=time.sleep))
    motor._ollama_reprobe_tick()  # schedules first probe 2s out
    assert motor._reprobe_next_at == 1002.0
    gaps = []
    for _ in range(7):
        clock["t"] = motor._reprobe_next_at
        motor._ollama_reprobe_tick()
        gaps.append(motor._reprobe_next_at - clock["t"])
    assert gaps == [4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]


def test_failed_reprobes_do_not_log_or_notify_the_ui(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    _down(motor)
    _log_text(motor)
    motor._ollama_reprobe_tick()
    for _ in range(3):
        motor._reprobe_next_at = 0.0
        motor._ollama_reprobe_tick()
    assert motor.ui_callback.call_count == 0
    assert _log_text(motor) == ""


def test_reprobe_tick_is_inert_when_ready_cloud_or_before_startup_check(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    _down(motor)
    motor._reprobe_next_at = 0.0
    motor.startup_check_done = False
    motor._ollama_reprobe_tick()
    motor.startup_check_done = True
    motor.is_ready = True
    motor._ollama_reprobe_tick()
    motor.is_ready = False
    motor._provider_config = {"active_provider": "openai", "fallback_mode": "auto"}
    assert motor._is_local is False
    motor._ollama_reprobe_tick()
    assert motor.ollama.list.call_count == 0


def test_reprobe_tick_skips_while_a_turn_is_in_flight(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    _down(motor)
    _up(motor, "bespoke:9b")
    motor._reprobe_next_at = 0.0
    motor._processing = True
    motor._ollama_reprobe_tick()
    assert motor.is_ready is False
    assert motor.ollama.list.call_count == 0


def _turn_motor(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    _down(motor)
    motor._ejecutar_inferencia = MagicMock()
    motor._complete_processing_cycle = MagicMock()
    return motor


def test_turn_with_a_stale_not_ready_flag_reconciles_and_proceeds(monkeypatch, tmp_path):
    motor = _turn_motor(monkeypatch, tmp_path)
    _up(motor, "bespoke:9b")
    motor._dispatch_command("process_context", "hello")
    assert motor.is_ready is True
    assert motor._ejecutar_inferencia.call_count == 1
    assert not any(c.args == ("ollama_unavailable",) for c in motor.ui_callback.call_args_list)


def test_turn_refusal_probes_at_most_once_per_window_and_hint_is_accurate(monkeypatch, tmp_path):
    motor = _turn_motor(monkeypatch, tmp_path)
    _log_text(motor)
    motor._dispatch_command("process_context", "one")
    motor._dispatch_command("process_context", "two")
    assert motor.ollama.list.call_count == 1  # second refusal is inside the 5s window
    assert motor._ejecutar_inferencia.call_count == 0
    emitted = [c.args[0] for c in motor.ui_callback.call_args_list]
    assert emitted.count("ollama_unavailable") == 2
    text = _log_text(motor)
    assert "Ollama no esta listo" in text
    assert "boton" not in text.lower()


def test_same_model_switch_while_not_ready_reconciles(monkeypatch, tmp_path):
    motor = _turn_motor(monkeypatch, tmp_path)
    _up(motor, "bespoke:9b")
    motor._dispatch_command("switch_model", motor.current_model)
    assert motor.is_ready is True


def test_same_model_switch_while_ollama_is_down_reports_failure(monkeypatch, tmp_path):
    motor = _turn_motor(monkeypatch, tmp_path)
    motor._dispatch_command("switch_model", motor.current_model)
    assert motor.is_ready is False
    assert [c.args[0] for c in motor.ui_callback.call_args_list] == ["model_switch_failed"]


def test_same_model_switch_when_ready_and_warmed_stays_silent(monkeypatch, tmp_path):
    motor = _turn_motor(monkeypatch, tmp_path)
    motor.is_ready = True
    motor._warmed_model = motor.current_model
    motor._desired_model = motor.current_model
    motor._dispatch_command("switch_model", motor.current_model)
    assert motor.ui_callback.call_count == 0
    assert motor.ollama.list.call_count == 0


def _wake_host(tmp_path, monkeypatch):
    from queue import Queue
    from types import SimpleNamespace

    from tests.test_ollama_autowake import _make_host

    host, mod = _make_host(tmp_path, monkeypatch)
    host.motor = SimpleNamespace(is_ready=False, command_queue=Queue())
    return host, mod


def test_wake_completion_asks_an_unready_engine_to_recheck_ollama(tmp_path, monkeypatch):
    from types import SimpleNamespace

    host, _mod = _wake_host(tmp_path, monkeypatch)
    host._nudge_engine_after_wake(SimpleNamespace(status="ready"))
    assert host.motor.command_queue.get_nowait() == ("check_ollama", None)


def test_wake_completion_does_not_nudge_on_failure_or_when_already_ready(tmp_path, monkeypatch):
    from types import SimpleNamespace

    host, _mod = _wake_host(tmp_path, monkeypatch)
    host._nudge_engine_after_wake(SimpleNamespace(status="timeout_waiting_ready"))
    host._nudge_engine_after_wake(SimpleNamespace(status="process_exited_early"))
    host._nudge_engine_after_wake(None)
    assert host.motor.command_queue.empty()
    host.motor.is_ready = True
    host._nudge_engine_after_wake(SimpleNamespace(status="ready"))
    assert host.motor.command_queue.empty()
    host.motor = None
    host._nudge_engine_after_wake(SimpleNamespace(status="ready"))  # engine not built yet: no error


def test_eager_wake_worker_nudges_the_engine_when_ollama_comes_up(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.test_ollama_autowake import _FakeReadyWatchdog, _join_wake_threads

    host, mod = _wake_host(tmp_path, monkeypatch)

    class _Manager:
        def __init__(self, **_kw):
            pass

        def start_and_wait(self, exe, models_dir):
            return SimpleNamespace(status="ready")

    monkeypatch.setattr(mod, "OllamaStartupManager", _Manager)
    monkeypatch.setattr(mod, "OllamaWatchdog", _FakeReadyWatchdog)
    monkeypatch.setattr(mod, "_find_ollama_executable", lambda: "ollama.exe")
    host._wake_ollama_eager()
    _join_wake_threads()
    assert host.motor.command_queue.get_nowait() == ("check_ollama", None)
