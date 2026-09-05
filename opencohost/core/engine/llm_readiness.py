"""LLM Readiness & Recovery backend authority (Track llm_readiness_recovery_20260904).

Single authoritative state resolver evaluating whether the active LLM engine
(local Ollama or Cloud API) is operational, authenticated, and ready for inference.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import asdict, dataclass
from typing import Any, Optional

from opencohost.config.settings import _canonical_model_tag

logger = logging.getLogger("opencohost.core.engine.llm_readiness")

# ──────────────────────────────────────────────────────────────────────────
# Discrete States
# ──────────────────────────────────────────────────────────────────────────

STATE_CHECKING = "CHECKING"
STATE_UNCONFIGURED = "UNCONFIGURED"
STATE_LOCAL_OLLAMA_MISSING = "LOCAL_OLLAMA_MISSING"
STATE_LOCAL_OLLAMA_OFFLINE = "LOCAL_OLLAMA_OFFLINE"
STATE_LOCAL_OLLAMA_STARTING = "LOCAL_OLLAMA_STARTING"
STATE_LOCAL_NO_MODELS = "LOCAL_NO_MODELS"
STATE_LOCAL_MODEL_MISSING = "LOCAL_MODEL_MISSING"
STATE_LOCAL_READY = "LOCAL_READY"
STATE_CLOUD_UNCONFIGURED = "CLOUD_UNCONFIGURED"
STATE_CLOUD_VALIDATING = "CLOUD_VALIDATING"
STATE_CLOUD_INVALID_CREDENTIALS = "CLOUD_INVALID_CREDENTIALS"
STATE_CLOUD_UNREACHABLE = "CLOUD_UNREACHABLE"
STATE_CLOUD_READY = "CLOUD_READY"


@dataclass
class LlmReadinessResult:
    state: str
    provider: str
    can_chat: bool
    selected_model: Optional[str]
    ollama: dict[str, Any]
    cloud: dict[str, Any]
    hardware: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────────
# Hardware / VRAM Guideline
# ──────────────────────────────────────────────────────────────────────────

def _detect_gpu_hardware() -> tuple[Optional[str], int]:
    """Poll GPU name and total VRAM (in MB) via pynvml, failing gracefully."""
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        raw_name = pynvml.nvmlDeviceGetName(handle)
        name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        total_mb = int(info.total / (1024 * 1024))
        return (name, total_mb)
    except Exception:
        return (None, 0)


def get_hardware_guideline() -> dict[str, Any]:
    """Return hardware-aware model recommendations based on total VRAM.
    
    Recommendations guide the user toward fast, reliable startup without
    prohibiting manual overrides:
      < 5000 MB: gemma4:e2b
      5000 - 11000 MB: gemma4:e4b (ideal for 6GB-8GB GPUs like RTX 5060/4060)
      >= 11000 MB: gemma4:12b
    """
    gpu_name, total_vram_mb = _detect_gpu_hardware()
    if total_vram_mb < 5000:
        recommended_tag = "gemma4:e2b"
    elif total_vram_mb < 11000:
        recommended_tag = "gemma4:e4b"
    else:
        recommended_tag = "gemma4:12b"

    return {
        "gpu_name": gpu_name,
        "total_vram_mb": total_vram_mb,
        "recommended_tag": recommended_tag,
        "recommended_command": f"ollama run {recommended_tag}",
    }


# ──────────────────────────────────────────────────────────────────────────
# Probes (Ollama & Cloud)
# ──────────────────────────────────────────────────────────────────────────

def _detect_ollama_binary() -> tuple[Optional[str], bool]:
    """Check if ollama executable is present in PATH or standard installation directory.

    Returns:
        (binary_path, in_path): path string and whether it was found directly in PATH.
    """
    which_path = shutil.which("ollama")
    if which_path:
        return (which_path, True)

    # Check default Windows installation directory
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        default_win_path = os.path.join(local_app_data, "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(default_win_path):
            return (default_win_path, False)

    # Check common UNIX fallbacks
    for fallback in ("/usr/local/bin/ollama", "/usr/bin/ollama"):
        if os.path.isfile(fallback):
            return (fallback, False)

    return (None, False)


def _ping_ollama(timeout: float = 1.5) -> tuple[bool, Optional[str], list[str]]:
    """Probe local Ollama daemon for reachability and installed model tags.
    
    Returns:
        (reachable, version, installed_models)
    """
    try:
        import ollama

        client = ollama.Client(timeout=timeout)
        response = client.list()
        models = getattr(response, "models", None)
        if models is None and isinstance(response, dict):
            models = response.get("models", [])

        tags = set()
        for model in models or []:
            if isinstance(model, dict):
                raw_tag = model.get("model") or model.get("name") or ""
            else:
                raw_tag = getattr(model, "model", None) or getattr(model, "name", None) or ""
            tag = _canonical_model_tag(raw_tag)
            if tag:
                tags.add(tag)

        version = getattr(response, "version", None) or "available"
        return (True, str(version), sorted(tags))
    except Exception:
        return (False, None, [])


def _check_cloud_key_configured(profile_id: str = "cloud") -> bool:
    """Check if an API key is present in OAuthStore for the given cloud profile."""
    try:
        from opencohost.api import deps
        from opencohost.stream_admin.oauth_store import OAuthStore

        store = OAuthStore(deps.llm_keys_file())
        token = store.load(profile_id)
        if isinstance(token, dict) and str(token.get("api_key") or "").strip():
            return True
        return False
    except Exception:
        return False


def _probe_cloud_status(
    cloud_profile: Optional[dict[str, Any]] = None,
    timeout: float = 3.0,
) -> tuple[bool, Optional[str]]:
    """Bounded test probe verifying cloud credentials and network connectivity."""
    try:
        import requests
        from opencohost.api import deps
        from opencohost.stream_admin.oauth_store import OAuthStore

        store = OAuthStore(deps.llm_keys_file())
        token = store.load("cloud")
        api_key = token.get("api_key") if isinstance(token, dict) else ""
        if not api_key:
            return (False, "invalid_credentials")

        base_url = (cloud_profile or {}).get("base_url") or "https://integrate.api.nvidia.com/v1"
        # Bounded probe against models endpoint
        probe_url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.get(probe_url, headers=headers, timeout=timeout)
        if resp.status_code in (401, 403):
            return (False, "invalid_credentials")
        if resp.status_code >= 500 or resp.status_code == 404:
            return (False, "network_error")
        return (True, None)
    except requests.exceptions.Timeout:
        return (False, "network_error")
    except Exception:
        return (False, "network_error")


# ──────────────────────────────────────────────────────────────────────────
# Authoritative Resolver
# ──────────────────────────────────────────────────────────────────────────

def resolve_llm_readiness(host: Any) -> LlmReadinessResult:
    """Resolve authoritative readiness state for the active LLM engine."""
    motor = getattr(host, "motor", None)
    provider_cfg: dict[str, Any] = {}
    if motor is not None and hasattr(motor, "_provider_config"):
        provider_cfg = getattr(motor, "_provider_config", {})
    if not provider_cfg:
        try:
            from opencohost.config.llm_provider import load_provider_config

            provider_cfg = load_provider_config()
        except Exception:
            provider_cfg = {"active_provider": "local"}

    active_provider = str(provider_cfg.get("active_provider") or "local")
    hardware = get_hardware_guideline()

    # 1. Local (Ollama)
    if active_provider == "local":
        if getattr(motor, "_simulated_readiness", False):
            cur = getattr(motor, "current_model", "qwen3:8b")
            reachable, version, installed_models = (True, "fake-0.1", [cur] if cur else [])
        else:
            reachable, version, installed_models = _ping_ollama()
        selected_model = (
            getattr(motor, "current_model", None)
            or getattr(host, "_display_model", lambda h: None)(host)
            or hardware.get("recommended_tag")
            or "gemma4:e4b"
        )
        canonical_selected = _canonical_model_tag(selected_model)

        binary_path, in_path = _detect_ollama_binary()

        if not reachable:
            state = STATE_LOCAL_OLLAMA_OFFLINE if binary_path else STATE_LOCAL_OLLAMA_MISSING
            can_chat = False
        elif len(installed_models) == 0:
            state = STATE_LOCAL_NO_MODELS
            can_chat = False
        elif (
            canonical_selected not in installed_models
            and selected_model not in installed_models
        ):
            state = STATE_LOCAL_MODEL_MISSING
            can_chat = False
        else:
            state = STATE_LOCAL_READY
            can_chat = True

        return LlmReadinessResult(
            state=state,
            provider="local",
            can_chat=can_chat,
            selected_model=selected_model,
            ollama={
                "reachable": reachable,
                "version": version,
                "installed_models": installed_models,
                "binary_found": bool(binary_path),
                "binary_path": binary_path,
                "in_path": in_path,
            },
            cloud={
                "configured": False,
                "provider_id": "nvidia",
                "reachable": None,
            },
            hardware=hardware,
        )

    # 2. Cloud Provider
    cloud_profiles = provider_cfg.get("profiles", {})
    cloud_profile = cloud_profiles.get("cloud") or cloud_profiles.get(active_provider) or {}
    provider_id = cloud_profile.get("provider_id", "nvidia")
    selected_model = cloud_profile.get("model") or "meta/llama-3.1-70b-instruct"

    key_configured = _check_cloud_key_configured("cloud")
    if not key_configured:
        return LlmReadinessResult(
            state=STATE_CLOUD_UNCONFIGURED,
            provider="cloud",
            can_chat=False,
            selected_model=selected_model,
            ollama={"reachable": False, "version": None, "installed_models": []},
            cloud={
                "configured": False,
                "provider_id": provider_id,
                "reachable": None,
            },
            hardware=hardware,
        )

    success, err_reason = _probe_cloud_status(cloud_profile)
    if success:
        state = STATE_CLOUD_READY
        can_chat = True
        cloud_reachable = True
    elif err_reason == "invalid_credentials":
        state = STATE_CLOUD_INVALID_CREDENTIALS
        can_chat = False
        cloud_reachable = False
    else:
        state = STATE_CLOUD_UNREACHABLE
        can_chat = False
        cloud_reachable = False

    return LlmReadinessResult(
        state=state,
        provider="cloud",
        can_chat=can_chat,
        selected_model=selected_model,
        ollama={"reachable": False, "version": None, "installed_models": []},
        cloud={
            "configured": True,
            "provider_id": provider_id,
            "reachable": cloud_reachable,
        },
        hardware=hardware,
    )
