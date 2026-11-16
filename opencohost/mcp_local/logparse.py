"""Tolerant, metadata-only parsing of OpenCohost session logs.

Log line shape: ``[HH:MM:SS] [LEVEL] [OpenCohost] message``. Anything that does
not match (tracebacks, partial writes) is counted as malformed and skipped;
parsing never raises. Free-text messages are never retained.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import Iterable, Optional

_LINE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] \[[^\]]*\] (.*)$")
_KV = re.compile(r"(\w+)=(\S+)")
_SWITCH = re.compile(r"Modelo cambiado a:\s*(\S+)")
_VAL_MAX = 40
MAX_LIST = 20
# Per-stage fields on [TURN_LATENCY] (core/engine/turn_timing.py); "-" is skipped.
_TURN_STAGE_KEYS = ("recall_ms", "web_ms", "prompt_build_ms", "llm_ms", "ttfa_ms")


def parse_line(line: str) -> Optional[tuple[str, str, str]]:
    m = _LINE.match(line.rstrip("\r\n"))
    return (m.group(1), m.group(2), m.group(3)) if m else None


def kv(msg: str) -> dict[str, str]:
    return {k: v[:_VAL_MAX] for k, v in _KV.findall(msg)}


def parse_sections(raw: str) -> list[dict]:
    """``name:chars#hash,...`` -> ``[{name, chars, hash}]``."""
    out = []
    if not raw or raw == "-":
        return out
    for part in raw.split(","):
        m = re.fullmatch(r"([\w\-]+):(\d+)#([0-9a-f]+)", part)
        if m:
            out.append({"name": m.group(1), "chars": int(m.group(2)), "hash": m.group(3)})
    return out


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def stats(values: list[int]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "p95": p95,
        "max": ordered[-1],
    }


def prompt_trace_turns(lines: Iterable[str]) -> list[dict]:
    """T1 ``[PROMPT_TRACE]`` lines as metadata dicts (log order)."""
    turns = []
    for line in lines:
        parsed = parse_line(line)
        if not parsed or not parsed[2].startswith("[PROMPT_TRACE] "):
            continue
        f = kv(parsed[2])
        if "turn" not in f:
            continue
        sections_raw = re.search(r"sections=(\S+)", parsed[2])
        turns.append(
            {
                "turn": f["turn"],
                "time": parsed[0],
                "source": f.get("source"),
                "model": f.get("model"),
                "sections": parse_sections(sections_raw.group(1) if sections_raw else ""),
                "history_msgs": _int(f.get("history_msgs")),
                "history_chars": _int(f.get("history_chars")),
                "prompt_chars": _int(f.get("prompt_chars")),
                "num_predict": _int(f.get("num_predict")),
                "think": f.get("think"),
                "prompt_eval": _int(f.get("prompt_eval")),
                "eval": _int(f.get("eval")),
                "done": f.get("done"),
                "cache_reused": f.get("cache_reused"),
                "profile": f.get("profile"),
                "error": f.get("error"),
                "layout": f.get("layout"),
                "stable_prefix": f.get("stable_prefix"),
                "history_pairs": _int(f.get("history_pairs")),
                "evicted_pairs": _int(f.get("evicted_pairs")),
                "head_moved": _int(f.get("head_moved")),
            }
        )
    return turns


