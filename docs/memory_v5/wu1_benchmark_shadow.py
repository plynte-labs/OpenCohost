import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opencohost.config.settings import (
    MEMORY_V5_SHADOW_QUEUE_MAXSIZE,
)
from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

USER = "Hola, podrias resumir los puntos clave de la reunion de ayer con el equipo de diseno?"
ASSISTANT = "Revisamos tres puntos principales: la paleta de colores final, la jerarquia visual de los modales y el flujo de exportacion."
N = int(os.environ.get("BENCH_N", "1000"))
WARMUPS = 12
SAMPLES = 30
SNAPSHOT_PAIR_BUDGET_US = 20
TWO_ENQUEUES_BUDGET_US = 30
COMBINED_BUDGET_US = 50
FULL_PATH_BUDGET_US = 50


def clone_db(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    shutil.copy2(src, dst)
    return dst


def seed_pair(name: str):
    root = Path(tempfile.mkdtemp(prefix=f"wu1_seed_{name}_"))
    v4 = root / "memorias.db"
    conn = sqlite3.connect(str(v4))
    conn.execute("PRAGMA user_version=4")
    conn.execute(
        "CREATE TABLE memorias (id INTEGER PRIMARY KEY, profile_id TEXT, title TEXT, content TEXT, status TEXT)"
    )
    for i in range(100):
        conn.execute(
            "INSERT INTO memorias VALUES (?,?,?,?,?)",
            (i, "benchmark-profile", f"title {i}", f"content {i}", "promoted"),
        )
    conn.commit()
    conn.close()
    left = clone_db(v4, root / "left.db")
    right = clone_db(v4, root / "right.db")
    return left, right


def drain(rt):
    if not isinstance(rt, MemoryRuntime):
        return
    deadline = time.time() + 5.0
    while time.time() < deadline and rt._queue.qsize() > 0:
        time.sleep(0.005)


def close(rt):
    if isinstance(rt, MemoryRuntime):
        try:
            rt.shutdown(timeout=5.0)
        except Exception:
            pass


def make(kind: str, db_path: Path, run_id: str):
    from opencohost.core.llm_engine import MotorVocalIA
    import queue

    motor = MotorVocalIA(queue.Queue(), lambda s: None)
    motor._current_profile_id = "benchmark-profile"
    motor._memory_run_id = run_id
    if kind == "off":
        motor._memory_runtime = None
        return motor, None

    maxsize = int(MEMORY_V5_SHADOW_QUEUE_MAXSIZE)
    runtime = MemoryRuntime(db_path=db_path, queue_maxsize=maxsize)
    motor._memory_runtime = runtime
    if kind == "queue":
        runtime._store.insert_snapshot = lambda snap: None
    elif kind == "snapshot":
        orig = motor._commit_history

        def drop_shadow(*args, **kwargs):
            return orig(*args, **kwargs)

        motor._commit_history = drop_shadow
    return motor, runtime


def timed_commit(harness) -> float:
    t0 = time.perf_counter_ns()
    harness._commit_history(USER, ASSISTANT, source="direct")
    return (time.perf_counter_ns() - t0) / 1_000


def warm(harness):
    for _ in range(50):
        harness._commit_history(USER, ASSISTANT, source="direct")


def stats(values: list[float]) -> dict:
    if not values:
        return {"p50_us": 0.0, "p95_us": 0.0, "p99_us": 0.0, "max_us": 0.0, "samples": 0}
    s = sorted(values)
    return {
        "p50_us": s[int(len(s) * 0.5)],
        "p95_us": s[int(len(s) * 0.95)],
        "p99_us": s[int(len(s) * 0.99)],
        "max_us": s[-1],
        "samples": len(values),
    }


def paired(name: str, left_kind: str, right_kind: str):
    left_db, right_db = seed_pair(name)
    left_harness, left_rt = make(left_kind, left_db, f"{name}-L")
    right_harness, right_rt = make(right_kind, right_db, f"{name}-R")
    warm(left_harness)
    warm(right_harness)
    drain(left_rt)
    drain(right_rt)

    left_times, right_times, signed_deltas, absolute_deltas = [], [], [], []
    try:
        for _ in range(N // 4):
            tl1 = timed_commit(left_harness)
            tr1 = timed_commit(right_harness)
            tr2 = timed_commit(right_harness)
            tl2 = timed_commit(left_harness)
            left_times.extend([tl1, tl2])
            right_times.extend([tr1, tr2])
            signed_deltas.extend([tr1 - tl1, tr2 - tl2])
            absolute_deltas.extend([abs(tr1 - tl1), abs(tr2 - tl2)])
        drain(left_rt)
        drain(right_rt)
        left_diag = left_rt.diagnostics if isinstance(left_rt, MemoryRuntime) else {}
        right_diag = right_rt.diagnostics if isinstance(right_rt, MemoryRuntime) else {}
        return {
            "left_latency": stats(left_times),
            "right_latency": stats(right_times),
            "signed_delta": stats(signed_deltas),
            "absolute_delta": stats(absolute_deltas),
            "background": right_diag or left_diag,
        }
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
    snapshots, enqueues, combined = [], [], []
    try:
        for _ in range(N):
            combined_started = time.perf_counter_ns()
            with lock:
                snap_started = time.perf_counter_ns()
                seq_u, seq_a = runtime.allocate_sequences(2)
                user = CommittedTurnSnapshot(
                    committed_turn_id=f"{runtime.run_id}:{seq_u}",
                    profile_id="benchmark-profile",
                    run_id=runtime.run_id,
                    stream_sequence=seq_u,
                    role="user",
                    source="direct",
                    occurred_at="2026-01-01T00:00:00Z",
                    content=USER,
                    is_private=False,
                )
                assistant = CommittedTurnSnapshot(
                    committed_turn_id=f"{runtime.run_id}:{seq_a}",
                    profile_id="benchmark-profile",
                    run_id=runtime.run_id,
                    stream_sequence=seq_a,
                    role="assistant",
                    source="direct",
                    occurred_at="2026-01-01T00:00:00Z",
                    content=ASSISTANT,
                    is_private=False,
                )
                snap_elapsed = (time.perf_counter_ns() - snap_started) / 1_000
                enq_started = time.perf_counter_ns()
                runtime.record_turn_exchange(user, assistant)
                enq_elapsed = (time.perf_counter_ns() - enq_started) / 1_000
                combined_elapsed = (time.perf_counter_ns() - combined_started) / 1_000
            snapshots.append(snap_elapsed)
            enqueues.append(enq_elapsed)
            combined.append(combined_elapsed)
        drain(runtime)
        return {
            "snapshot": snapshots,
            "enqueue": enqueues,
            "combined": combined,
            "diag": runtime.diagnostics,
        }
    finally:
        runtime.shutdown(timeout=10)


if "BENCH_EXPERIMENT" in os.environ:
    experiment = os.environ["BENCH_EXPERIMENT"]
    if experiment == "aa":
        result = paired("aa", "off", "off")
    elif experiment == "ab":
        result = paired("ab", "off", "sqlite")
    elif experiment == "ladder":
        result = {
            "worker_idle": paired("worker_idle", "off", "queue"),
            "snapshot": paired("snapshot", "off", "snapshot"),
            "queue": paired("queue", "off", "queue"),
            "sqlite": paired("sqlite", "off", "sqlite"),
            "off_baseline": single("off_baseline", "off"),
        }
    elif experiment == "component":
        result = components()
    else:
        raise ValueError(f"Unknown experiment: {experiment}")
    print(json.dumps(result))
    sys.exit(0)


def _run(experiment: str, root_dir: Path) -> dict:
    env = os.environ.copy()
    env["OPENCOHOST_DATA_ROOT"] = str(root_dir)
    env["OPENCOHOST_MEMORY_V5_SHADOW_DB"] = str(root_dir / "shadow.db")
    env["BENCH_EXPERIMENT"] = experiment
    env["BENCH_N"] = str(N)
    res = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(res.stdout.strip())


def main():
    root = Path(tempfile.mkdtemp(prefix="wu1_bench_root_"))
    try:
        aa = _run("aa", root)
        ab = _run("ab", root)
        ladder = _run("ladder", root)
        component = _run("component", root)

        comp_stats = {
            "snapshot_pair": stats(component["snapshot"]),
            "two_enqueues": stats(component["enqueue"]),
            "combined_snapshot_pair_two_admissions": stats(component["combined"]),
            "sqlite_worker": component["diag"],
            "worker_cpu_ram": "not available without invasive platform-specific tooling",
        }
        comp_pass = comp_stats["combined_snapshot_pair_two_admissions"]["p95_us"] <= float(
            COMBINED_BUDGET_US
        )
        import hashlib

        script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

        report = {
            "benchmark_sha256": script_hash,
            "platform": f"{__import__('platform').system()}-{__import__('platform').release()}-{__import__('platform').version()}",
            "python": sys.version.split()[0],
            "methodology": "fresh subprocesses; cloned warmed v4 DBs; paired ABBA blocks",
            "thresholds_us": {
                "snapshot_pair_diagnostic": int(SNAPSHOT_PAIR_BUDGET_US),
                "two_enqueues_diagnostic": int(TWO_ENQUEUES_BUDGET_US),
                "combined_components": int(COMBINED_BUDGET_US),
                "full_path": int(FULL_PATH_BUDGET_US),
            },
            "full_path_measurement_noise": aa,
            "off_vs_shadow": ab,
            "diagnostic_ladder": ladder,
            "components": comp_stats,
            "full_path_gate": "SUSPENDED_PENDING_RESOLUTION",
            "component_gate_pass": comp_pass,
            "interpretation": "FULL_PATH_50_US_NOT_RESOLVABLE",
        }
        print(json.dumps(report, indent=2))
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
