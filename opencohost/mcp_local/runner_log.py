"""llama-server runner log parser (mcp-local-20261015).

OpenCohost's own ``ollama serve`` writes the llama-server runner output to
``<data root>/logs/ollama_startup_stderr.log``. For hybrid/SWA models Ollama's
``prompt_eval_count`` is the TOTAL prompt size, so the real KV-cache reuse is
only visible here. Only numbers are extracted, through strict regexes; no raw
line is ever stored or returned. The file is large, so only a bounded tail is
streamed line by line.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

RUNNER_LOG_NAME = "ollama_startup_stderr.log"
DEFAULT_TAIL_BYTES = 16 * 1024 * 1024
MIN_PROMPT_TOKENS = 100  # smaller tasks are warmups/probes, never owner turns
_MAX_LINE = 65536

_CONFIG_RE = re.compile(r'^time=\S+ level=\w+ source=\S+ msg="server config"')
_SLOT_RE = re.compile(r"^slot\s+\S+:\s+id\s+\d+ \| task (\d+) \| (.*)$")
_NEW_PROMPT_RE = re.compile(r"^new prompt, .*task\.n_tokens = (\d+)")
_AGAINST_RE = re.compile(r"^Checking checkpoint with \[\d+, \d+\] against (\d+)")
_RESTORED_RE = re.compile(r"^restored context checkpoint \(.*?n_past = (\d+),")
_FORCED_RE = re.compile(r"^forcing full prompt re-processing")
_CREATED_RE = re.compile(r"^created context checkpoint (\d+) of \d+ \(.*?size = ([\d.]+) MiB\)")
_PROMPT_EVAL_RE = re.compile(r"^prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens")
_CACHE_STATE_RE = re.compile(
    r"cache state: (\d+) prompts, ([\d.]+) MiB \(limits: ([\d.]+) MiB"
)


def read_tail_lines(path: Path, max_bytes: int = DEFAULT_TAIL_BYTES) -> Iterator[str]:
    """Stream the last *max_bytes* of *path* line by line (a cut first line is dropped)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - max(0, int(max_bytes)))
            fh.seek(start)
            if start > 0:
                fh.readline(_MAX_LINE)
            while True:
                raw = fh.readline(_MAX_LINE)
                if not raw:
                    return
                yield raw.decode("utf-8", errors="replace")
    except OSError:
        return


def _new_task(run: int, tid: int) -> dict:
    return {
        "run": run, "task": tid, "prompt_tokens": None, "common_prefix": None,
        "reused_tokens": None, "full_reprocess": False, "evaluated_tokens": None,
        "prefill_ms": None, "checkpoints_created": 0, "checkpoint_mib": 0.0,
        "max_checkpoint_index": None,
    }


def parse(lines: Iterable[str]) -> dict:
    """Return ``{"tasks": [...], "cache_state": {run: {...}}}`` (numbers only)."""
    tasks: list[dict] = []
    by_key: dict[tuple[int, int], dict] = {}
    cache_state: dict[int, dict] = {}
    run = 0
    run_active = False
    for line in lines:
        line = line.rstrip("\r\n")
        if _CONFIG_RE.match(line):
            if run_active:
                run += 1
                run_active = False
            continue
        m = _CACHE_STATE_RE.search(line)
        if m and line.lstrip().startswith("srv"):
            cache_state[run] = {"prompts": int(m.group(1)), "mib": float(m.group(2)),
                                "limit_mib": float(m.group(3))}
            run_active = True
            continue
        m = _SLOT_RE.match(line)
        if not m:
            continue
        run_active = True
        tid, rest = int(m.group(1)), m.group(2).strip()
        task = by_key.get((run, tid))
        if task is None:
            task = _new_task(run, tid)
            by_key[(run, tid)] = task
            tasks.append(task)
        g = _NEW_PROMPT_RE.match(rest)
        if g:
            task["prompt_tokens"] = int(g.group(1))
            continue
        g = _AGAINST_RE.match(rest)
        if g:
            task["common_prefix"] = int(g.group(1))
            continue
        g = _RESTORED_RE.match(rest)
        if g:
            task["reused_tokens"] = int(g.group(1))
            continue
        if _FORCED_RE.match(rest):
            task["reused_tokens"] = 0
            task["full_reprocess"] = True
            continue
        g = _CREATED_RE.match(rest)
        if g:
            idx = int(g.group(1))
            task["checkpoints_created"] += 1
            task["checkpoint_mib"] = round(task["checkpoint_mib"] + float(g.group(2)), 3)
            if task["max_checkpoint_index"] is None or idx > task["max_checkpoint_index"]:
                task["max_checkpoint_index"] = idx
            continue
        g = _PROMPT_EVAL_RE.match(rest)
        if g:
            task["prefill_ms"] = int(round(float(g.group(1))))
            task["evaluated_tokens"] = int(g.group(2))
    return {"tasks": tasks, "cache_state": cache_state}


