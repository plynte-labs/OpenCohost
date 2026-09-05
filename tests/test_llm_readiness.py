"""Unit tests for LLM Readiness & Recovery backend authority (Track llm_readiness_recovery_20260904).

Covers:
1. State resolver transitions for Local (Ollama) and Cloud providers.
2. Hardware VRAM detection and guideline recommendations.
3. GET /api/llm/readiness endpoint schema and behavior.
4. Fail-fast protection on POST /api/chat/message when unready.
5. Catalog vs Installed separation in opencohost.config.settings.
"""

from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from opencohost.config.settings import MODELS_CATALOG, is_runtime_model_available
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app(host_factory=FakeHost):
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# 1. State Resolver Units (Local Ollama)
# ──────────────────────────────────────────────────────────────────────────

def test_readiness_local_ollama_missing(monkeypatch):
    """When provider is local, Ollama is unreachable and binary is NOT found -> LOCAL_OLLAMA_MISSING."""
    from opencohost.core.engine.llm_readiness import resolve_llm_readiness

    host = FakeHost()
    host.motor._provider_config = {"active_provider": "local"}
    host.motor.current_model = "gemma4:e4b"

    # Mock Ollama ping failure and binary missing
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._ping_ollama",
        lambda: (False, None, [])
    )
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._detect_ollama_binary",
        lambda: (None, False)
    )

    res = resolve_llm_readiness(host)
    assert res.state == "LOCAL_OLLAMA_MISSING"
    assert res.can_chat is False
    assert res.provider == "local"
    assert res.ollama["reachable"] is False
    assert res.ollama["binary_found"] is False
    assert res.ollama["installed_models"] == []


def test_readiness_local_ollama_offline(monkeypatch):
    """When Ollama binary exists on disk or PATH but daemon is not reachable -> LOCAL_OLLAMA_OFFLINE."""
    from opencohost.core.engine.llm_readiness import resolve_llm_readiness

    host = FakeHost()
    host.motor._provider_config = {"active_provider": "local"}
    host.motor.current_model = "gemma4:e4b"

    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._ping_ollama",
        lambda: (False, None, [])
    )
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._detect_ollama_binary",
        lambda: ("C:\\Users\\tavo_\\AppData\\Local\\Programs\\Ollama\\ollama.exe", False)
    )

    res = resolve_llm_readiness(host)
    assert res.state == "LOCAL_OLLAMA_OFFLINE"
    assert res.can_chat is False
    assert res.provider == "local"
    assert res.ollama["reachable"] is False
    assert res.ollama["binary_found"] is True
    assert res.ollama["in_path"] is False
    assert res.ollama["binary_path"] == "C:\\Users\\tavo_\\AppData\\Local\\Programs\\Ollama\\ollama.exe"



def test_readiness_local_no_models(monkeypatch):
    """When Ollama is reachable but 0 models exist, state must be LOCAL_NO_MODELS and can_chat=False."""
    from opencohost.core.engine.llm_readiness import resolve_llm_readiness

    host = FakeHost()
    host.motor._provider_config = {"active_provider": "local"}
    host.motor.current_model = "gemma4:e4b"

    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._ping_ollama",
        lambda: (True, "0.5.1", [])
    )

    res = resolve_llm_readiness(host)
    assert res.state == "LOCAL_NO_MODELS"
    assert res.can_chat is False
    assert res.ollama["reachable"] is True
    assert res.ollama["installed_models"] == []


def test_readiness_local_model_missing(monkeypatch):
    """When Ollama is reachable, models exist, but selected model is not installed -> LOCAL_MODEL_MISSING."""
    from opencohost.core.engine.llm_readiness import resolve_llm_readiness

    host = FakeHost()
    host.motor._provider_config = {"active_provider": "local"}
    host.motor.current_model = "gemma4:e4b"

    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._ping_ollama",
        lambda: (True, "0.5.1", ["qwen3:4b"])
    )

    res = resolve_llm_readiness(host)
    assert res.state == "LOCAL_MODEL_MISSING"
    assert res.can_chat is False
    assert res.selected_model == "gemma4:e4b"
    assert "qwen3:4b" in res.ollama["installed_models"]


