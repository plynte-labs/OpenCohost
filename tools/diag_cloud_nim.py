"""Diagnostico cloud/NIM para VM sin Ollama — lee TUS perfiles, no usa hardcodes.

Uso en la VM (instalada o dev):
    python tools/diag_cloud_nim.py --list
    python tools/diag_cloud_nim.py
    python tools/diag_cloud_nim.py --profile nvidia_nim_2
    python tools/diag_cloud_nim.py --profile nvidia_nim --no-ping

Que hace:
  1. Resuelve rutas reales (instalado: %APPDATA%/OpenCohost/config/*,
     dev: <repo>/config/*) e imprime cual existe.
  2. Carga config/llm_provider.json via load_provider_config() y lista
     TODOS tus perfiles (active_provider marcado con *).
  3. Verifica key SOLO como has_token + longitud, nunca imprime la key.
  4. Hace GET {base_url}/models (igual que readiness) y POST
     {base_url}/chat/completions minimo (igual que generacion), clasifica
     con cloud_llm_client.classify_cloud_error / extract_error_code.
  5. Si el backend esta vivo (127.0.0.1:8765/8770), consulta
     /api/llm/provider y /api/llm/readiness para comparar disco vs runtime.

Seguridad: la api_key solo viaja en el header Authorization hacia tu
base_url. Nunca se loguea, nunca se imprime (solo "***len=N").
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _mask_len(secret: str) -> str:
    s = str(secret or "")
    if not s:
        return "ausente"
    return f"***len={len(s.strip())}"


def resolve_paths() -> dict:
    from opencohost.config import settings, storage

    out = {
        "user_data_dir": str(storage.USER_DATA_DIR),
        "provider_cfg": str(settings.LLM_PROVIDER_CONFIG_FILE),
        "keys_file": str(settings.LLM_KEYS_FILE),
    }
    # Ruta instalada equivalente (modo frozen en Windows) para orientar al usuario de VM.
    try:
        import os

        appdata = os.environ.get("APPDATA", "")
        if appdata:
            out["installed_hint"] = str(
                Path(appdata) / "OpenCohost" / "config" / "llm_provider.json"
            )
    except Exception:
        pass
    return out


def load_profiles() -> dict:
    from opencohost.config.llm_provider import load_provider_config

    cfg = load_provider_config()
    if not isinstance(cfg, dict):
        return {"active_provider": "local", "fallback_mode": "auto", "profiles": {}}
    return cfg


def key_status(profile_id: str) -> tuple[bool, str]:
    """(has_token, masked_len) — nunca devuelve la key."""
    try:
        from opencohost.api import deps
        from opencohost.stream_admin.oauth_store import OAuthStore

        try:
            keys_file = deps.llm_keys_file()
        except Exception:
            from opencohost.config.settings import LLM_KEYS_FILE

            keys_file = LLM_KEYS_FILE
        token = OAuthStore(keys_file).load(profile_id)
        api_key = token.get("api_key") if isinstance(token, dict) else ""
        if isinstance(api_key, str) and api_key.strip():
            return True, _mask_len(api_key)
        return False, "ausente"
    except Exception as exc:
        return False, f"error-leyendo-store: {type(exc).__name__}"


def probe_models(base_url: str, api_key: str, timeout: float = 8.0) -> dict:
    import requests

    url = base_url.rstrip("/") + "/models"
    t0 = time.monotonic()
    try:
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout
        )
        dt = time.monotonic() - t0
        body_head = (resp.text or "")[:300].replace("\n", " ")
        return {
            "ok": resp.status_code < 400,
            "status": resp.status_code,
            "seconds": round(dt, 2),
            "body_head": body_head,
        }
    except requests.exceptions.Timeout:
        return {"ok": False, "status": "timeout", "seconds": round(time.monotonic() - t0, 2)}
    except Exception as exc:
        return {
            "ok": False,
            "status": f"{type(exc).__name__}",
            "seconds": round(time.monotonic() - t0, 2),
        }


def ping_chat(base_url: str, api_key: str, model: str, timeout: float = 80.0) -> dict:
    """POST minimo identico al path de generacion. Clasifica sin exponer la key."""
    from opencohost.core.providers.cloud import cloud_llm_client

    messages = [{"role": "user", "content": "ping"}]
    t0 = time.monotonic()
    try:
        out = cloud_llm_client.send_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            options={},
            timeout=timeout,
        )
        dt = time.monotonic() - t0
        content = ((out.get("message") or {}).get("content") or "")[:200]
        return {
            "ok": True,
            "seconds": round(dt, 2),
            "content_head": content.replace("\n", " "),
        }
    except Exception as exc:
        dt = time.monotonic() - t0
        try:
            failure_class = cloud_llm_client.classify_cloud_error(exc)
        except Exception:
            failure_class = "unknown"
        try:
            code = cloud_llm_client.extract_error_code(exc)
        except Exception:
            code = None
        return {
            "ok": False,
            "seconds": round(dt, 2),
            "status_code": getattr(exc, "status_code", None),
            "failure_class": failure_class,
            "provider_code": code,
            "error": f"{type(exc).__name__}: {str(exc)[:220]}",
        }


def backend_snapshot() -> dict:
    """Lee el backend vivo si existe (8765, fallback 8770). Solo metadata, sin keys."""
    import urllib.request

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
    ap = argparse.ArgumentParser(description="Diagnostico cloud/NIM con TUS perfiles")
    ap.add_argument("--list", action="store_true", help="Solo lista perfiles y keys (sin red)")
    ap.add_argument("--profile", default="", help="Profile id a diagnosticar (default: active_provider)")
    ap.add_argument("--no-ping", action="store_true", help="Omite el POST /chat/completions de pago")
    ap.add_argument("--timeout", type=float, default=80.0, help="Timeout del ping (default 80s)")
    args = ap.parse_args()

    paths = resolve_paths()
    print("== rutas ==")
    for k, v in paths.items():
        exists = ""
        if k in ("provider_cfg", "keys_file"):
            try:
                exists = " (existe)" if Path(str(v)).exists() else " (NO existe)"
            except Exception:
                pass
        print(f"  {k}: {v}{exists}")

    cfg = load_profiles()
    active = str(cfg.get("active_provider") or "local")
    profiles = cfg.get("profiles") or {}
    print(f"\n== perfiles (active_provider={active} fallback_mode={cfg.get('fallback_mode')}) ==")
    if not profiles:
        print("  (sin perfiles cloud — todo local)")
    for pid, prof in profiles.items():
        mark = "*" if pid == active else " "
        has_key, masked = key_status(pid)
        print(
            f"  {mark} {pid}: base_url={prof.get('base_url')} model={prof.get('model')} "
            f"preset={prof.get('preset')} key={masked} has_token={has_key}"
        )

    target = (args.profile or active).strip()
    if args.list:
        print("\n(listo — modo --list, sin llamadas de red)")
        return 0
    if target == "local" or target not in profiles:
        print(f"\n[STOP] target '{target}' no es un perfil cloud.")
        print("Usa: python tools/diag_cloud_nim.py --list  y luego --profile <id>")
        return 2

    prof = profiles[target] or {}
    base_url = str(prof.get("base_url") or "")
    model = str(prof.get("model") or "")
    print(f"\n== target: {target} ==")
    print(f"  base_url={base_url} model={model}")
    if not base_url.startswith(("http://", "https://")):
        print("  [CAUSA] base_url invalido — el PUT lo habria rechazado con 422.")
        return 2
    if not model:
        print("  [CAUSA] model vacio — el PUT lo habria rechazado con 422.")
        return 2

    # Key SOLO para el header, jamas se imprime.
    from opencohost.api import deps
    from opencohost.stream_admin.oauth_store import OAuthStore

    try:
        token = OAuthStore(deps.llm_keys_file()).load(target) or {}
    except Exception:
        from opencohost.config.settings import LLM_KEYS_FILE

        token = OAuthStore(LLM_KEYS_FILE).load(target) or {}
    api_key = str((token or {}).get("api_key") or "")
    if not api_key.strip():
        print("  [CAUSA] sin api_key para este profile id.")
        print("  Fijate si la key quedo bajo OTRO id (mira la lista de arriba):")
        print("  eso pasa cuando se duplica un preset (nvidia_nim_2) y se activa otro.")
        print("  En la UI esto se ve como CLOUD_UNCONFIGURED y el composer bloquea el envio.")
        return 2
    print(f"  key: {_mask_len(api_key)} (no se muestra el valor)")

    print("\n== probe GET /models (igual que readiness) ==")
    print(json.dumps(probe_models(base_url, api_key), indent=2, ensure_ascii=False))

    if not args.no_ping:
        print(f"\n== ping POST /chat/completions model={model} (igual que generacion) ==")
        res = ping_chat(base_url, api_key, model, timeout=args.timeout)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        if not res.get("ok"):
            print("\n-- lectura --")
            print(f"  failure_class={res.get('failure_class')} status={res.get('status_code')} "
                  f"provider_code={res.get('provider_code')}")
            fc = res.get("failure_class")
            if fc == "bad_key":
                print("  -> 401/403: la key es invalida o es de otro proveedor. Re-pegala sin espacios.")
            elif fc == "rate_limited":
                print("  -> 429 con Retry-After: cuota temporal. Espera y reintenta.")
            elif fc == "ambiguous_429":
                print("  -> 429 SIN timing: puede ser cuota agotada. Revisa billing/limites en NVIDIA.")
            else:
                print("  -> transient: red/timeout/5xx o modelo inexistente (404 cuenta como transient).")
                print("     Si el modelo esta deprecado (ej. r1 viejo), NIM devuelve 404 y el motor lo ve igual que un timeout.")
        else:
            print(f"\n  OK en {res.get('seconds')}s — si la app igual dice 'cloud fallo', el problema")
            print("  esta en config activa vs perfil (disco vs runtime), no en NIM.")

    print("\n== backend vivo (disco vs runtime) ==")
    print(json.dumps(backend_snapshot(), indent=2, ensure_ascii=False)[:3000])
    print("\nPegame este bloque completo (las keys ya van enmascaradas) y te digo cual de las 4 causas es.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