def summarize(lines: Iterable[str]) -> dict:
    malformed = 0
    total = 0
    levels: Counter = Counter()
    by_source: Counter = Counter()
    by_model: Counter = Counter()
    decisions: dict[str, dict] = {}
    web = {"decision": Counter(), "outcome": Counter(), "engine": Counter(), "reason": Counter()}
    recall_ms: list[int] = []
    recall = {"decision": Counter(), "fallback": Counter(), "inject_skipped": Counter(),
              "intent": Counter(), "best_reason": Counter()}
    recall_count = 0
    guard_lines = 0
    claim = Counter()
    sanitizer = {"verdict": Counter(), "stage": Counter()}
    think_rescue = {"count": 0, "spoken_before_yes": 0}
    done_ms: list[int] = []
    done_outcome: Counter = Counter()
    ttfa_ms: list[int] = []
    latency: dict[str, list[int]] = {}
    stages: dict[str, list[int]] = {}
    stage_paths: Counter = Counter()
    watchdog = rollbacks = 0
    switches: list[dict] = []
    for line in lines:
        total += 1
        parsed = parse_line(line)
        if not parsed:
            malformed += 1
            continue
        time_s, level, msg = parsed
        levels[level] += 1
        if msg.startswith("[PROMPT_TRACE] "):
            f = kv(msg)
            if "turn" in f:
                by_source[f.get("source", "?")] += 1
                by_model[f.get("model", "?")] += 1
        elif msg.startswith("[DECISION] id="):
            f = kv(msg)
            d = decisions.setdefault(
                f.get("id", "?"),
                {"count": 0, "mode": Counter(), "rule": Counter(), "laya": Counter(),
                 "acted_by": Counter(), "status": Counter()},
            )
            d["count"] += 1
            for key, src in (("mode", "mode"), ("rule", "rule"), ("laya", "laya"),
                             ("acted_by", "acted_by"), ("status", "status")):
                d[key][f.get(src, "-")] += 1
        elif msg.startswith("[WEB] decision="):
            f = kv(msg)
            for key in web:
                if key in f:
                    web[key][f[key]] += 1
        elif msg.startswith("memory_recall:"):
            f = kv(msg)
            recall_count += 1
            recall["decision"][f.get("decision", "?")] += 1
            recall["fallback"][f.get("fallback", "?")] += 1
            ms = _int(f.get("ms"))
            if ms is not None:
                recall_ms.append(ms)
            if "intent" in f:  # recall-observability T4 fields (absent on older logs)
                recall["intent"][f["intent"]] += 1
                recall["best_reason"][f.get("best_reason", "-")] += 1
            if "inject=skipped" in msg:
                recall["inject_skipped"][f.get("reason", "?")] += 1
        elif msg.startswith("[GUARDRAILS] policy"):
            guard_lines += 1
        elif msg.startswith("[SEARCH_CLAIM]"):
            claim[kv(msg).get("stage", "?")] += 1
        elif msg.startswith("[CLAUSE_SANITIZER]"):
            f = kv(msg)
            sanitizer["verdict"][f.get("verdict", "?")] += 1
            sanitizer["stage"][f.get("stage", "?")] += 1
        elif msg.startswith("[STREAM_DONE]"):
            f = kv(msg)
            ms = _int(f.get("elapsed_ms"))
            if ms is not None:
                done_ms.append(ms)
            if "outcome" in f:
                done_outcome[f["outcome"]] += 1
        elif msg.startswith("[STREAM_TTFA]"):
            ms = _int(kv(msg).get("first_audio_submit_ms"))
            if ms is not None:
                ttfa_ms.append(ms)
        elif msg.startswith("[THINK_RESCUE]"):
            f = kv(msg)
            think_rescue["count"] += 1
            if f.get("spoken_before") == "yes":
                think_rescue["spoken_before_yes"] += 1
        elif msg.startswith("[TURN_LATENCY]"):
            f = kv(msg)
            ms = _int(f.get("request_to_tts_ms"))
            if ms is not None:
                latency.setdefault(f.get("source", "?"), []).append(ms)
            for key in _TURN_STAGE_KEYS:
                value = _int(f.get(key))
                if value is not None:
                    stages.setdefault(key, []).append(value)
            if "path" in f:
                stage_paths[f["path"]] += 1
        elif msg.startswith("Inference watchdog timeout:"):
            watchdog += 1
        elif "rollback autom" in msg:
            rollbacks += 1
        else:
            m = _SWITCH.search(msg)
            if m and len(switches) < MAX_LIST:
                switches.append({"time": time_s, "model": m.group(1)[:_VAL_MAX]})
    return {
        "lines": total,
        "malformed_lines": malformed,
        "levels": dict(levels),
        "turns": {"total": sum(by_source.values()), "by_source": dict(by_source),
                  "by_model": dict(by_model)},
        "turn_latency_ms": {src: stats(v) for src, v in latency.items()},
        "turn_latency_stages": {key: stats(v) for key, v in stages.items()},
        "turn_latency_path": dict(stage_paths),
        "decisions": {
            did: {k: (dict(v) if isinstance(v, Counter) else v) for k, v in d.items()}
            for did, d in decisions.items()
        },
        "web": {k: dict(v) for k, v in web.items()},
        "memory_recall": {
            "count": recall_count,
            "decision": dict(recall["decision"]),
            "fallback": dict(recall["fallback"]),
            "fallback_timeout": recall["fallback"].get("timeout", 0),
            "ms": stats(recall_ms),
            "inject_skipped": dict(recall["inject_skipped"]),
            "intent": dict(recall["intent"]),
            "best_reason": dict(recall["best_reason"]),
        },
        "guardrails": {"policy_lines": guard_lines},
        "search_claim": {"count": sum(claim.values()), "by_stage": dict(claim)},
        "clause_sanitizer": {k: dict(v) for k, v in sanitizer.items()},
        "stream": {
            "done": {"count": len(done_ms), "elapsed_ms": stats(done_ms),
                     "outcome": dict(done_outcome)},
            "ttfa_ms": stats(ttfa_ms),
        },
        "think_rescue": dict(think_rescue),
        "events": {"watchdog_timeouts": watchdog, "rollbacks": rollbacks,
                   "model_switches": switches},
    }


