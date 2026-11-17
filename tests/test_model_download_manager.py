"""Unit tests for the in-app model download manager (model-download-manager T2).

Every Ollama pull here is a scripted fake: no test touches a real Ollama
daemon or the network. The job record always lives under ``tmp_path``.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest
from ollama import ResponseError

from opencohost.core.model_downloads import manager as mdm
from opencohost.core.model_downloads.manager import (
    ActiveDownloadError,
    InvalidTransitionError,
    JobNotFoundError,
    ModelDownloadManager,
)

SUCCESS_TAIL = [
    {"status": "verifying sha256 digest"},
    {"status": "writing manifest"},
    {"status": "success"},
]


def layer(digest: str, total: int, completed: int) -> dict:
    return {"status": f"pulling {digest}", "digest": digest, "total": total, "completed": completed}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Stream:
    def __init__(self, script, owner) -> None:
        self._script = script
        self._owner = owner

    def __iter__(self):
        for item in self._script:
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                item()
                continue
            yield item

    def abort(self) -> None:
        self._owner.aborted += 1


class ScriptedPull:
    """pull_fn fake: each call consumes the next scripted attempt."""

    def __init__(self, *attempts) -> None:
        self.attempts = list(attempts)
        self.calls: list[str] = []
        self.aborted = 0

    def __call__(self, tag: str):
        self.calls.append(tag)
        script = self.attempts.pop(0) if self.attempts else []
        return _Stream(script, self)


class Recorder:
    def __init__(self) -> None:
        self.waits: list[float] = []
        self.events: list[str] = []

    def wait(self, stop_event, seconds):
        self.waits.append(seconds)
        return stop_event.is_set()

    def event(self, name):
        self.events.append(name)


def make_manager(tmp_path, pull, *, discover=None, clock=None, rec=None, record_name="model_downloads.json"):
    rec = rec or Recorder()
    mgr = ModelDownloadManager(
        tmp_path / record_name,
        pull_fn=pull,
        discover_fn=discover if discover is not None else (lambda: []),
        on_event=rec.event,
        clock=clock or FakeClock(),
        wait_fn=rec.wait,
    )
    return mgr, rec


def only_job(mgr) -> dict:
    jobs = mgr.list_jobs()
    assert len(jobs) == 1
    return jobs[0]


# ── happy path, aggregation, discovery refresh ───────────────────────────


def test_happy_path_aggregates_layers_and_refreshes_discovery(tmp_path):
    snapshots = []
    holder = {}
    pull = ScriptedPull(
        [
            {"status": "pulling manifest"},
            layer("sha256:a", 100, 0),
            layer("sha256:a", 100, 50),
            layer("sha256:b", 50, 25),
            lambda: snapshots.append(holder["mgr"].get_job(holder["id"])),
            layer("sha256:a", 100, 100),
            layer("sha256:b", 50, 50),
            *SUCCESS_TAIL,
        ]
    )
    discovered_calls = []

    def discover():
        discovered_calls.append(1)
        return ["qwen3:1.7b", "llama3"]

    mgr, rec = make_manager(tmp_path, pull, discover=discover)
    holder["mgr"] = mgr
    job = mgr.start("qwen3:1.7b")
    holder["id"] = job["id"]
    assert mgr.join(5)

    mid = snapshots[0]
    assert mid["total_bytes"] == 150
    assert mid["completed_bytes"] == 75
    assert mid["percent"] == pytest.approx(50.0)
    assert mid["phase"] == "pulling"

    final = only_job(mgr)
    assert final["state"] == "completed"
    assert final["total_bytes"] == 150
    assert final["completed_bytes"] == 150
    assert final["percent"] == pytest.approx(100.0)
    assert final["installed"] is True
    assert discovered_calls == [1]
    assert rec.events == ["download_start", "download_done"]
    assert pull.calls == ["qwen3:1.7b"]


def test_discovery_failure_still_completes(tmp_path):
    def boom():
        raise RuntimeError("discovery down")

    pull = ScriptedPull([layer("sha256:a", 10, 10), *SUCCESS_TAIL])
    mgr, _ = make_manager(tmp_path, pull, discover=boom)
    mgr.start("llama3")
    assert mgr.join(5)
    final = only_job(mgr)
    assert final["state"] == "completed"
    assert final["installed"] is None


# ── speed EMA / ETA ───────────────────────────────────────────────────────


def test_speed_is_an_ema_of_completed_deltas_and_eta_follows(tmp_path):
    clock = FakeClock()
    snaps = []
    holder = {}

    def snap():
        snaps.append(holder["mgr"].get_job(holder["id"]))

    pull = ScriptedPull(
        [
            layer("sha256:a", 1000, 0),
            lambda: clock.advance(1.0),
            layer("sha256:a", 1000, 100),
            snap,
            lambda: clock.advance(1.0),
            layer("sha256:a", 1000, 300),
            snap,
            *SUCCESS_TAIL,
        ]
    )
    mgr, _ = make_manager(tmp_path, pull, clock=clock)
    holder["mgr"] = mgr
    holder["id"] = mgr.start("llama3")["id"]
    assert mgr.join(5)

    first, second = snaps
    assert first["speed_bps"] == pytest.approx(100.0)
    assert first["eta_seconds"] == pytest.approx(900 / 100.0)
    alpha = mdm.SPEED_EMA_ALPHA
    expected = alpha * 200.0 + (1 - alpha) * 100.0
    assert second["speed_bps"] == pytest.approx(expected)
    assert second["eta_seconds"] == pytest.approx(700 / expected)


# ── retry bounds / backoff ───────────────────────────────────────────────


def test_network_errors_retry_with_bounded_backoff_then_fail(tmp_path):
    attempts = [[ConnectionError("net down")] for _ in range(mdm.MAX_RETRIES + 1)]
    pull = ScriptedPull(*attempts)
    mgr, rec = make_manager(tmp_path, pull)
    mgr.start("llama3")
    assert mgr.join(5)

    final = only_job(mgr)
    assert final["state"] == "failed"
    assert final["error_category"] == "network"
    assert final["error_type"] == "ConnectionError"
    assert final["attempt"] == mdm.MAX_RETRIES + 1
    assert rec.waits == list(mdm.RETRY_BACKOFF_SECONDS)
    assert rec.waits == [2.0, 8.0, 30.0]
    assert len(pull.calls) == mdm.MAX_RETRIES + 1
    assert rec.events[-1] == "download_error"


def test_retry_resumes_from_ollama_chunks_and_resets_budget_on_progress(tmp_path):
    pull = ScriptedPull(
        [layer("sha256:a", 100, 0), layer("sha256:a", 100, 40), ConnectionError("reset")],
        [layer("sha256:a", 100, 40), layer("sha256:a", 100, 100), *SUCCESS_TAIL],
    )
    mgr, rec = make_manager(tmp_path, pull)
    mgr.start("llama3")
    assert mgr.join(5)
    final = only_job(mgr)
    assert final["state"] == "completed"
    assert final["attempt"] == 2
    assert final["retries_used"] == 0
    assert rec.waits == [2.0]


def test_stream_ending_without_success_is_retried(tmp_path):
    pull = ScriptedPull(
        [layer("sha256:a", 100, 10)],
        [layer("sha256:a", 100, 100), *SUCCESS_TAIL],
    )
    mgr, rec = make_manager(tmp_path, pull)
    mgr.start("llama3")
    assert mgr.join(5)
    assert only_job(mgr)["state"] == "completed"
    assert rec.waits == [2.0]


def test_not_found_error_fails_without_retry(tmp_path):
    pull = ScriptedPull([ResponseError("pull model manifest: file does not exist")])
    mgr, rec = make_manager(tmp_path, pull)
    mgr.start("nope:1b")
    assert mgr.join(5)
    final = only_job(mgr)
    assert final["state"] == "failed"
    assert final["error_category"] == "not_found"
    assert final["error_type"] == "ResponseError"
    assert rec.waits == []
    assert len(pull.calls) == 1


def test_ollama_network_chunk_error_is_retryable(tmp_path):
    pull = ScriptedPull(
        [ResponseError("max retries exceeded: read tcp: connection reset by peer")],
        [layer("sha256:a", 1, 1), *SUCCESS_TAIL],
    )
    mgr, rec = make_manager(tmp_path, pull)
    mgr.start("llama3")
    assert mgr.join(5)
    assert only_job(mgr)["state"] == "completed"
    assert rec.waits == [2.0]


def test_pause_during_backoff_stops_retrying(tmp_path):
    holder = {}

    class PausingRecorder(Recorder):
        def wait(self, stop_event, seconds):
            self.waits.append(seconds)
            holder["mgr"].pause(holder["id"])
            return stop_event.is_set()

    rec = PausingRecorder()
    pull = ScriptedPull([ConnectionError("x")], [ConnectionError("y")])
    mgr, _ = make_manager(tmp_path, pull, rec=rec)
    holder["mgr"] = mgr
    holder["id"] = mgr.start("llama3")["id"]
    assert mgr.join(5)
    assert only_job(mgr)["state"] == "paused"
    assert len(pull.calls) == 1


# ── pause / resume / cancel state machine ────────────────────────────────


def test_pause_closes_stream_and_resume_reports_ollama_percent(tmp_path):
    holder = {}
    snaps = []

    def pause_now():
        holder["mgr"].pause(holder["id"])

    pull = ScriptedPull(
        [layer("sha256:a", 1000, 0), layer("sha256:a", 1000, 400), pause_now, layer("sha256:a", 1000, 500)],
        # Crash-like resume: Ollama's checkpoint is BELOW the pre-pause value.
        [
            layer("sha256:a", 1000, 300),
            lambda: snaps.append(holder["mgr"].get_job(holder["id"])),
            layer("sha256:a", 1000, 1000),
            *SUCCESS_TAIL,
        ],
    )
    mgr, rec = make_manager(tmp_path, pull)
    holder["mgr"] = mgr
    holder["id"] = mgr.start("llama3")["id"]
    assert mgr.join(5)

    paused = only_job(mgr)
    assert paused["state"] == "paused"
    assert paused["completed_bytes"] == 400  # the 500 chunk after pause is never applied
    assert pull.aborted >= 1
    record = json.loads((tmp_path / "model_downloads.json").read_text(encoding="utf-8"))
    assert record["jobs"][0]["state"] == "paused"

    resumed = mgr.resume(holder["id"])
    assert resumed["state"] in {"queued", "downloading"}
    assert mgr.join(5)
    assert snaps[0]["completed_bytes"] == 300
    assert snaps[0]["percent"] == pytest.approx(30.0)
    final = only_job(mgr)
    assert final["state"] == "completed"
    assert len(pull.calls) == 2
    assert rec.events.count("download_start") == 2


def test_cancel_from_downloading_is_terminal(tmp_path):
    holder = {}
    pull = ScriptedPull(
        [layer("sha256:a", 100, 10), lambda: holder["mgr"].cancel(holder["id"]), layer("sha256:a", 100, 20)]
    )
    mgr, _ = make_manager(tmp_path, pull)
    holder["mgr"] = mgr
    holder["id"] = mgr.start("llama3")["id"]
    assert mgr.join(5)
    job = only_job(mgr)
    assert job["state"] == "cancelled"
    assert job["completed_bytes"] == 10
    with pytest.raises(InvalidTransitionError):
        mgr.cancel(holder["id"])
    with pytest.raises(InvalidTransitionError):
        mgr.resume(holder["id"])
    with pytest.raises(InvalidTransitionError):
        mgr.pause(holder["id"])


def test_cancel_from_paused(tmp_path):
    holder = {}
    pull = ScriptedPull([layer("sha256:a", 100, 10), lambda: holder["mgr"].pause(holder["id"])])
    mgr, _ = make_manager(tmp_path, pull)
    holder["mgr"] = mgr
    holder["id"] = mgr.start("llama3")["id"]
    assert mgr.join(5)
    assert mgr.cancel(holder["id"])["state"] == "cancelled"


def test_resume_is_only_valid_from_paused_or_failed(tmp_path):
    pull = ScriptedPull([layer("sha256:a", 1, 1), *SUCCESS_TAIL])
    mgr, _ = make_manager(tmp_path, pull)
    job_id = mgr.start("llama3")["id"]
    assert mgr.join(5)
    with pytest.raises(InvalidTransitionError):
        mgr.resume(job_id)
    with pytest.raises(InvalidTransitionError):
        mgr.pause(job_id)


def test_failed_job_can_be_resumed_manually(tmp_path):
    pull = ScriptedPull(
        [ResponseError("pull model manifest: file does not exist")],
        [layer("sha256:a", 1, 1), *SUCCESS_TAIL],
    )
    mgr, _ = make_manager(tmp_path, pull)
    job_id = mgr.start("llama3")["id"]
    assert mgr.join(5)
    assert only_job(mgr)["state"] == "failed"
    mgr.resume(job_id)
    assert mgr.join(5)
    job = only_job(mgr)
    assert job["state"] == "completed"
    assert job["error_category"] is None


def test_unknown_job_raises_not_found(tmp_path):
    mgr, _ = make_manager(tmp_path, ScriptedPull())
    for action in (mgr.pause, mgr.resume, mgr.cancel):
        with pytest.raises(JobNotFoundError):
            action("does-not-exist")


def test_second_start_while_active_is_rejected(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(5)

    pull = ScriptedPull([layer("sha256:a", 10, 1), block, layer("sha256:a", 10, 10), *SUCCESS_TAIL])
    mgr, _ = make_manager(tmp_path, pull)
    first = mgr.start("llama3")
    assert started.wait(5)
    with pytest.raises(ActiveDownloadError) as exc:
        mgr.start("gemma4:e2b")
    assert exc.value.active_job_id == first["id"]
    release.set()
    assert mgr.join(5)
    assert only_job(mgr)["state"] == "completed"


def test_start_for_a_paused_tag_resumes_that_job(tmp_path):
    holder = {}
    pull = ScriptedPull(
        [layer("sha256:a", 10, 5), lambda: holder["mgr"].pause(holder["id"])],
        [layer("sha256:a", 10, 10), *SUCCESS_TAIL],
    )
    mgr, _ = make_manager(tmp_path, pull)
    holder["mgr"] = mgr
    holder["id"] = mgr.start("llama3")["id"]
    assert mgr.join(5)
    again = mgr.start("llama3")
    assert again["id"] == holder["id"]
    assert mgr.join(5)
    assert only_job(mgr)["state"] == "completed"


def test_start_rejects_invalid_tag(tmp_path):
    mgr, _ = make_manager(tmp_path, ScriptedPull())
    with pytest.raises(ValueError):
        mgr.start("https://evil.example/model")


# ── persistence ──────────────────────────────────────────────────────────


def test_restart_surfaces_non_terminal_job_as_paused_without_autostart(tmp_path):
    record = tmp_path / "model_downloads.json"
    record.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "j1",
                        "tag": "hf.co/bartowski/SmolLM2-135M-Instruct-GGUF:Q4_K_M",
                        "state": "downloading",
                        "total_bytes": 1000,
                        "completed_bytes": 700,
                        "attempt": 1,
                        "created_at": 1.0,
                        "updated_at": 2.0,
                    },
                    {"id": "j0", "tag": "llama3", "state": "completed", "created_at": 0.5, "updated_at": 0.6},
                ],
            }
        ),
        encoding="utf-8",
    )
    pull = ScriptedPull()
    mgr, _ = make_manager(tmp_path, pull)
    jobs = {j["id"]: j for j in mgr.list_jobs()}
    assert jobs["j1"]["state"] == "paused"
    assert jobs["j1"]["interrupted"] is True
    assert jobs["j0"]["state"] == "completed"
    assert mgr.active_job_id() is None
    assert pull.calls == []


def test_shutdown_mid_download_persists_paused_and_next_boot_offers_resume(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(5)

    pull = ScriptedPull([layer("sha256:a", 100, 30), block, layer("sha256:a", 100, 40)])
    mgr, _ = make_manager(tmp_path, pull)
    job_id = mgr.start("llama3")["id"]
    assert started.wait(5)
    mgr.shutdown(timeout=0.05)
    release.set()
    assert mgr.join(5)

    reborn, _ = make_manager(tmp_path, ScriptedPull())
    job = reborn.get_job(job_id)
    assert job["state"] == "paused"
    assert job["interrupted"] is True
    assert job["completed_bytes"] == 30


def test_corrupt_record_fails_open_and_is_rewritten(tmp_path):
    record = tmp_path / "model_downloads.json"
    record.write_text("{not json", encoding="utf-8")
    pull = ScriptedPull([layer("sha256:a", 1, 1), *SUCCESS_TAIL])
    mgr, _ = make_manager(tmp_path, pull)
    assert mgr.list_jobs() == []
    mgr.start("llama3")
    assert mgr.join(5)
    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["version"] == mdm.RECORD_VERSION
    assert data["jobs"][0]["state"] == "completed"


def test_record_with_invalid_entries_keeps_only_valid_jobs(tmp_path):
    record = tmp_path / "model_downloads.json"
    record.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    "garbage",
                    {"id": "bad", "tag": "https://evil/x", "state": "paused"},
                    {"id": "bad2", "tag": "llama3", "state": "exploded"},
                    {"id": "ok", "tag": "llama3", "state": "paused"},
                ],
            }
        ),
        encoding="utf-8",
    )
    mgr, _ = make_manager(tmp_path, ScriptedPull())
    assert [j["id"] for j in mgr.list_jobs()] == ["ok"]


def test_record_keeps_a_bounded_history(tmp_path):
    attempts = [[layer("sha256:a", 1, 1), *SUCCESS_TAIL] for _ in range(mdm.MAX_JOBS_KEPT + 3)]
    pull = ScriptedPull(*attempts)
    mgr, _ = make_manager(tmp_path, pull)
    for i in range(mdm.MAX_JOBS_KEPT + 3):
        mgr.start(f"model{i}")
        assert mgr.join(5)
    assert len(mgr.list_jobs()) == mdm.MAX_JOBS_KEPT
    data = json.loads((tmp_path / "model_downloads.json").read_text(encoding="utf-8"))
    assert len(data["jobs"]) == mdm.MAX_JOBS_KEPT
    assert mgr.list_jobs()[0]["tag"] == f"model{mdm.MAX_JOBS_KEPT + 2}"


# ── privacy: logs and wire shape ─────────────────────────────────────────


def test_logs_and_job_carry_no_error_text(tmp_path, caplog):
    secret = "SECRET-UPSTREAM-TEXT C:/Users/someone/private"  # path-ok
    pull = ScriptedPull([ResponseError(f"pull model manifest: file does not exist {secret}")])
    mgr, _ = make_manager(tmp_path, pull)
    with caplog.at_level(logging.DEBUG, logger="opencohost.model_downloads"):
        mgr.start("llama3")
        assert mgr.join(5)
    assert "SECRET-UPSTREAM-TEXT" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "llama3" in caplog.text
    assert "ResponseError" in caplog.text
    job = only_job(mgr)
    assert "SECRET" not in json.dumps(job)
    assert "SECRET" not in (tmp_path / "model_downloads.json").read_text(encoding="utf-8")


# ── error classification table ───────────────────────────────────────────


@pytest.mark.parametrize(
    "exc, category, retryable",
    [
        (ConnectionError("x"), "network", True),
        (TimeoutError("x"), "network", True),
        (ResponseError("pull model manifest: file does not exist"), "not_found", False),
        (ResponseError("model not found", 404), "not_found", False),
        (ResponseError("max retries exceeded: EOF"), "network", True),
        (ResponseError("internal", 500), "network", True),
        (ResponseError("bad request", 400), "rejected", False),
        (ResponseError("write /models/blobs: no space left on device"), "disk_full", False),
        (ResponseError("digest mismatch"), "pull_error", False),
        (ValueError("x"), "internal", False),
    ],
)
def test_classify_error(exc, category, retryable):
    assert mdm.classify_error(exc) == (category, retryable)


def test_httpx_transport_errors_are_network():
    import httpx

    assert mdm.classify_error(httpx.ReadTimeout("t")) == ("network", True)
    assert mdm.classify_error(httpx.ConnectError("c")) == ("network", True)
