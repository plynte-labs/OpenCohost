"""Edge-TTS connection benchmark (edge-tts-prewarm E2).

Measures per-sentence synthesis latency against the live Microsoft Edge-TTS
endpoint for three strategies:

  fresh          asyncio.run() per sentence, no shared state (mirrors the app today)
  shared         one event loop and ONE aiohttp.TCPConnector for every sentence
  fresh_prewarm  like fresh, but one tiny "Hola." synthesis runs first

Run with the opencohost runtime (edge_tts 7.3.1, aiohttp):

    <path-to-opencohost-env>\\python.exe scripts\\bench_edge_tts_connection.py

Raw results go to temp/bench-edge-tts-<timestamp>.json. Audio is written to a
temporary directory that is removed before the script exits.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime

import aiohttp
import edge_tts

# Same voice the app uses by default (opencohost/i18n/active.py LEGACY_EDGE_VOICE).
VOICE = "es-MX-DaliaNeural"
CALL_TIMEOUT_S = 45
ROUNDS = 3
PREWARM_TEXT = "Hola."
SENTENCES = [
    "Hola, bienvenidos al stream de hoy.",
    "Espero que todos estén teniendo una buena tarde.",
    "Hoy vamos a hablar de tecnología y de videojuegos.",
    "Antes de empezar, recuerden seguirnos en el canal.",
    "Tenemos un par de temas interesantes preparados.",
    "Si tienen preguntas, escríbanlas en el chat.",
    "Vamos a empezar con el primer tema de la noche.",
    "Gracias por acompañarnos, ya casi arrancamos.",
]
BASE_ORDER = ["fresh", "shared", "fresh_prewarm"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "temp")


class SharedPoolConnector(aiohttp.TCPConnector):
    """TCPConnector for the shared mode: keeps the pool alive across calls.

    edge_tts builds ``aiohttp.ClientSession(connector=...)`` without
    ``connector_owner``, so aiohttp's default (True) closes this connector
    when each Communicate call's session exits. ``close()`` is therefore a
    no-op until ``allow_close()`` is called; the benchmark closes the pool
    itself in its ``finally`` block.

    The counters are best-effort internals of aiohttp: ``acquisitions`` counts
    ``connect()`` calls (one per request) and ``new_connections`` counts
    ``_create_connection()`` calls (a new TCP/TLS connection). Fewer new
    connections than acquisitions means a pooled connection was reused.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._close_allowed = False
        self.acquisitions = 0
        self.new_connections = 0

    def allow_close(self) -> None:
        self._close_allowed = True

    async def connect(self, *args, **kwargs):
        self.acquisitions += 1
        return await super().connect(*args, **kwargs)

    async def _create_connection(self, *args, **kwargs):
        self.new_connections += 1
        return await super()._create_connection(*args, **kwargs)

    async def close(self, *, abort_ssl: bool = False) -> None:
        if not self._close_allowed:
            return
        await super().close(abort_ssl=abort_ssl)


def _fresh_call(text: str, path: str) -> dict:
    """One synthesis in its own asyncio.run, exactly like the app's productor()."""
    async def _one():
        await asyncio.wait_for(edge_tts.Communicate(text, VOICE).save(path), CALL_TIMEOUT_S)

    start = time.perf_counter()
    try:
        asyncio.run(_one())
        ok, err = True, None
    except Exception as exc:  # network, timeout, service refusal: count and continue
        ok, err = False, type(exc).__name__
    return {"ms": int((time.perf_counter() - start) * 1000), "ok": ok, "err": err}


def run_fresh(tmpdir: str, round_no: int, prewarm: bool) -> dict:
    prewarm_result = None
    if prewarm:
        prewarm_result = _fresh_call(PREWARM_TEXT, os.path.join(tmpdir, f"pw_r{round_no}.mp3"))
    start = time.perf_counter()
    sentences = []
    for idx, text in enumerate(SENTENCES, start=1):
        path = os.path.join(tmpdir, f"r{round_no}_{'pw' if prewarm else 'fr'}_{idx}.mp3")
        result = _fresh_call(text, path)
        result["idx"] = idx
        sentences.append(result)
    return {
        "sentences": sentences,
        "prewarm": prewarm_result,
        "total_ms": int((time.perf_counter() - start) * 1000),
    }


async def _run_shared(tmpdir: str, round_no: int) -> dict:
    conn = SharedPoolConnector(limit=4, keepalive_timeout=30)
    sentences = []
    start = time.perf_counter()
    try:
        for idx, text in enumerate(SENTENCES, start=1):
            path = os.path.join(tmpdir, f"r{round_no}_sh_{idx}.mp3")
            acq0, new0 = conn.acquisitions, conn.new_connections
            t0 = time.perf_counter()
            try:
                await asyncio.wait_for(
                    edge_tts.Communicate(text, VOICE, connector=conn).save(path),
                    CALL_TIMEOUT_S,
                )
                ok, err = True, None
            except Exception as exc:
                ok, err = False, type(exc).__name__
            sentences.append({
                "idx": idx,
                "ms": int((time.perf_counter() - t0) * 1000),
                "ok": ok,
                "err": err,
                "acquisitions": conn.acquisitions - acq0,
                "new_connections": conn.new_connections - new0,
            })
        total_ms = int((time.perf_counter() - start) * 1000)
    finally:
        pooled_before_close = sum(len(v) for v in getattr(conn, "_conns", {}).values())
        conn.allow_close()
        await conn.close()
    connector_closed = conn.closed
    assert connector_closed is True, "shared connector must be closed at the end of the run"
    print(f"  connector closed: {connector_closed}")
    return {
        "sentences": sentences,
        "prewarm": None,
        "total_ms": total_ms,
        "acquisitions": conn.acquisitions,
        "new_connections": conn.new_connections,
        "pooled_before_close": pooled_before_close,
        "connector_closed": connector_closed,
    }


