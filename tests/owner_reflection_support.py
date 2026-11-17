"""Shared fakes for the owner-reflection-memory suites (ADR-065).

Not a test module: pytest does not collect it. All memory text is synthetic.
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import opencohost.core.llm_engine as llm_engine  # first: breaks the mixin import cycle
import opencohost.core.engine.llm_engine_reflection as reflection_mod  # noqa: E402
from opencohost.config import settings

PROFILE = "profile-1"
MAIN_THREAD = threading.get_ident


def make_motor(monkeypatch, tmp_path, *, enabled=True, now=1000.0):
    monkeypatch.setattr(settings, "OWNER_REFLECTION_ENABLED", enabled)
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    monkeypatch.setattr(reflection_mod, "_monotonic", lambda: now, raising=False)
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._provider_config = {"active_provider": "local"}
    motor._cloud_fallback_active = False
    motor._current_profile_id = PROFILE
    motor._loaded_model = "m"
    motor.current_model = "m"
    motor._last_known_good_model = "m"
    resident = MagicMock()
    resident.ps.side_effect = lambda: SimpleNamespace(models=[SimpleNamespace(model=motor._loaded_model)])
    motor.ollama.Client.return_value = resident
    motor._pending_model_switch = None
    motor._awaiting_first_success_after_switch = False
    motor._check_capabilities_reasoning = lambda model: False
    motor._promotion_wall_clock = lambda: 100
    motor._last_turn_done_monotonic = now - 60.0
    return motor


def owner_text(i: int) -> str:
    return f"owner statement number {i} about gardening tomatoes"


def kira_text(i: int) -> str:
    return f"kira answer number {i} about compost heaps"


def feed(motor, n=8, *, source="direct", start=1):
    """Offer *n* owner exchanges to the engine's ring (owner gate applies)."""
    ring = motor._owner_reflection_ring()
    for i in range(start, start + n):
        ring.offer(source, False, owner_text(i), kira_text(i))
    return ring


def fact_json(*facts) -> str:
    return json.dumps({"facts": [dict(q=q, a=a, turns=list(t)) for q, a, t in facts]})


def ndjson(reply: str):
    """Ollama streaming NDJSON lines carrying *reply* in two chunks."""
    half = max(1, len(reply) // 2)
    parts = [reply[:half], reply[half:]]
    lines = [json.dumps({"message": {"role": "assistant", "content": p}, "done": False}) for p in parts]
    lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}))
    return lines


class FakeHandle:
    """A fake streaming HTTP response.

    ``release`` gates the lines (set it to let them flow). ``close`` records the
    calling thread and sets ``closed`` but does NOT stop the lines when
    ``keep_yielding`` is True: the worst case where a late result still arrives
    after cancellation and must be discarded.
    """

    def __init__(self, lines=(), *, hold=False, keep_yielding=False):
        self.lines = list(lines)
        self.hold = hold
        self.keep_yielding = keep_yielding
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.close_threads = []
        if not hold:
            self.release.set()

    def iter_lines(self):
        self.started.set()
        self.release.wait(10)
        if self.closed.is_set() and not self.keep_yielding:
            raise OSError("response closed")
        yield from self.lines

    def close(self):
        self.close_threads.append(threading.get_ident())
        self.closed.set()
        if not self.keep_yielding:
            self.release.set()


class StreamSpy:
    def __init__(self, handle_factory):
        self.calls = []
        self._factory = handle_factory
        self.handles = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        handle = self._factory()
        self.handles.append(handle)
        return handle


def install_stream(monkeypatch, motor, reply=None, *, handle=None):
    spy = StreamSpy(lambda: handle if handle is not None else FakeHandle(ndjson(reply or fact_json())))
    monkeypatch.setattr(motor, "_owner_reflection_open_stream", spy, raising=False)
    return spy


def spy_capture(monkeypatch, motor):
    calls = []
    orig = motor._capture_memoria

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return orig(*args, **kwargs)

    monkeypatch.setattr(motor, "_capture_memoria", spy)
    return calls


def run_job(motor, timeout=5.0):
    motor._owner_reflection_tick()
    motor._owner_reflection_join(timeout)


def seed_row(db_path, row_id, title, content, *, status="promoted", judged="2026-01-01",
             updated="2026-01-01", stable_key=None, profile=PROFILE):
    from opencohost.core.memory.memoria_store import MemoriaStore

    MemoriaStore(db_path)  # make sure the schema exists
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, status, "
            "pinned, private, inactive, created_at, updated_at, signature, judged_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 0, 0, 0, '2026-01-01', ?, ?, ?)",
            (row_id, profile, stable_key or f"{profile}|{row_id}", title, content, status,
             updated, content, judged),
        )


def all_rows(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM memorias ORDER BY created_at, id").fetchall()


def owner_reflection_lines(caplog):
    return [r.getMessage() for r in caplog.records if "[OWNER_REFLECTION]" in r.getMessage()]
