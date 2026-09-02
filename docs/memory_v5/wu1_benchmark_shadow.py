"""WU1 benchmark: A/A resolution control before OFF/SHADOW inference."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


SAMPLES = 1_000
WARMUPS = 25


def _pct(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * percentile))]


def _distribution(samples: list[float]) -> dict[str, float | int]:
    return {
        "samples": len(samples),
        "p50_us": statistics.median(samples),
        "p95_us": _pct(samples, 0.95),
        "p99_us": _pct(samples, 0.99),
        "max_us": max(samples),
    }


INNER = textwrap.dedent(
    r'''
import gc, json, os, queue, shutil, statistics, threading, time, uuid
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from opencohost.config.settings import HISTORY_MAX_TURNS
from opencohost.core.llm_engine import MemoriaCaptureMixin
from opencohost.core.memory.memoria_store import MemoriaStore
from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

gc.disable()
N = int(os.environ["BENCH_SAMPLES"])
WARMUPS = int(os.environ["BENCH_WARMUPS"])
ROOT = Path(os.environ["BENCH_ROOT"])
USER = "prefiero cafe colombiano por la manana"
ASSISTANT = "Entendido, prefieres cafe colombiano por la manana."


class Harness(MemoriaCaptureMixin):
    def __init__(self, db_path, runtime=None):
        self._prefetch_lock = threading.Lock()
        self._prefetch_epoch = 0
        self._prefetched_agenda = None
        self._prefetch_done = threading.Event()
        self._history_lock = threading.Lock()
        self.historial = deque(maxlen=HISTORY_MAX_TURNS * 2)
        self._memory_digest = deque(maxlen=100)
        self._digested_turn_keys = set()
        self._memorias_private = False
        self._current_profile_id = "benchmark-profile"
        self._memoria_store_lock = threading.Lock()
        self._memoria_store = MemoriaStore(db_path)
        self._session_memoria_titles = []
        self._memory_runtime = runtime
        self._memory_run_id = runtime.run_id if runtime else None
        self._memory_stream_seq = 0


class SnapshotSink:
    def __init__(self):
        self.run_id = uuid.uuid4().hex

    def record_turn(self, snapshot):
        pass


class QueueSink(SnapshotSink):
    def __init__(self):
        super().__init__()
        self.queue = queue.Queue(maxsize=10_000)

    def record_turn(self, snapshot):
        self.queue.put_nowait(snapshot)


def timed_commit(harness):
    started = time.perf_counter_ns()
    harness._commit_history(USER, ASSISTANT, source="direct")
    return (time.perf_counter_ns() - started) / 1_000


def warm(harness):
    for _ in range(WARMUPS):
        harness._commit_history(USER, ASSISTANT, source="direct")


def drain(runtime):
    if isinstance(runtime, MemoryRuntime):
        runtime._queue.join()


def seed_pair(name):
    directory = ROOT / name
    directory.mkdir(parents=True, exist_ok=True)
    seed = directory / "seed.db"
    warm(Harness(seed))
    left, right = directory / "left.db", directory / "right.db"
    shutil.copy2(seed, left)
    shutil.copy2(seed, right)
    return left, right


def make(kind, v4_path, name):
    runtime = None
    if kind == "snapshot":
        runtime = SnapshotSink()
    elif kind == "queue":
        runtime = QueueSink()
    elif kind in ("worker_idle", "sqlite"):
        runtime = MemoryRuntime(ROOT / f"{name}-shadow.db", queue_maxsize=5_000)
        if kind == "worker_idle":
            runtime._store.insert_snapshot = lambda snapshot: SimpleNamespace(outcome="INSERTED")
    return Harness(v4_path, runtime), runtime


def close(runtime):
    if isinstance(runtime, MemoryRuntime):
        drain(runtime)
        runtime.shutdown(timeout=10)


def paired(name, left_kind, right_kind):
    left_db, right_db = seed_pair(name)
    left, left_rt = make(left_kind, left_db, f"{name}-left")
    right, right_rt = make(right_kind, right_db, f"{name}-right")
    warm(left)
    warm(right)
    drain(left_rt)
    drain(right_rt)
    deltas = []
    left_times, right_times = [], []
    orders = ((left, right), (right, left), (right, left), (left, right))
    try:
        for i in range(N):
            first, second = orders[i % 4]
            first_us = timed_commit(first)
            second_us = timed_commit(second)
            if first is left:
                left_us, right_us = first_us, second_us
            else:
                right_us, left_us = first_us, second_us
            left_times.append(left_us)
            right_times.append(right_us)
            deltas.append(right_us - left_us)
        drain(left_rt)
        drain(right_rt)
        diag = right_rt.diagnostics if isinstance(right_rt, MemoryRuntime) else {}
        return {"signed": deltas, "absolute": [abs(x) for x in deltas],
                "left": left_times, "right": right_times, "diag": diag}
    finally:
        close(left_rt)
        close(right_rt)


def single(name, kind):
    left_db, _ = seed_pair(name)
    harness, runtime = make(kind, left_db, name)
    warm(harness)
    drain(runtime)
    started_cpu = time.process_time_ns()
    times = [timed_commit(harness) for _ in range(N)]
    process_cpu_ms = (time.process_time_ns() - started_cpu) / 1_000_000
    drain(runtime)
    diag = runtime.diagnostics if isinstance(runtime, MemoryRuntime) else {}
    close(runtime)
    return {"times": times, "diag": diag, "process_cpu_ms": process_cpu_ms}


def components():
    runtime = MemoryRuntime(ROOT / "component-shadow.db", queue_maxsize=5_000)
    lock = threading.Lock()
    seq = 100_000
    snapshots, enqueues, combined = [], [], []
    try:
        for _ in range(N):
            combined_started = time.perf_counter_ns()
            started = time.perf_counter_ns()
            with lock:
                seq += 1
                user = CommittedTurnSnapshot(
                    committed_turn_id=f"{runtime.run_id}:{seq}",
                    profile_id="benchmark-profile",
                    run_id=runtime.run_id,
                    stream_sequence=seq,
                    role="user",
                    source="direct",
                    occurred_at="2026-01-01T00:00:00Z",
                    content=USER,
                    is_private=False,
                )
                seq += 1
                assistant = CommittedTurnSnapshot(
                    committed_turn_id=f"{runtime.run_id}:{seq}",
                    profile_id="benchmark-profile",
                    run_id=runtime.run_id,
                    stream_sequence=seq,
                    role="assistant",
                    source="direct",
                    occurred_at="2026-01-01T00:00:00Z",
                    content=ASSISTANT,
                    is_private=False,
                )
            snapshots.append((time.perf_counter_ns() - started) / 1_000)
            started = time.perf_counter_ns()
            runtime.record_turn(user)
            runtime.record_turn(assistant)
            enqueues.append((time.perf_counter_ns() - started) / 1_000)
            combined.append((time.perf_counter_ns() - combined_started) / 1_000)
        drain(runtime)
        return {
            "snapshot": snapshots,
            "enqueue": enqueues,
            "combined": combined,
            "diag": runtime.diagnostics,
        }
    finally:
        runtime.shutdown(timeout=10)


experiment = os.environ["BENCH_EXPERIMENT"]
if experiment == "aa":
    result = paired("aa", "off", "off")
elif experiment == "ab":
    result = paired("ab", "off", "sqlite")
elif experiment == "ladder":
    result = {"off": single("ladder-off", "off")}
    for kind in ("snapshot", "queue", "worker_idle", "sqlite"):
        result[kind] = paired(f"ladder-{kind}", "off", kind)
else:
    result = components()
print(json.dumps(result))
gc.enable()
'''
)


def _run(experiment: str, root: Path) -> dict:
    env = os.environ.copy()
    env.update(
        BENCH_EXPERIMENT=experiment,
        BENCH_ROOT=str(root / experiment),
        BENCH_SAMPLES=str(SAMPLES),
        BENCH_WARMUPS=str(WARMUPS),
    )
    result = subprocess.run(
        [sys.executable, "-c", INNER], capture_output=True, text=True, env=env, timeout=180
    )
    if result.returncode:
        raise RuntimeError(f"{experiment} failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def _pair_report(raw: dict) -> dict:
    return {
        "signed_delta": _distribution(raw["signed"]),
        "absolute_delta": _distribution(raw["absolute"]),
        "left_latency": _distribution(raw["left"]),
        "right_latency": _distribution(raw["right"]),
        "background": raw["diag"],
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wu1-bench-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        aa = _run("aa", root)
        ab = _run("ab", root)
        ladder = _run("ladder", root)
        component = _run("component", root)

    aa_p95 = _pct(aa["absolute"], 0.95)
    ab_p95 = _pct(ab["signed"], 0.95)
    resolvable = aa_p95 < 50
    interpretation = "FULL_PATH_50_US_NOT_RESOLVABLE"
    if resolvable:
        interpretation = "PRODUCT_EFFECT_REQUIRES_ISOLATION" if ab_p95 > 50 else "FULL_PATH_WITHIN_50_US"
    component_pass = _pct(component["combined"], 0.95) <= 50
    ladder_report = {"off_baseline": _distribution(ladder["off"]["times"])}
    ladder_report["off_baseline"]["process_cpu_ms"] = ladder["off"]["process_cpu_ms"]
    for kind in ("snapshot", "queue", "worker_idle", "sqlite"):
        ladder_report[kind] = _pair_report(ladder[kind])
    output = {
        "methodology": "fresh subprocesses; cloned warmed v4 DBs; paired ABBA blocks",
        "full_path_measurement_noise": _pair_report(aa),
        "off_vs_shadow": _pair_report(ab),
        "diagnostic_ladder": ladder_report,
        "components": {
            "combined_snapshot_pair_two_admissions": _distribution(component["combined"]),
            "snapshot_pair": _distribution(component["snapshot"]),
            "two_enqueues": _distribution(component["enqueue"]),
            "sqlite_worker": {
                key: component["diag"].get(key)
                for key in ("bg_p50_ms", "bg_p95_ms", "bg_p99_ms", "queue_depth",
                            "dropped_evidence_total", "control_failures_total", "degraded")
            },
            "worker_cpu_ram": "not available without invasive platform-specific tooling",
        },
        "thresholds_us": {"full_path": 50, "combined_components": 50,
                          "snapshot_pair_diagnostic": 20, "two_enqueues_diagnostic": 30},
        "full_path_gate": "SUSPENDED_PENDING_RESOLUTION",
        "component_gate_pass": component_pass,
        "interpretation": interpretation,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
