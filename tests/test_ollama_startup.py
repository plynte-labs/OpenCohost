from __future__ import annotations

from dataclasses import dataclass

from opencohost.core.ollama_startup import OllamaStartupManager


@dataclass
class FakeProcess:
    stderr_text: str = ""
    poll_values: tuple[int | None, ...] = (None,)

    def __post_init__(self) -> None:
        self.stderr = FakeStream(self.stderr_text)
        self._polls = list(self.poll_values)

    def poll(self) -> int | None:
        if len(self._polls) > 1:
            return self._polls.pop(0)
        return self._polls[0]


class FakeStream:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


class ReadShouldNotBeCalled:
    def read(self) -> str:
        raise AssertionError("live stderr pipe should not be drained before process exits")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_process_exits_early_reports_stderr_snippet() -> None:
    proc = FakeProcess(
        stderr_text="bind: Only one usage of each socket address is normally permitted\nextra details",
        poll_values=(1,),
    )

    manager = OllamaStartupManager(
        popen=lambda *args, **kwargs: proc,
        is_ready=lambda: False,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    result = manager.start_and_wait("C:/Ollama/ollama.exe", "D:/ollama-models")  # path-ok: test exercises drive-letter path handling

    assert result.status == "process_exited_early"
    assert result.process is proc
    assert "Only one usage" in result.diagnostic
    assert "silently" not in result.diagnostic.lower()


def test_slow_readiness_before_configured_timeout_returns_ready() -> None:
    proc = FakeProcess(poll_values=(None, None, None, None))
    clock = FakeClock()
    probes = {"count": 0}

    def is_ready() -> bool:
        probes["count"] += 1
        return probes["count"] == 5

    manager = OllamaStartupManager(
        popen=lambda *args, **kwargs: proc,
        is_ready=is_ready,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        timeout_seconds=30.0,
        poll_interval_seconds=5.0,
    )

    result = manager.start_and_wait("ollama", "D:/ollama-models")  # path-ok: test exercises drive-letter path handling

    assert result.status == "ready"
    assert result.process is proc
    assert clock.now == 15.0
    assert len(clock.sleeps) == 3


def test_ready_before_start_reports_already_running_without_duplicate_process() -> None:
    popen_calls = []

    manager = OllamaStartupManager(
        popen=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        is_ready=lambda: True,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    result = manager.start_and_wait("ollama", "D:/ollama-models")  # path-ok: test exercises drive-letter path handling

    assert result.status == "already_running"
    assert result.process is None
    assert popen_calls == []


def test_start_env_includes_ollama_models_path() -> None:
    proc = FakeProcess(poll_values=(None,))
    captured: dict[str, object] = {}
    probes = {"count": 0}

    def is_ready() -> bool:
        probes["count"] += 1
        return probes["count"] == 2

    def popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return proc

    manager = OllamaStartupManager(
        popen=popen,
        is_ready=is_ready,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        base_env={"EXISTING": "1"},
    )

    result = manager.start_and_wait("ollama", "/fake/custom-ollama-models")

    assert result.status == "ready"
    assert captured["args"] == ["ollama", "serve"]
    assert captured["env"] == {
        "EXISTING": "1",
        "OLLAMA_MODELS": "/fake/custom-ollama-models",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_FLASH_ATTENTION": "1",
    }


def test_operator_env_overrides_are_preserved() -> None:
    """A3 RAM ceilings use setdefault, so an operator who already exported
    OLLAMA_NUM_PARALLEL / OLLAMA_MAX_LOADED_MODELS keeps their value while the
    other ceiling default is still applied (ram_llm_hardening_20260626 Phase 0)."""
    proc = FakeProcess(poll_values=(None,))
    captured: dict[str, object] = {}
    probes = {"count": 0}

    def is_ready() -> bool:
        probes["count"] += 1
        return probes["count"] == 2

    def popen(args, **kwargs):
        captured["env"] = kwargs["env"]
        return proc

    manager = OllamaStartupManager(
        popen=popen,
        is_ready=is_ready,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        base_env={"OLLAMA_NUM_PARALLEL": "4"},
    )

    result = manager.start_and_wait("ollama", "/fake/models")

    assert result.status == "ready"
    assert captured["env"]["OLLAMA_NUM_PARALLEL"] == "4"  # operator override preserved
    assert captured["env"]["OLLAMA_MAX_LOADED_MODELS"] == "1"  # default still applied


def test_operator_can_disable_flash_attention_and_no_kv_overhead_knobs() -> None:
    """Flash attention uses setdefault, so an operator who exported
    OLLAMA_FLASH_ATTENTION=0 keeps it. KV-cache-type and GPU-overhead are NOT set
    by the app: the ADR-023 spill premise they guarded did not hold at runtime
    (gemma4:e4b is ~3.3GB resident, 100% GPU with headroom — not 9.6GB), so FA
    stays as the one free win. See ADR-023 Runtime Correction."""
    proc = FakeProcess(poll_values=(None,))
    captured: dict[str, object] = {}
    probes = {"count": 0}

    def is_ready() -> bool:
        probes["count"] += 1
        return probes["count"] == 2

    def popen(args, **kwargs):
        captured["env"] = kwargs["env"]
        return proc

    manager = OllamaStartupManager(
        popen=popen,
        is_ready=is_ready,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        base_env={"OLLAMA_FLASH_ATTENTION": "0"},
    )

    result = manager.start_and_wait("ollama", "/fake/models")

    assert result.status == "ready"
    assert captured["env"]["OLLAMA_FLASH_ATTENTION"] == "0"  # operator override preserved
    assert "OLLAMA_KV_CACHE_TYPE" not in captured["env"]  # trimmed — app does not set it
    assert "OLLAMA_GPU_OVERHEAD" not in captured["env"]  # trimmed — app does not set it


def test_timeout_waiting_ready_reports_process_and_model_path_context(tmp_path) -> None:
    stderr_log = tmp_path / "ollama_stderr.log"
    stderr_log.write_text("loading manifest", encoding="utf-8")
    proc = FakeProcess(poll_values=(None, None, None))
    clock = FakeClock()

    manager = OllamaStartupManager(
        popen=lambda *args, **kwargs: proc,
        is_ready=lambda: False,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        timeout_seconds=10.0,
        poll_interval_seconds=5.0,
        stderr_log_path=str(stderr_log),
    )

    result = manager.start_and_wait("ollama", "D:/ollama-models")  # path-ok: test exercises drive-letter path handling

    assert result.status == "timeout_waiting_ready"
    assert result.process is proc
    assert "D:/ollama-models" in result.diagnostic  # path-ok: test exercises drive-letter path handling
    assert "loading manifest" in result.diagnostic


def test_timeout_does_not_block_reading_live_stderr_pipe_without_log_path() -> None:
    proc = FakeProcess(poll_values=(None, None, None))
    proc.stderr = ReadShouldNotBeCalled()
    clock = FakeClock()

    manager = OllamaStartupManager(
        popen=lambda *args, **kwargs: proc,
        is_ready=lambda: False,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        timeout_seconds=1.0,
        poll_interval_seconds=1.0,
    )

    result = manager.start_and_wait("ollama", "D:/ollama-models")  # path-ok: test exercises drive-letter path handling

    assert result.status == "timeout_waiting_ready"
    assert "sin stderr capturado" in result.diagnostic


def test_popen_raises_reports_process_exited_early() -> None:
    """Ollama not installed or permission denied — Popen raises."""
    manager = OllamaStartupManager(
        popen=_raise_file_not_found,
        is_ready=lambda: False,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    result = manager.start_and_wait("C:/NoExiste/ollama.exe", "D:/ollama-models")  # path-ok: test exercises drive-letter path handling

    assert result.status == "process_exited_early"
    assert result.process is None
    assert "No se pudo iniciar Ollama" in result.diagnostic
    assert "FileNotFoundError" in result.diagnostic or "No such file" in result.diagnostic


def _raise_file_not_found(*args, **kwargs):
    raise FileNotFoundError("[WinError 2] No such file or directory: 'C:/NoExiste/ollama.exe'")  # path-ok: test exercises drive-letter path handling

