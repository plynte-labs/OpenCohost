"""Real KV-cache reuse for the usage ledger, from the llama-server runner log.

``<data root>/logs/ollama_startup_stderr.log`` is the only place the real prompt
size and reused prefix are visible (Ollama's own counters are not reliable for
hybrid/SWA models). It is ingested incrementally at summary time from a stored
byte offset, in bounded slices, and folded into ``cache_real_daily`` as numbers
only. No raw log line is ever stored.

Rules:
- only complete lines are consumed (a half-written tail line waits);
- a trailing task that has not finished its prompt evaluation waits for the
  next ingest, so a task is never split across two calls;
- tasks under ``MIN_PROMPT_TOKENS`` (warmups, probes) are ignored;
- each task is attributed to the date of the last ``time=YYYY-MM-DD`` line seen
  (carried across calls), falling back to today;
- a file smaller than the stored offset means it rotated: the offset resets;
- a missing file means ``real_available`` stays false.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

from opencohost.config import settings
from opencohost.mcp_local import runner_log

MAX_INGEST_BYTES = 8 * 1024 * 1024

_TIME_RE = re.compile(r"^time=(\d{4}-\d{2}-\d{2})")


def _meta_get(con: sqlite3.Connection, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _hold_back_unfinished_tail(lines: list[str]) -> int:
    """Index where the trailing unfinished task starts, or ``len(lines)``."""
    tasks = runner_log.parse(lines)["tasks"]
    if not tasks:
        return len(lines)
    last = tasks[-1]
    if last["prompt_tokens"] is None or last["evaluated_tokens"] is not None:
        return len(lines)
    cut = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        m = runner_log._SLOT_RE.match(lines[i].rstrip("\r\n"))
        if m:
            if int(m.group(1)) != last["task"]:
                break
            cut = i
    return cut


def ingest(con: sqlite3.Connection, log_path: Optional[Path] = None) -> None:
    """Fold new runner-log tasks into ``cache_real_daily``. Commits on success."""
    path = Path(log_path) if log_path else Path(str(settings.LOG_DIR)) / runner_log.RUNNER_LOG_NAME
    try:
        size = path.stat().st_size
    except OSError:
        _meta_set(con, "runner_log_seen", "0")
        con.commit()
        return
    _meta_set(con, "runner_log_seen", "1")
    offset = int(_meta_get(con, "runner_log_offset") or 0)
    if size < offset:  # rotated or truncated
        offset = 0
    if size == offset:
        _meta_set(con, "runner_log_offset", str(offset))
        con.commit()
        return
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(MAX_INGEST_BYTES)
    end = data.rfind(b"\n") + 1
    if end == 0:
        if len(data) < MAX_INGEST_BYTES:
            con.commit()
            return  # a half-written line: wait for the rest
        end = len(data)  # one absurdly long line: skip it rather than stall
    raw_lines = [part + b"\n" for part in data[:end].split(b"\n")[:-1]] or [data[:end]]
    lines = [raw.decode("utf-8", errors="replace") for raw in raw_lines]
    cut = _hold_back_unfinished_tail(lines)
    consumed = sum(len(raw) for raw in raw_lines[:cut])
    lines = lines[:cut]

    current = _meta_get(con, "runner_log_date") or date.today().isoformat()
    segments: list[tuple[str, list[str]]] = [(current, [])]
    for line in lines:
        m = _TIME_RE.match(line)
        if m and m.group(1) != current:
            current = m.group(1)
            segments.append((current, []))
        segments[-1][1].append(line)

    per_day: dict[str, list[int]] = {}
    for day, seg_lines in segments:
        for task in runner_log.parse(seg_lines)["tasks"]:
            prompt = task["prompt_tokens"]
            if prompt is None or prompt < runner_log.MIN_PROMPT_TOKENS:
                continue
            acc = per_day.setdefault(day, [0, 0, 0])
            acc[0] += prompt
            acc[1] += task["reused_tokens"] or 0
            acc[2] += 1 if task["full_reprocess"] else 0
    for day, (prompt, reused, full) in per_day.items():
        con.execute(
            "INSERT INTO cache_real_daily(date,prompt_tokens,reused_tokens,full_reprocess) "
            "VALUES(?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
            "prompt_tokens=prompt_tokens+excluded.prompt_tokens,"
            "reused_tokens=reused_tokens+excluded.reused_tokens,"
            "full_reprocess=full_reprocess+excluded.full_reprocess",
            (day, prompt, reused, full),
        )
    _meta_set(con, "runner_log_offset", str(offset + consumed))
    _meta_set(con, "runner_log_date", current)
    con.commit()
