"""LLM Readiness & Recovery backend authority (Track llm_readiness_recovery_20260904).

Single authoritative state resolver evaluating whether the active LLM engine
(local Ollama or Cloud API) is operational, authenticated, and ready for inference.
"""

from __future__ import annotations

import logging
import json
import re
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Optional, Union

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
      no GPU detected (VRAM 0 / detection failure): qwen3:1.7b, cpu_only=True
      < 5000 MB: qwen3:1.7b
      5000 - 7499 MB: hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M (6 GB cards)
      >= 7500 MB: hf.co/unsloth/gemma-4-E4B-it-GGUF:IQ4_NL
    """
    gpu_name, total_vram_mb = _detect_gpu_hardware()
    cpu_only = total_vram_mb <= 0
    if total_vram_mb < 5000:
        recommended_tag = "qwen3:1.7b"
    elif total_vram_mb < 7500:
        recommended_tag = "hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M"
    else:
        recommended_tag = "hf.co/unsloth/gemma-4-E4B-it-GGUF:IQ4_NL"

    return {
        "gpu_name": gpu_name,
        "total_vram_mb": total_vram_mb,
        "cpu_only": cpu_only,
        "recommended_tag": recommended_tag,
        "recommended_command": f"ollama run {recommended_tag}",
    }


# ──────────────────────────────────────────────────────────────────────────
# Probes (Ollama & Cloud)
# ──────────────────────────────────────────────────────────────────────────

def _detect_ollama_binary() -> tuple[Optional[str], bool]:
    """Check if ollama executable is present in PATH or a standard install dir.

    Delegates to the shared finder (also used by the eager wake), so the two
    can never disagree about whether Ollama is installed.

    Returns:
        (binary_path, in_path): path string and whether it was found directly in PATH.
    """
    from opencohost.core import ollama_locator

    return ollama_locator.locate_ollama_binary()


# Minimum Ollama build we expect for the default ``gemma4`` family. Below it the
# model may fail to load or answer; readiness only WARNS (never blocks), because
# the floor is a best estimate and the owner may run a patched build.
# ASSUMPTION (unverified against Ollama release notes): 0.20.0. The only builds
# this repo has been measured on are 0.30.11 (owner) and python client 0.6.2.
MIN_OLLAMA_VERSION: tuple[int, ...] = (0, 20, 0)

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")
_old_version_warned = False


def parse_ollama_version(raw: Any) -> Optional[tuple[int, ...]]:
    """"0.30.11" / "v0.9.0-rc2" -> (0, 30, 11) / (0, 9, 0); anything else -> None."""
    if not isinstance(raw, str):
        return None
    match = _VERSION_RE.match(raw.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def ollama_version_supported(raw: Any) -> Optional[bool]:
    """True/False against MIN_OLLAMA_VERSION, None when the version is unknown."""
    parsed = parse_ollama_version(raw)
    if parsed is None:
        return None
    width = max(len(parsed), len(MIN_OLLAMA_VERSION))
    pad = lambda t: tuple(t) + (0,) * (width - len(t))  # noqa: E731
    return pad(parsed) >= pad(MIN_OLLAMA_VERSION)


def _fetch_ollama_version(timeout: float = 1.5) -> Optional[str]:
    """GET /api/version on the main daemon; None on any failure."""
    try:
        from opencohost.core.vision.model_inventory import main_ollama_base_url

        url = main_ollama_base_url().rstrip("/") + "/api/version"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        version = data.get("version") if isinstance(data, dict) else None
        return str(version) if version else None
    except Exception:
        return None


def _ping_ollama(timeout: float = 1.5) -> tuple[bool, Optional[str], list[str]]:
    """Probe local Ollama daemon for reachability and installed model tags.

    Returns:
        (reachable, version, installed_models). ``version`` is the real build
        from GET /api/version, or None when the daemon did not report one.
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

        return (True, _fetch_ollama_version(timeout), sorted(tags))
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
    profile_id: Union[str, dict[str, Any]] = "cloud",
    cloud_profile: Optional[dict[str, Any]] = None,
    timeout: float = 3.0,
) -> tuple[bool, Optional[str]]:
    """Bounded test probe verifying cloud credentials and network connectivity."""
    try:
        import requests
        from opencohost.api import deps
        from opencohost.stream_admin.oauth_store import OAuthStore

        if isinstance(profile_id, dict):
            cloud_profile = profile_id
            profile_id = "cloud"

        store = OAuthStore(deps.llm_keys_file())
        token = store.load(profile_id)
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

        version_supported = ollama_version_supported(version) if reachable else None
        global _old_version_warned
        if version_supported is False and not _old_version_warned:
            _old_version_warned = True
            logger.warning(
                "Ollama %s is older than the minimum %s expected for gemma4; "
                "update Ollama if the model fails to load.",
                version,
                ".".join(str(n) for n in MIN_OLLAMA_VERSION),
            )

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
                "min_version": ".".join(str(n) for n in MIN_OLLAMA_VERSION),
                "version_supported": version_supported,
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
    cloud_profile = cloud_profiles.get(active_provider) or {}
    provider_id = cloud_profile.get("provider_id") or cloud_profile.get("preset") or active_provider
    selected_model = cloud_profile.get("model") or "meta/llama-3.1-70b-instruct"

    key_configured = _check_cloud_key_configured(active_provider)
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

    success, err_reason = _probe_cloud_status(active_provider, cloud_profile)
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
