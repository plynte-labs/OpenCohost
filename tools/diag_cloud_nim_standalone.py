"""Diagnostico NIM standalone para VM instalada — CERO dependencias.

No importa `opencohost`, no necesita venv ni PYTHONPATH.
Solo stdlib (urllib/json). La key NUNCA se imprime (solo ***len=N).

Guardalo donde quieras, ej: C:\\Temp\\diag_nim.py, y corre:
    python C:\\Temp\\diag_nim.py --list
    python C:\\Temp\\diag_nim.py --profile nvidia_nim
    python C:\\Temp\\diag_nim.py --profile nvidia_nim --no-ping

Funciona con el python que sea (el del sistema sirve).
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def candidates() -> list[Path]:
    out: list[Path] = []
    env_root = (os.environ.get("OPENCOHOST_DATA_ROOT") or "").strip()
    if env_root:
        out.append(Path(os.path.expandvars(os.path.expanduser(env_root))) / "config" / "llm_provider.json")
    # Dev: junto al repo (este archivo esta en <repo>/tools/)
    try:
        repo_cfg = Path(__file__).resolve().parent.parent / "config" / "llm_provider.json"
        out.append(repo_cfg)
    except Exception:
        pass
    try:
        out.append(Path.cwd() / "config" / "llm_provider.json")
    except Exception:
        pass
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        out.append(Path(appdata) / "OpenCohost" / "config" / "llm_provider.json")
    # Layout real visto en tu VM (tree C:\opencohost\resources\runtime):
    # el runtime trae config/ y data/ como hermanos de opencohost/ y python/.
    for base in (
        r"C:\opencohost\resources\runtime",
        r"C:\opencohost",
        r"C:\Program Files\OpenCohost",
        r"C:\ProgramData\OpenCohost",
    ):
        out.append(Path(base) / "config" / "llm_provider.json")
        out.append(Path(base) / "data" / "config" / "llm_provider.json")
    # backend.config.json suele declarar data_root del installer; si existe,
    # se resuelve abajo en main() y se antepone. Aqui solo dejamos el fallback.
    # Deduplicar manteniendo orden
    seen, uniq = set(), []
    for p in out:
        s = str(p).lower()
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return uniq


def load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def key_info(keys_path: Path, profile_id: str) -> tuple[bool, str]:
    data = load_json(keys_path)
    token = data.get(profile_id)
    api_key = token.get("api_key") if isinstance(token, dict) else ""
    if isinstance(api_key, str) and api_key.strip():
        return True, f"***len={len(api_key.strip())}"
    return False, "ausente"


def http_get(url: str, api_key: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"}, method="GET")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(600).decode("utf-8", "replace")
        return {"ok": True, "status": r.status, "seconds": round(time.monotonic() - t0, 2),
                "body_head": body[:300].replace("\n", " ")}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(600).decode("utf-8", "replace")
        except Exception:
            body = ""
        return {"ok": False, "status": e.code, "seconds": round(time.monotonic() - t0, 2),
                "body_head": body[:300].replace("\n", " ")}
    except Exception as e:
        # URLError esconde el motivo en .reason — mostrarlo es lo que distingue
        # DNS vs SSL vs proxy vs firewall. Nunca incluye la key.
        reason = getattr(e, "reason", None)
        return {"ok": False, "status": type(e).__name__, "seconds": round(time.monotonic() - t0, 2),
                "reason": f"{type(reason).__name__}: {reason}"[:300] if reason is not None else str(e)[:300]}


def http_post_chat(base_url: str, api_key: str, model: str, timeout: float) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}]}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                                 method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(4000).decode("utf-8", "replace") or "{}")
        dt = round(time.monotonic() - t0, 2)
        try:
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        except Exception:
            content = ""
        return {"ok": True, "status": r.status, "seconds": dt, "content_head": str(content)[:200].replace("\n", " ")}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(600).decode("utf-8", "replace")
        except Exception:
            body = ""
        try:
            code = (json.loads(body or "{}").get("error") or {}).get("code")
        except Exception:
            code = None
        fc = "bad_key" if e.code in (401, 403) else ("ambiguous_429" if e.code == 429 else "transient")
        return {"ok": False, "status": e.code, "seconds": round(time.monotonic() - t0, 2),
                "failure_class": fc, "provider_code": code, "body_head": body[:300].replace("\n", " ")}
    except Exception as e:
        reason = getattr(e, "reason", None)
        return {"ok": False, "status": type(e).__name__, "seconds": round(time.monotonic() - t0, 2),
                "failure_class": "transient",
                "reason": f"{type(reason).__name__}: {reason}"[:300] if reason is not None else str(e)[:300]}


def backend_snapshot() -> dict:
    for port in (8765, 8770):
        base = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(base + "/api/llm/provider", timeout=3) as r:
                provider = json.loads(r.read().decode("utf-8"))
            try:
                with urllib.request.urlopen(base + "/api/llm/readiness", timeout=5) as r:
                    readiness = json.loads(r.read().decode("utf-8"))
            except Exception:
                readiness = {"_note": "readiness no disponible"}
            return {"base": base, "provider": provider, "readiness": readiness}
        except Exception:
            continue
    return {"base": None, "_note": "backend no responde en 8765/8770 (app cerrada?)"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Diag NIM standalone (sin dependencias)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--profile", default="")
    ap.add_argument("--config", default="", help="Ruta explicita a llm_provider.json")
    ap.add_argument("--no-ping", action="store_true")
    ap.add_argument("--timeout", type=float, default=80.0)
    a = ap.parse_args()

    cfg_path: Path | None = Path(a.config) if a.config else None
    checked: list[str] = []
    if cfg_path is None:
        # 0. backend.config.json del installer (declara data_root real)
        for bc in (
            Path(r"C:\opencohost\resources\runtime\backend.config.json"),
            Path(r"C:\opencohost\resources\backend.config.json"),
            Path(r"C:\opencohost\backend.config.json"),
        ):
            if bc.exists():
                try:
                    raw = json.loads(bc.read_text(encoding="utf-8") or "{}")
                    dr = str(raw.get("data_root") or "").strip()
                    if dr:
                        p = Path(os.path.expandvars(os.path.expanduser(dr))) / "config" / "llm_provider.json"
                        checked.append(f"{p} (via {bc}) {'(existe)' if p.exists() else ''}")
                        if p.exists():
                            cfg_path = p
                            break
                except Exception:
                    pass
            if cfg_path is not None:
                break
    if cfg_path is None:
        for c in candidates():
            checked.append(f"{c} {'(existe)' if c.exists() else ''}")
            if c.exists():
                cfg_path = c
                break
    print("== config buscada ==")
    if a.config:
        print(f"  explicita: {a.config} {'(existe)' if Path(a.config).exists() else '(NO existe)'}")
    else:
        for line in checked:
            print(f"  {line}")
    if cfg_path is None or not cfg_path.exists():
        print("\n[STOP] no encontre llm_provider.json. Pasame --config <ruta> o dime tu OPENCOHOST_DATA_ROOT.")
        return 2
    print(f"  usando: {cfg_path}")

    cfg = load_json(cfg_path)
    active = str(cfg.get("active_provider") or "local")
    profiles = cfg.get("profiles") or {}
    keys_path = cfg_path.parent / "llm_keys.json"
    print(f"\n== perfiles (active={active} fallback={cfg.get('fallback_mode')}) ==")
    print(f"   keys en: {keys_path} {'(existe)' if keys_path.exists() else '(NO existe)'}")
    for pid, prof in profiles.items():
        mark = "*" if pid == active else " "
        has_key, masked = key_info(keys_path, pid)
        print(f"  {mark} {pid}: base={(prof or {}).get('base_url')} model={(prof or {}).get('model')} key={masked}")

    if a.list:
        print("\n(listo — --list sin red)")
        return 0
    target = (a.profile or active).strip()
    if target == "local" or target not in profiles:
        print(f"\n[STOP] target '{target}' no es perfil cloud. Usa --list y luego --profile <id>.")
        return 2
    prof = profiles[target] or {}
    base_url, model = str(prof.get("base_url") or ""), str(prof.get("model") or "")
    print(f"\n== target {target}: base={base_url} model={model} ==")
    keys_data = load_json(keys_path)
    api_key = str(((keys_data.get(target) or {}) if isinstance(keys_data.get(target), dict) else {}).get("api_key") or "")
    if not api_key.strip():
        print("  [CAUSA] sin api_key bajo este id. Mira la lista: la key quedo bajo OTRO id?")
        print("  En UI eso es CLOUD_UNCONFIGURED y el composer bloquea el envio.")
        return 2
    print(f"  key: ***len={len(api_key.strip())}")

    print("\n== GET /models ==")
    print(json.dumps(http_get(base_url.rstrip("/") + "/models", api_key, 8.0), indent=2, ensure_ascii=False))
    if not a.no_ping:
        print(f"\n== POST /chat/completions model={model} ==")
        res = http_post_chat(base_url, api_key, model, a.timeout)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        if not res.get("ok"):
            print(f"\n  failure_class={res.get('failure_class')} status={res.get('status')}")
    print("\n== backend ==")
    print(json.dumps(backend_snapshot(), indent=2, ensure_ascii=False)[:3000])
    print("\nPegame este bloque (keys enmascaradas) y te digo la causa.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