def test_readiness_local_ready(monkeypatch):
    """When Ollama is reachable and selected model is in installed models -> LOCAL_READY and can_chat=True."""
    from opencohost.core.engine.llm_readiness import resolve_llm_readiness

    host = FakeHost()
    host.motor._provider_config = {"active_provider": "local"}
    host.motor.current_model = "gemma4:e4b"

    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._ping_ollama",
        lambda: (True, "0.5.1", ["gemma4:e4b", "qwen3:4b"])
    )

    res = resolve_llm_readiness(host)
    assert res.state == "LOCAL_READY"
    assert res.can_chat is True
    assert res.selected_model == "gemma4:e4b"


@pytest.fixture(autouse=True)
def _disable_fake_motor_simulated_readiness(monkeypatch):
    """Ensure FakeMotor doesn't bypass ping_ollama in readiness unit tests."""
    from tests.test_api_phase1 import FakeMotor

    monkeypatch.setattr(FakeMotor, "_simulated_readiness", False, raising=False)


# ──────────────────────────────────────────────────────────────────────────
# 2. State Resolver Units (Cloud Provider)
# ──────────────────────────────────────────────────────────────────────────

def test_readiness_cloud_unconfigured(monkeypatch):
    """When cloud provider is active but no API key configured -> CLOUD_UNCONFIGURED."""
    from opencohost.core.engine.llm_readiness import resolve_llm_readiness

    host = FakeHost()
    host.motor._provider_config = {
        "active_provider": "cloud",
        "profiles": {"cloud": {"provider_id": "nvidia", "model": "meta/llama-3.1-70b-instruct"}}
    }
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._check_cloud_key_configured",
        lambda *args, **kwargs: False
    )

    res = resolve_llm_readiness(host)
    assert res.state == "CLOUD_UNCONFIGURED"
    assert res.can_chat is False
    assert res.cloud["configured"] is False


def test_readiness_cloud_invalid_credentials(monkeypatch):
    """When cloud key is invalid -> CLOUD_INVALID_CREDENTIALS."""
    from opencohost.core.engine.llm_readiness import resolve_llm_readiness

    host = FakeHost()
    host.motor._provider_config = {
        "active_provider": "cloud",
        "profiles": {"cloud": {"provider_id": "nvidia", "model": "meta/llama-3.1-70b-instruct"}}
    }
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._check_cloud_key_configured",
        lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._probe_cloud_status",
        lambda *args, **kwargs: (False, "invalid_credentials")
    )

    res = resolve_llm_readiness(host)
    assert res.state == "CLOUD_INVALID_CREDENTIALS"
    assert res.can_chat is False


def test_readiness_cloud_unreachable(monkeypatch):
    """When cloud endpoint times out or is unreachable -> CLOUD_UNREACHABLE."""
    from opencohost.core.engine.llm_readiness import resolve_llm_readiness

    host = FakeHost()
    host.motor._provider_config = {
        "active_provider": "cloud",
        "profiles": {"cloud": {"provider_id": "nvidia", "model": "meta/llama-3.1-70b-instruct"}}
    }
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._check_cloud_key_configured",
        lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._probe_cloud_status",
        lambda *args, **kwargs: (False, "network_error")
    )

    res = resolve_llm_readiness(host)
    assert res.state == "CLOUD_UNREACHABLE"
    assert res.can_chat is False


def test_readiness_cloud_ready(monkeypatch):
    """When cloud credentials and endpoint are verified -> CLOUD_READY and can_chat=True."""
    from opencohost.core.engine.llm_readiness import resolve_llm_readiness

    host = FakeHost()
    host.motor._provider_config = {
        "active_provider": "cloud",
        "profiles": {"cloud": {"provider_id": "nvidia", "model": "meta/llama-3.1-70b-instruct"}}
    }
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._check_cloud_key_configured",
        lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._probe_cloud_status",
        lambda *args, **kwargs: (True, None)
    )

    res = resolve_llm_readiness(host)
    assert res.state == "CLOUD_READY"
    assert res.can_chat is True


