"""Probe de los 3 snippets oficiales de NVIDIA + 1 modelo chico de control.

Los IDs salen de la propia lista /v1/models del usuario (no inventados):
  - google/gemma-4-31b-it
  - deepseek-ai/deepseek-v4-flash-0731
  - moonshotai/kimi-k3
  - mistralai/mistral-7b-instruct-v0.3 (control chico/rapido)

La key va SOLO por variable de entorno, nunca en comandos ni archivos:
    $env:NVIDIA_API_KEY="nvapi-..."
    python C:\\Temp\\nim_probe.py
    Remove-Item Env:\\NVIDIA_API_KEY

Solo stdlib. Solo imprime status/tiempos/extractos, jamas la key.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://integrate.api.nvidia.com/v1"

CASES: list[dict] = [
    {
        "name": "gemma-4-31b-it (snippet 1, no-stream)",
        "payload": {
            "model": "google/gemma-4-31b-it",
            "max_tokens": 64,
            "temperature": 0.5,
            "messages": [{"role": "user", "content": "What is 17 * 23?"}],
        },
    },
    {
        "name": "deepseek-v4-flash (snippet 2, SIN thinking extra = baseline)",
        "payload": {
            "model": "deepseek-ai/deepseek-v4-flash-0731",
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 1,
            "top_p": 0.95,
            "max_tokens": 64,
        },
    },
    {
        "name": "kimi-k3 (snippet 3, texto solo, no-stream)",
        "payload": {
            "model": "moonshotai/kimi-k3",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 64,
            "temperature": 1,
        },
    },
    {
        "name": "control chico: mistral-7b-instruct",
        "payload": {
            "model": "mistralai/mistral-7b-instruct-v0.3",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 32,
        },
    },
]

TIMEOUT = 100.0


def post(payload: dict, api_key: str) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=data,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = json.loads(r.read(2000).decode("utf-8", "replace") or "{}")
        dt = round(time.monotonic() - t0, 2)
        try:
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception:
            content = ""
        return {"ok": True, "status": r.status, "seconds": dt,
                "content_head": str(content)[:160].replace("\n", " ")}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(400).decode("utf-8", "replace")
        except Exception:
            body = ""
        return {"ok": False, "status": e.code, "seconds": round(time.monotonic() - t0, 2),
                "body_head": body[:200].replace("\n", " ")}
    except Exception as e:
        reason = getattr(e, "reason", None)
        detail = (f"{type(reason).__name__}: {reason}" if reason is not None else str(e))[:160]
        return {"ok": False, "status": type(e).__name__, "seconds": round(time.monotonic() - t0, 2),
                "reason": detail}


def main() -> int:
    api_key = (os.environ.get("NVIDIA_API_KEY") or "").strip()
    if not api_key:
        print('[STOP] falta $env:NVIDIA_API_KEY (no la pegues aqui, solo en tu consola).')
        return 2
    print(f"key: ***len={len(api_key)} (no se muestra el valor)")
    results = []
    for case in CASES:
        print(f"\n== {case['name']} ==")
        res = post(case["payload"], api_key)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        results.append((case["name"], res))
    print("\n== resumen ==")
    for name, res in results:
        flag = "OK" if res.get("ok") else "FALLO"
        print(f"  [{flag}] {name}: status={res.get('status')} {res.get('seconds')}s")
    print("\nPegame SOLO este resumen (sin keys).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
