"""T3: real KV-cache reuse from the runner log (usage-activity-ledger-20261109)."""
import sqlite3
from datetime import date

import pytest

from opencohost.config import settings
from opencohost.core.usage import ledger, runner_cache

SECRET = "SECRET-RUNNER-LINE-TEXT"

DAY1 = """\
time=2026-10-15T10:00:00.000-06:00 level=INFO source=routes.go:1961 msg="server config" env="map[A:B]"
slot launch_slot_: id  0 | task 1 | processing task, is_child = 0
slot   operator(): id  0 | task 1 | new prompt, n_ctx_slot = 16384, n_keep = 4, task.n_tokens = 18
slot print_timing: id  0 | task 1 | prompt eval time =      50.00 ms /    18 tokens (    1.01 ms per token,   985.74 tokens per second)
slot launch_slot_: id  0 | task 2 | processing task, is_child = 0
slot   operator(): id  0 | task 2 | new prompt, n_ctx_slot = 16384, n_keep = 4, task.n_tokens = 2887
slot   operator(): id  0 | task 2 | restored context checkpoint (pos_min = 1287, pos_max = 1287, n_tokens = 1288, n_past = 1288, size = 50.251 MiB)
slot print_timing: id  0 | task 2 | prompt eval time =    1622.13 ms /  1599 tokens (    1.01 ms per token,   985.74 tokens per second)
"""
DAY2 = """\
time=2026-10-16T09:00:00.000-06:00 level=INFO source=routes.go:1961 msg="server config" env="map[A:B]"
slot launch_slot_: id  0 | task 3 | processing task, is_child = 0
slot   operator(): id  0 | task 3 | new prompt, n_ctx_slot = 16384, n_keep = 4, task.n_tokens = 3500
slot   operator(): id  0 | task 3 | forcing full prompt re-processing due to lack of cache data
slot print_timing: id  0 | task 3 | prompt eval time =    3000.00 ms /  3500 tokens (    0.86 ms per token,  1166.00 tokens per second)
"""
PARTIAL = """\
slot launch_slot_: id  0 | task 4 | processing task, is_child = 0
slot   operator(): id  0 | task 4 | new prompt, n_ctx_slot = 16384, n_keep = 4, task.n_tokens = 400
"""
FINISH = """\
slot print_timing: id  0 | task 4 | prompt eval time =     400.00 ms /   400 tokens (    1.00 ms per token,   100.00 tokens per second)
"""


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    ledger._reset_for_tests()
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "LOG_DIR", str(logs))
    yield logs
    ledger._reset_for_tests()


def _log(logs):
    return logs / "ollama_startup_stderr.log"


def _real():
    return ledger.summary(date_from="2000-01-01", date_to="2100-01-01")["cache"]


def _seed():
    ledger.record_call(model="m", is_local=True, kind="turn", source="direct")


def test_missing_log_means_real_unavailable():
    _seed()
    cache = _real()
    assert cache["real_available"] is False
    assert cache["real_prompt_tokens"] is None and cache["real_reused_tokens"] is None


def test_ingest_counts_tasks_over_threshold_by_date(_env):
    _seed()
    _log(_env).write_text(DAY1 + DAY2, encoding="utf-8")
    cache = _real()
    assert cache["real_available"] is True
    assert cache["real_prompt_tokens"] == 2887 + 3500  # the 18-token warmup is ignored
    assert cache["real_reused_tokens"] == 1288
    con = sqlite3.connect(settings.USAGE_LEDGER_DB)
    rows = con.execute("select date,prompt_tokens,reused_tokens,full_reprocess from cache_real_daily order by date").fetchall()
    con.close()
    assert rows == [("2026-10-15", 2887, 1288, 0), ("2026-10-16", 3500, 0, 1)]


def test_ingest_is_incremental_no_double_count(_env):
    _seed()
    log = _log(_env)
    log.write_text(DAY1, encoding="utf-8")
    assert _real()["real_prompt_tokens"] == 2887
    assert _real()["real_prompt_tokens"] == 2887  # same summary again: not recounted
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(DAY2)
    assert _real()["real_prompt_tokens"] == 2887 + 3500


def test_incomplete_tail_task_waits_for_completion(_env):
    _seed()
    log = _log(_env)
    log.write_text(DAY1 + PARTIAL, encoding="utf-8")
    assert _real()["real_prompt_tokens"] == 2887
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(FINISH)
    assert _real()["real_prompt_tokens"] == 2887 + 400


def test_partial_line_is_not_consumed(_env):
    _seed()
    log = _log(_env)
    log.write_text(DAY1 + "slot   operator(): id  0 | task 9 | new prompt, n_ctx_slot = 1, task.n_t", encoding="utf-8")
    assert _real()["real_prompt_tokens"] == 2887


def test_rotation_resets_offset(_env):
    _seed()
    log = _log(_env)
    log.write_text(DAY1 + DAY2, encoding="utf-8")
    assert _real()["real_prompt_tokens"] == 2887 + 3500
    log.write_text(DAY2, encoding="utf-8")  # smaller than the stored offset
    assert _real()["real_prompt_tokens"] == 2887 + 3500 + 3500


def test_ingest_is_bounded_per_call(_env, monkeypatch):
    _seed()
    log = _log(_env)
    log.write_bytes((DAY1 + DAY2).encode("utf-8"))  # bytes: no CRLF translation
    monkeypatch.setattr(runner_cache, "MAX_INGEST_BYTES", len(DAY1.encode()))
    first = _real()["real_prompt_tokens"]
    assert first == 2887  # only the first slice was consumed
    assert _real()["real_prompt_tokens"] == 2887 + 3500  # the next call continues


def test_date_falls_back_to_today_without_time_lines(_env):
    _seed()
    _log(_env).write_text(DAY1.split("\n", 1)[1] , encoding="utf-8")
    con_rows = None
    _real()
    con = sqlite3.connect(settings.USAGE_LEDGER_DB)
    con_rows = con.execute("select date from cache_real_daily").fetchall()
    con.close()
    assert con_rows == [(date.today().isoformat(),)]


def test_no_raw_lines_are_stored(_env):
    _seed()
    _log(_env).write_text(DAY1 + f"slot x: id 0 | task 7 | {SECRET}\n", encoding="utf-8")
    _real()
    con = sqlite3.connect(settings.USAGE_LEDGER_DB)
    dump = str([r for t in ("usage_daily", "cache_real_daily", "meta") for r in con.execute(f"select * from {t}")])
    con.close()
    assert SECRET not in dump


def test_purge_keeps_offset_so_history_is_not_reingested(_env):
    _seed()
    _log(_env).write_text(DAY1, encoding="utf-8")
    assert _real()["real_prompt_tokens"] == 2887
    ledger.purge()
    _seed()
    assert _real()["real_prompt_tokens"] == 0