# ──────────────────────────────────────────────────────────────────────────
# 3. Hardware / VRAM Guideline Calculation
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "vram_mb,expected_tag,expected_cmd",
    [
        (2048, "gemma4:e2b", "ollama run gemma4:e2b"),
        (4096, "gemma4:e2b", "ollama run gemma4:e2b"),
        (8192, "gemma4:e4b", "ollama run gemma4:e4b"),
        (12288, "gemma4:12b", "ollama run gemma4:12b"),
    ],
)
def test_vram_hardware_guidelines(vram_mb, expected_tag, expected_cmd, monkeypatch):
    """VRAM guidelines must recommend appropriate model based on GPU memory without prohibiting."""
    from opencohost.core.engine.llm_readiness import get_hardware_guideline

    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness._detect_gpu_hardware",
        lambda: ("NVIDIA GeForce GPU", vram_mb)
    )

    hw = get_hardware_guideline()
    assert hw["total_vram_mb"] == vram_mb
    assert hw["recommended_tag"] == expected_tag
    assert hw["recommended_command"] == expected_cmd


# ──────────────────────────────────────────────────────────────────────────
# 4. GET /api/llm/readiness Endpoint Test
# ──────────────────────────────────────────────────────────────────────────

def test_get_llm_readiness_endpoint(monkeypatch):
    """GET /api/llm/readiness returns 200 with structured readiness schema."""
    from opencohost.core.engine.llm_readiness import LlmReadinessResult

    mock_result = LlmReadinessResult(
        state="LOCAL_READY",
        provider="local",
        can_chat=True,
        selected_model="gemma4:e4b",
        ollama={"reachable": True, "version": "0.5.1", "installed_models": ["gemma4:e4b"]},
        cloud={"configured": False, "provider_id": "nvidia", "reachable": None},
        hardware={
            "gpu_name": "NVIDIA GeForce RTX 5060",
            "total_vram_mb": 8192,
            "recommended_tag": "gemma4:e4b",
            "recommended_command": "ollama run gemma4:e4b"
        }
    )

    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness.resolve_llm_readiness",
        lambda host: mock_result
    )

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/llm/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "LOCAL_READY"
        assert data["can_chat"] is True
        assert data["selected_model"] == "gemma4:e4b"
        assert data["hardware"]["recommended_tag"] == "gemma4:e4b"


# ──────────────────────────────────────────────────────────────────────────
# 5. Fail-Fast Guard on POST /api/chat/message
# ──────────────────────────────────────────────────────────────────────────

def test_post_chat_message_fails_fast_when_unready(monkeypatch):
    """POST /api/chat/message must reject with 409 Conflict immediately when can_chat is False."""
    from opencohost.core.engine.llm_readiness import LlmReadinessResult

    mock_unready = LlmReadinessResult(
        state="LOCAL_OLLAMA_MISSING",
        provider="local",
        can_chat=False,
        selected_model="gemma4:e4b",
        ollama={"reachable": False, "version": None, "installed_models": []},
        cloud={"configured": False, "provider_id": "nvidia", "reachable": None},
        hardware={"gpu_name": None, "total_vram_mb": 0, "recommended_tag": "gemma4:e2b", "recommended_command": "ollama run gemma4:e2b"}
    )

    monkeypatch.setattr(
        "opencohost.core.engine.llm_readiness.resolve_llm_readiness",
        lambda host: mock_unready
    )

    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/chat/message",
            json={"text": "Hola Kira", "source": "chat"}
        )
        assert resp.status_code == 409
        detail = resp.json().get("detail", {})
        assert detail.get("error") == "ENGINE_UNAVAILABLE"
        assert detail.get("state") == "LOCAL_OLLAMA_MISSING"


# ──────────────────────────────────────────────────────────────────────────
# 6. Catalog vs Installed Separation in Settings
# ──────────────────────────────────────────────────────────────────────────

def test_is_runtime_model_available_rejects_uninstalled_catalog_model():
    """Catalog presence must NEVER satisfy availability if the model is not installed."""
    # Ensure gemma4:e4b is in MODELS_CATALOG for test validity
    assert "gemma4:e4b" in MODELS_CATALOG

    # When installed_model_tags is empty, it MUST return False
    assert is_runtime_model_available("gemma4:e4b", installed_model_tags=[]) is False

    # When installed_model_tags has the model, it returns True
    assert is_runtime_model_available("gemma4:e4b", installed_model_tags=["gemma4:e4b"]) is True

    # When installed_model_tags has a different model, it returns False
    assert is_runtime_model_available("gemma4:e4b", installed_model_tags=["qwen3:4b"]) is False