def run_shared(tmpdir: str, round_no: int) -> dict:
    return asyncio.run(_run_shared(tmpdir, round_no))


RUNNERS = {
    "fresh": lambda d, r: run_fresh(d, r, prewarm=False),
    "shared": run_shared,
    "fresh_prewarm": lambda d, r: run_fresh(d, r, prewarm=True),
}


def _median(vals):
    return round(statistics.median(vals)) if vals else None


def _p90(vals):
    """Nearest-rank 90th percentile."""
    if not vals:
        return None
    ordered = sorted(vals)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def summarize(results: list) -> dict:
    summary = {}
    for mode in BASE_ORDER:
        runs = [r for r in results if r["mode"] == mode]
        firsts = [r["sentences"][0]["ms"] for r in runs if r["sentences"][0]["ok"]]
        rest = [s["ms"] for r in runs for s in r["sentences"][1:] if s["ok"]]
        errors = sum(not s["ok"] for r in runs for s in r["sentences"])
        errors += sum(1 for r in runs if r["prewarm"] is not None and not r["prewarm"]["ok"])
        entry = {
            "rounds": len(runs),
            "first_ms_per_round": firsts,
            "first_ms_median": _median(firsts),
            "rest_median_ms": _median(rest),
            "rest_p90_ms": _p90(rest),
            "total_ms_median": _median([r["total_ms"] for r in runs]),
            "errors": errors,
        }
        if mode == "shared":
            entry["acquisitions"] = sum(r["acquisitions"] for r in runs)
            entry["new_tcp_connections"] = sum(r["new_connections"] for r in runs)
            entry["pooled_before_close_per_round"] = [r["pooled_before_close"] for r in runs]
            entry["connector_closed_all_rounds"] = all(r["connector_closed"] for r in runs)
        if mode == "fresh_prewarm":
            entry["prewarm_ms_per_round"] = [r["prewarm"]["ms"] for r in runs if r["prewarm"]]
        summary[mode] = entry
    return summary


def print_table(summary: dict) -> None:
    header = (
        f"{'mode':<14}{'first med':>10}{'first/round':>26}{'s2..N med':>11}"
        f"{'s2..N p90':>11}{'total med':>11}{'errors':>8}"
    )
    print()
    print(header)
    print("-" * len(header))
    for mode in BASE_ORDER:
        e = summary[mode]
        per_round = ",".join(str(v) for v in e["first_ms_per_round"])
        print(
            f"{mode:<14}{str(e['first_ms_median']):>10}{per_round:>26}"
            f"{str(e['rest_median_ms']):>11}{str(e['rest_p90_ms']):>11}"
            f"{str(e['total_ms_median']):>11}{e['errors']:>8}"
        )
    print()
    shared = summary["shared"]
    print(
        f"shared: acquisitions={shared['acquisitions']} "
        f"new_tcp_connections={shared['new_tcp_connections']} "
        f"pooled_before_close={shared['pooled_before_close_per_round']} "
        f"(internal, best-effort)"
    )
    print(f"fresh_prewarm: prewarm_ms per round={summary['fresh_prewarm']['prewarm_ms_per_round']}")


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    round_orders = []
    results = []
    tmpdir = tempfile.mkdtemp(prefix="bench-edge-tts-")
    try:
        for round_no in range(1, ROUNDS + 1):
            order = BASE_ORDER if round_no % 2 == 1 else list(reversed(BASE_ORDER))
            round_orders.append(order)
            for mode in order:
                print(f"round {round_no} mode {mode} ...", flush=True)
                run = RUNNERS[mode](tmpdir, round_no)
                errs = sum(not s["ok"] for s in run["sentences"])
                print(f"  done total_ms={run['total_ms']} errors={errs}", flush=True)
                results.append({"round": round_no, "mode": mode, **run})
    finally:
        shutil.rmtree(tmpdir)
    temp_removed = not os.path.exists(tmpdir)
    print(f"temp audio removed: {temp_removed}")

    summary = summarize(results)
    print_table(summary)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"bench-edge-tts-{stamp}.json")
    payload = {
        "generated_at": stamp,
        "voice": VOICE,
        "call_timeout_s": CALL_TIMEOUT_S,
        "rounds": ROUNDS,
        "round_orders": round_orders,
        "sentences": SENTENCES,
        "prewarm_text": PREWARM_TEXT,
        "python": sys.version.split()[0],
        "edge_tts": edge_tts.__version__,
        "aiohttp": aiohttp.__version__,
        "summary": summary,
        "results": results,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"raw json: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
