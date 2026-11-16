"""Specs for the dedicated vision Ollama daemon lifecycle (vision-on-demand V1.4).

No real ``ollama`` is spawned and no socket is opened: the process factory,
the HTTP transport, the tree killer and the clock are all injected fakes.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from opencohost.core.providers.local.vision_daemon import (
    VisionDaemon,
    VisionDaemonStatus,
)


VERSION_URL = "http://127.0.0.1:11435/api/version"
TAGS_URL = "http://127.0.0.1:11435/api/tags"
GENERATE_URL = "http://127.0.0.1:11435/api/generate"
PS_URL = "http://127.0.0.1:11435/api/ps"


class FakeProcess:
    def __init__(self, pid: int = 4242, poll_value: int | None = None) -> None:
        self.pid = pid
        self.poll_value = poll_value

    def poll(self) -> int | None:
        return self.poll_value


class FakePopen:
    def __init__(self, process: FakeProcess | None = None, error: Exception | None = None) -> None:
        self.process = process or FakeProcess()
        self.error = error
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.process


class FakeHttp:
    """Scripted transport. ``version`` is a list of responses consumed in order
    (the last one repeats). A response is ``(status, body)`` or an exception."""

    def __init__(self, version=None, tags=None, generate=(200, "{}"), ps=None) -> None:
        self.version = list(version or [ConnectionRefusedError("refused")])
        self.tags = tags
        self.generate = generate
        self.ps = ps
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.post_timeouts: list[float] = []

    @staticmethod
    def _resolve(response):
        if isinstance(response, BaseException):
            raise response
        return response

    def get(self, url: str, timeout: float):
        self.gets.append(url)
        if url == VERSION_URL:
            response = self.version.pop(0) if len(self.version) > 1 else self.version[0]
            return self._resolve(response)
        if url == TAGS_URL:
            return self._resolve(self.tags)
        if url == PS_URL:
            return self._resolve(self.ps)
        raise AssertionError(f"unexpected GET {url}")

    def post_json(self, url: str, payload: dict, timeout: float):
        self.posts.append((url, payload))
        self.post_timeouts.append(timeout)
        return self._resolve(self.generate)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


OLLAMA_OK = (200, json.dumps({"version": "0.30.11"}))
REFUSED = ConnectionRefusedError("refused")


def make_daemon(http: FakeHttp, popen: FakePopen | None = None, killed: list | None = None, **kwargs):
    clock = FakeClock()
    killed_list = killed if killed is not None else []
    daemon = VisionDaemon(
        ollama_exe="C:/fake/ollama.exe",
        models_dir="D:/models",
        startup_timeout_s=kwargs.pop("startup_timeout_s", 5.0),
        popen=popen or FakePopen(),
        http=http,
        kill_tree=killed_list.append,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        base_env=kwargs.pop("base_env", {"PATH": "C:/bin", "OLLAMA_HOST": "127.0.0.1:11434"}),
        **kwargs,
    )
    return daemon, clock


# --- ensure_running -----------------------------------------------------------


def test_reuses_healthy_external_daemon_without_spawning() -> None:
    popen = FakePopen()
    daemon, _ = make_daemon(FakeHttp(version=[OLLAMA_OK]), popen)

    status = daemon.ensure_running()

    assert popen.calls == []
    assert status.state == "external"
    assert status.owned is False
    assert status.pid is None
    assert status.port == 11435


def test_spawns_and_reports_running_once_healthy() -> None:
    popen = FakePopen(FakeProcess(pid=777))
    http = FakeHttp(version=[REFUSED, REFUSED, REFUSED, OLLAMA_OK])
    daemon, _ = make_daemon(http, popen)

    status = daemon.ensure_running()

    assert len(popen.calls) == 1
    args, kwargs = popen.calls[0]
    assert args[0] == ["C:/fake/ollama.exe", "serve"]
    assert status == VisionDaemonStatus(
        state="running", owned=True, pid=777, port=11435, last_error=None
    )


def test_spawn_env_has_overrides_and_does_not_mutate_os_environ(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    before = dict(os.environ)
    popen = FakePopen()
    daemon, _ = make_daemon(FakeHttp(version=[REFUSED, OLLAMA_OK]), popen, base_env=None)

    daemon.ensure_running()

    env = popen.calls[0][1]["env"]
    assert env["OLLAMA_HOST"] == "127.0.0.1:11435"
    assert env["OLLAMA_MODELS"] == "D:/models"
    assert env["OLLAMA_MAX_LOADED_MODELS"] == "1"
    assert env["OLLAMA_NUM_PARALLEL"] == "1"
    assert env is not os.environ
    assert dict(os.environ) == before


def test_spawn_env_overrides_operator_values_and_keeps_parent_env() -> None:
    popen = FakePopen()
    base = {
        "PATH": "C:/bin",
        "OLLAMA_MAX_LOADED_MODELS": "3",
        "OLLAMA_NUM_PARALLEL": "4",
        "OLLAMA_MODELS": "X:/other",
    }
    daemon, _ = make_daemon(FakeHttp(version=[REFUSED, OLLAMA_OK]), popen, base_env=base)

    daemon.ensure_running()

    env = popen.calls[0][1]["env"]
    assert env["PATH"] == "C:/bin"
    assert env["OLLAMA_MAX_LOADED_MODELS"] == "1"
    assert env["OLLAMA_NUM_PARALLEL"] == "1"
    assert env["OLLAMA_MODELS"] == "D:/models"
    assert base["OLLAMA_MAX_LOADED_MODELS"] == "3"


def test_spawn_without_log_paths_uses_devnull_and_no_console_flags() -> None:
    import subprocess

    popen = FakePopen()
    daemon, _ = make_daemon(FakeHttp(version=[REFUSED, OLLAMA_OK]), popen, creationflags=0x08000000)

    daemon.ensure_running()

    kwargs = popen.calls[0][1]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["creationflags"] == 0x08000000


def test_spawn_with_log_paths_appends_to_log_files(tmp_path) -> None:
    popen = FakePopen()
    out_log = tmp_path / "logs" / "vision_stdout.log"
    err_log = tmp_path / "logs" / "vision_stderr.log"
    daemon, _ = make_daemon(
        FakeHttp(version=[REFUSED, OLLAMA_OK]),
        popen,
        stdout_log_path=str(out_log),
        stderr_log_path=str(err_log),
    )

    daemon.ensure_running()

    kwargs = popen.calls[0][1]
    assert kwargs["stdout"].name == str(out_log)
    assert kwargs["stderr"].name == str(err_log)
    assert kwargs["stdout"].closed and kwargs["stderr"].closed
    assert out_log.exists() and err_log.exists()


def test_spawn_timeout_kills_child_and_fails() -> None:
    process = FakeProcess(pid=99)
    killed: list = []
    daemon, clock = make_daemon(
        FakeHttp(version=[REFUSED]), FakePopen(process), killed, startup_timeout_s=3.0
    )

    status = daemon.ensure_running()

    assert killed == [process]
    assert status.state == "failed"
    assert status.owned is False
    assert status.pid is None
    assert status.last_error == "startup_timeout"
    assert clock.now >= 3.0


def test_child_exiting_early_fails_without_waiting_for_timeout() -> None:
    process = FakeProcess(pid=5, poll_value=1)
    daemon, clock = make_daemon(FakeHttp(version=[REFUSED]), FakePopen(process), startup_timeout_s=30.0)

    status = daemon.ensure_running()

    assert status.state == "failed"
    assert status.last_error == "process_exited_early"
    assert clock.now < 30.0


def test_spawn_error_fails_with_reason_code() -> None:
    daemon, _ = make_daemon(FakeHttp(version=[REFUSED]), FakePopen(error=FileNotFoundError("nope")))

    status = daemon.ensure_running()

    assert status.state == "failed"
    assert status.last_error == "spawn_failed"


def test_missing_executable_fails_without_spawning() -> None:
    popen = FakePopen()
    daemon = VisionDaemon(
        ollama_exe=None,
        models_dir="D:/models",
        popen=popen,
        http=FakeHttp(version=[REFUSED]),
        kill_tree=lambda _p: None,
    )

    status = daemon.ensure_running()

    assert popen.calls == []
    assert status.state == "failed"
    assert status.last_error == "ollama_not_found"


@pytest.mark.parametrize(
    "response",
    [
        (200, "<html>not ollama</html>"),
        (200, json.dumps(["a", "list"])),
        (200, json.dumps({"hello": "world"})),
        (404, "not found"),
    ],
)
def test_port_answered_by_something_else_fails_port_in_use(response) -> None:
    popen = FakePopen()
    daemon, _ = make_daemon(FakeHttp(version=[response]), popen)

    status = daemon.ensure_running()

    assert popen.calls == []
    assert status.state == "failed"
    assert status.last_error == "port_in_use"


def test_ensure_running_is_idempotent_while_owned_daemon_is_healthy() -> None:
    popen = FakePopen()
    daemon, _ = make_daemon(FakeHttp(version=[REFUSED, OLLAMA_OK]), popen)

    daemon.ensure_running()
    status = daemon.ensure_running()

    assert len(popen.calls) == 1
    assert status.state == "running"
    assert status.owned is True


def test_concurrent_ensure_running_spawns_once() -> None:
    popen = FakePopen()
    http = FakeHttp(version=[REFUSED, OLLAMA_OK])
    daemon, _ = make_daemon(http, popen)
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        daemon.ensure_running()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(popen.calls) == 1
    assert daemon.status().state == "running"


# --- stop ----------------------------------------------------------------------


def test_stop_kills_owned_process_tree() -> None:
    process = FakeProcess(pid=321)
    killed: list = []
    daemon, _ = make_daemon(FakeHttp(version=[REFUSED, OLLAMA_OK]), FakePopen(process), killed)
    daemon.ensure_running()

    daemon.stop()

    assert killed == [process]
    status = daemon.status()
    assert status.state == "stopped"
    assert status.owned is False
    assert status.pid is None


def test_stop_never_touches_external_daemon() -> None:
    killed: list = []
    daemon, _ = make_daemon(FakeHttp(version=[OLLAMA_OK]), killed=killed)
    daemon.ensure_running()

    daemon.stop()

    assert killed == []
    assert daemon.status().state == "stopped"


def test_stop_is_idempotent() -> None:
    killed: list = []
    daemon, _ = make_daemon(FakeHttp(version=[REFUSED, OLLAMA_OK]), killed=killed)
    daemon.ensure_running()

    daemon.stop()
    daemon.stop()
    VisionDaemon(ollama_exe="x", models_dir="y", http=FakeHttp(), kill_tree=killed.append).stop()

    assert len(killed) == 1


def test_stop_swallows_kill_errors_and_still_releases() -> None:
    def boom(_process) -> None:
        raise OSError("access denied")

    daemon = VisionDaemon(
        ollama_exe="C:/fake/ollama.exe",
        models_dir="D:/models",
        popen=FakePopen(),
        http=FakeHttp(version=[REFUSED, OLLAMA_OK]),
        kill_tree=boom,
        sleep=lambda _s: None,
        monotonic=lambda: 0.0,
        base_env={},
    )
    daemon.ensure_running()

    daemon.stop()

    assert daemon.status().state == "stopped"
    assert daemon.status().last_error == "stop_failed"


def test_initial_status_is_stopped() -> None:
    daemon, _ = make_daemon(FakeHttp())

    assert daemon.status() == VisionDaemonStatus(
        state="stopped", owned=False, pid=None, port=11435, last_error=None
    )


# --- models ----------------------------------------------------------------------


TAGS_BODY = json.dumps(
    {
        "models": [
            {"name": "qwen3-vl:4b-instruct", "model": "qwen3-vl:4b-instruct"},
            {"name": "llava:latest", "model": "llava:latest"},
        ]
    }
)


@pytest.mark.parametrize(
    "model, expected",
    [
        ("qwen3-vl:4b-instruct", True),
        ("llava", True),
        ("llava:latest", True),
        ("qwen3-vl", False),
        ("qwen3-vl:4b", False),
        ("gemma4:e4b", False),
        ("", False),
    ],
)
def test_model_installed_tag_matching(model, expected) -> None:
    http = FakeHttp(tags=(200, TAGS_BODY))
    daemon, _ = make_daemon(http)

    assert daemon.model_installed(model) is expected


def test_model_installed_matches_untagged_listing_against_latest() -> None:
    http = FakeHttp(tags=(200, json.dumps({"models": [{"name": "moondream"}]})))
    daemon, _ = make_daemon(http)

    assert daemon.model_installed("moondream:latest") is True


@pytest.mark.parametrize("tags", [REFUSED, (500, "boom"), (200, "not json"), (200, json.dumps({"x": 1}))])
def test_model_installed_is_false_when_tags_unavailable(tags) -> None:
    daemon, _ = make_daemon(FakeHttp(tags=tags))

    assert daemon.model_installed("llava") is False


def test_unload_posts_keep_alive_zero_to_vision_port() -> None:
    http = FakeHttp()
    daemon, _ = make_daemon(http)

    assert daemon.unload("qwen3-vl:4b-instruct") is True
    assert http.posts == [(GENERATE_URL, {"model": "qwen3-vl:4b-instruct", "keep_alive": 0})]


@pytest.mark.parametrize("generate", [REFUSED, (500, "boom")])
def test_unload_returns_false_on_error(generate) -> None:
    daemon, _ = make_daemon(FakeHttp(generate=generate))

    assert daemon.unload("qwen3-vl:4b-instruct") is False


def test_custom_port_is_used_for_probe_and_env() -> None:
    http = FakeHttp()
    urls: list[str] = []

    def get(url: str, timeout: float):
        urls.append(url)
        return OLLAMA_OK

    http.get = get  # type: ignore[method-assign]
    daemon, _ = make_daemon(http, port=11555)

    status = daemon.ensure_running()

    assert status.port == 11555
    assert urls == ["http://127.0.0.1:11555/api/version"]


# -- V1.8: pre-warm and residency probe ------------------------------------------


def test_warm_is_a_load_only_generate_with_keep_alive_and_a_long_timeout() -> None:
    http = FakeHttp(generate=(200, json.dumps({"done": True, "done_reason": "load"})))
    daemon, _ = make_daemon(http)

    assert daemon.warm("qwen3-vl:4b-instruct", keep_alive=-1, timeout_s=90.0) is True
    # no prompt and no messages: Ollama loads the model and returns at once
    assert http.posts == [(GENERATE_URL, {"model": "qwen3-vl:4b-instruct", "keep_alive": -1})]
    assert http.post_timeouts == [90.0]


@pytest.mark.parametrize("generate", [REFUSED, (404, "model not found"), (500, "boom")])
def test_warm_returns_false_on_error(generate) -> None:
    daemon, _ = make_daemon(FakeHttp(generate=generate))

    assert daemon.warm("qwen3-vl:4b-instruct", keep_alive=-1) is False


def test_loaded_models_lists_resident_models_from_api_ps() -> None:
    body = json.dumps({"models": [{"name": "qwen3-vl:4b-instruct", "model": "qwen3-vl:4b-instruct"},
                                  {"model": "moondream"}]})
    daemon, _ = make_daemon(FakeHttp(ps=(200, body)))

    assert daemon.loaded_models() == ["qwen3-vl:4b-instruct", "moondream:latest"]
    assert daemon.model_loaded("qwen3-vl:4b-instruct") is True
    assert daemon.model_loaded("moondream:latest") is True
    assert daemon.model_loaded("llava") is False


def test_loaded_models_empty_when_nothing_is_resident() -> None:
    daemon, _ = make_daemon(FakeHttp(ps=(200, json.dumps({"models": []}))))

    assert daemon.loaded_models() == []
    assert daemon.model_loaded("qwen3-vl:4b-instruct") is False


@pytest.mark.parametrize("ps", [REFUSED, (500, "boom"), (200, "not json"), (200, json.dumps({"x": 1}))])
def test_loaded_models_is_none_when_ps_unavailable(ps) -> None:
    daemon, _ = make_daemon(FakeHttp(ps=ps))

    assert daemon.loaded_models() is None
    assert daemon.model_loaded("qwen3-vl:4b-instruct") is None
