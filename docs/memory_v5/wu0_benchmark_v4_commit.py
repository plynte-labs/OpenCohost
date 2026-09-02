"""Reproducible WU0 microbenchmark of the real v4 history commit seam."""

from __future__ import annotations

import json
import platform
import statistics
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from opencohost.config.settings import HISTORY_MAX_TURNS
from opencohost.core.llm_engine import MemoriaCaptureMixin
from opencohost.core.memory.memoria_store import MemoriaStore


WARMUP_SAMPLES = 25
MEASURED_SAMPLES = 1_000
USER_TEXT = "prefiero cafe colombiano por la manana"
ASSISTANT_TEXT = "Entendido, prefieres cafe colombiano por la manana."


class _CommitHarness(MemoriaCaptureMixin):
    def __init__(self, db_path: Path) -> None:
        self._prefetch_lock = threading.Lock()
        self._prefetch_epoch = 0
        self._prefetched_agenda = None
        self._prefetch_done = threading.Event()
        self._history_lock = threading.Lock()
        self.historial = deque(maxlen=HISTORY_MAX_TURNS * 2)
        self._memory_digest = deque(maxlen=100)
        self._digested_turn_keys: set[str] = set()
        self._memorias_private = False
        self._current_profile_id = "wu0-benchmark-profile"
        self._memoria_store_lock = threading.Lock()
        self._memoria_store = MemoriaStore(db_path)
        self._session_memoria_titles: list[str] = []


def _percentile(sorted_samples: list[float], percentile: float) -> float:
    index = min(len(sorted_samples) - 1, int((len(sorted_samples) - 1) * percentile))
    return sorted_samples[index]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="opencohost-wu0-") as temp_dir:
        harness = _CommitHarness(Path(temp_dir) / "memorias.db")
        commit = harness._commit_history

        for _ in range(WARMUP_SAMPLES):
            commit(USER_TEXT, ASSISTANT_TEXT, source="direct")

        samples_us: list[float] = []
        for _ in range(MEASURED_SAMPLES):
            started_ns = time.perf_counter_ns()
            commit(USER_TEXT, ASSISTANT_TEXT, source="direct")
            samples_us.append((time.perf_counter_ns() - started_ns) / 1_000)

    ordered = sorted(samples_us)
    print(
        json.dumps(
            {
                "method": commit.__qualname__,
                "samples": len(ordered),
                "warmups": WARMUP_SAMPLES,
                "p50_us": statistics.median(ordered),
                "p95_us": _percentile(ordered, 0.95),
                "p99_us": _percentile(ordered, 0.99),
                "max_us": max(ordered),
                "mean_us": statistics.fmean(ordered),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "persistence": "real MemoriaStore temporary DB, conflict-update path",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