def _float_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(float(value)) if value is not None else None
    except ValueError:
        return None


def turn_log_stats(lines: Iterable[str], window: int = 20) -> dict[str, dict]:
    """turn -> {cache_reused, prompt_eval, prefill_ms} from one log.

    prefill_ms comes from the ctx_utilization line of the same call: same
    source and prompt_eval_count == prompt_eval, nearest within *window*
    lines (either order). Missing values are None.
    """
    by_turn: dict[str, dict] = {}
    open_t1: list[tuple[int, dict]] = []
    open_ctx: list[tuple[int, dict]] = []
    for idx, line in enumerate(lines):
        parsed = parse_line(line)
        if not parsed:
            continue
        msg = parsed[2]
        if msg.startswith("[PROMPT_TRACE] "):
            f = kv(msg)
            if "turn" not in f:
                continue
            entry = {"cache_reused": f.get("cache_reused"), "prompt_eval": _int(f.get("prompt_eval")),
                     "prefill_ms": None, "_source": f.get("source")}
            by_turn[f["turn"]] = entry
            for cidx, ctx in open_ctx:
                if (not ctx["used"] and ctx["source"] == entry["_source"]
                        and ctx["eval"] == entry["prompt_eval"]):
                    entry["prefill_ms"] = ctx["prefill_ms"]
                    ctx["used"] = True
                    break
            if entry["prefill_ms"] is None:
                open_t1.append((idx, entry))
        elif msg.startswith("ctx_utilization:"):
            f = kv(msg)
            ctx = {"source": f.get("source"), "eval": _int(f.get("prompt_eval_count")),
                   "prefill_ms": _float_int(f.get("prefill_ms")), "used": False}
            for tidx, entry in reversed(open_t1):
                if (entry["prefill_ms"] is None and entry["_source"] == ctx["source"]
                        and entry["prompt_eval"] == ctx["eval"]):
                    entry["prefill_ms"] = ctx["prefill_ms"]
                    ctx["used"] = True
                    break
            if not ctx["used"]:
                open_ctx.append((idx, ctx))
        open_t1 = [(i, e) for i, e in open_t1 if idx - i <= window]
        open_ctx = [(i, c) for i, c in open_ctx if idx - i <= window and not c["used"]]
    for entry in by_turn.values():
        entry.pop("_source", None)
    return by_turn


# --- per-turn lines joined by tid (turn-events-20261112 T4) ------------------

MAX_TID_LINES = 60
TID_TAGS = ("TURN_STATE", "TURN_SPEECH", "TURN_LATENCY", "PROMPT_TRACE")
_TID_TAG = re.compile(r"^\[(" + "|".join(TID_TAGS) + r")\] ")
_FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FIELD_VAL = re.compile(r"^[A-Za-z0-9_.:/#,\-]{1,100}$")
_TRANSITION = re.compile(r"\b([a-z_]+->[a-z_]+)\b")


def turn_lines_by_tid(lines: Iterable[str], tid: str) -> list[dict]:
    """Tagged per-turn log lines carrying ``tid=<tid>`` as ``{time, tag, fields}``.

    Only the four turn tags are read; only ``key=value`` tokens whose key and
    value are short code-like strings are kept, so free text is never returned.
    Capped at ``MAX_TID_LINES``; never raises.
    """
    out: list[dict] = []
    needle = f"tid={tid}"
    for line in lines:
        if needle not in line:
            continue
        parsed = parse_line(line)
        if not parsed:
            continue
        tag = _TID_TAG.match(parsed[2])
        if not tag:
            continue
        fields = {
            k: v for k, v in _KV.findall(parsed[2])
            if k != "tid" and _FIELD_KEY.match(k) and _FIELD_VAL.match(v)
        }
        if kv(parsed[2]).get("tid") != tid:
            continue
        entry = {"time": parsed[0], "tag": tag.group(1), "fields": fields}
        arrow = _TRANSITION.search(parsed[2])
        if arrow:
            entry["transition"] = arrow.group(1)
        out.append(entry)
        if len(out) >= MAX_TID_LINES:
            break
    return out