def load(root: Path, max_bytes: int = DEFAULT_TAIL_BYTES) -> dict:
    return parse(read_tail_lines(Path(root) / "logs" / RUNNER_LOG_NAME, max_bytes))


def match_turns(turns: "list[tuple[Any, Optional[int]]]", parsed: dict) -> "dict[Any, dict]":
    """Map turn key -> runner task. *turns* is ``[(key, prompt_tokens)]`` in order.

    A trace turn's ``prompt_eval_count`` equals the runner task's
    ``task.n_tokens``. Runs are tried newest first; inside a run the scan is
    monotonic forward (tasks and turns are both chronological). Tasks under
    ``MIN_PROMPT_TOKENS`` are ignored.
    """
    runs: dict[int, list[dict]] = {}
    for t in parsed.get("tasks", []):
        if t["prompt_tokens"] is not None and t["prompt_tokens"] >= MIN_PROMPT_TOKENS:
            runs.setdefault(t["run"], []).append(t)
    matched: dict[Any, dict] = {}
    for run in sorted(runs, reverse=True):
        cands = runs[run]
        pos = 0
        for key, tokens in turns:
            if key in matched or not isinstance(tokens, int) or tokens < MIN_PROMPT_TOKENS:
                continue
            for i in range(pos, len(cands)):
                if cands[i]["prompt_tokens"] == tokens:
                    matched[key] = cands[i]
                    pos = i + 1
                    break
    return matched


def view(task: dict) -> dict:
    """Public numbers of one runner task (what the tools return)."""
    return {
        "task": task["task"], "prompt_tokens": task["prompt_tokens"],
        "reused_tokens": task["reused_tokens"], "full_reprocess": task["full_reprocess"],
        "evaluated_tokens": task["evaluated_tokens"], "prefill_ms": task["prefill_ms"],
        "common_prefix_tokens": task["common_prefix"],
        "checkpoints_created": task["checkpoints_created"],
        "checkpoint_mib": task["checkpoint_mib"],
        "max_checkpoint_index": task["max_checkpoint_index"],
    }


def memory_summary(parsed: dict, matched: "dict[Any, dict]") -> dict:
    """Runner memory numbers for the run holding the matches (else the newest run)."""
    runs = {t["run"] for t in matched.values()}
    run = max(runs) if runs else max(parsed.get("cache_state", {}) or {0: 0})
    tasks = [t for t in parsed.get("tasks", []) if t["run"] == run]
    idx = [t["max_checkpoint_index"] for t in tasks if t["max_checkpoint_index"] is not None]
    sizes = [t["checkpoint_mib"] / t["checkpoints_created"] for t in tasks if t["checkpoints_created"]]
    return {
        "max_live_checkpoints": max(idx) if idx else None,
        "checkpoint_mib": round(max(sizes), 1) if sizes else None,
        "last_cache_state": parsed.get("cache_state", {}).get(run),
    }
