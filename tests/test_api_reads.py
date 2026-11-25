"""Tests for the Tier-B direct-read GET endpoints (B3): /api/models,
/api/tts/config, /api/memoria/stats.

All three are sync-def direct reads — no command_queue, no engine mutation,
no new host plumbing (design v2.1 Tier B). R8-CRITICAL: /api/memoria/stats
must never expose memoria/card text — counts only, via the
`memory_inspector_snapshot` provenance gate (reused verbatim, never
re-derived — see opencohost/core/llm_engine.py).
"""

import pytest
from fastapi.testclient import TestClient

from opencohost.core.observability.health_monitor import MonitorState
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/models
# ──────────────────────────────────────────────────────────────────────────


def test_models_shape_and_real_catalog(tmp_path, monkeypatch):
    from opencohost.config.settings import MODELS_CATALOG

    # T2 (model_context_auto_config_20260928): isolate model_parameters.json
    # so `context_config`'s asserted values do not depend on this repo's own
    # ambient dev config/model_parameters.json (a real file with the
    # developer's own overrides, e.g. context.policy="requested" -- see
    # test_update_model_reasoning_endpoint for the same isolation idiom;
    # reasoning_config below happened to match defaults by coincidence, this
    # keeps context_config from depending on that same luck).
    import opencohost.config.model_parameters as mp
    monkeypatch.setattr(mp, "MODEL_PARAMETERS_CONFIG_FILE", str(tmp_path / "model_params.json"))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "catalog",
            "discovered",
            "current_model",
            "tiers",
            "active_tier",
            "is_reasoning_active",
            "reasoning_config",
            "runtime_state",
            "context_config",
        }
        assert body["is_reasoning_active"] is True
        # T2 (controls-cleanup-adaptive-reasoning-20260928): a fresh/isolated
        # config is Auto by default (budget_tokens=None), not the historical
        # literal 512 -- the adaptive engine decides at generation time.
        assert body["reasoning_config"] == {"enabled": False, "budget_tokens": None}
        # "effective_budget" is best-effort and read-only: under Auto there is
        # no cheaply-available last-known value to report, so it is null
        # rather than a fabricated number (T2 "don't invent data you don't have").
        assert body["runtime_state"]["effective_budget"] is None
        # real curated catalog, not a hand-rolled stub
        assert set(body["catalog"].keys()) == set(MODELS_CATALOG.keys())
        assert body["current_model"] == "qwen3:8b"
        assert body["active_tier"] == "balanced"
        assert isinstance(body["tiers"], dict) and body["tiers"]
        assert isinstance(body["discovered"], list)
        # T2 (model_context_auto_config_20260928): FakeMotor has no
        # _model_ctx_limit/_resolve_effective_ctx_limit, so native/effective
        # degrade to None -- unconfigured (Auto) with the default cap.
        assert body["context_config"] == {
            "model": "qwen3:8b",
            "context_tokens": None,
            "auto_context_tokens": 16384,
            "native_context": None,
            "effective_context": None,
            "policy": "runtime_truth",
        }


def test_models_discovery_timeout_degrades_to_catalog_only(monkeypatch):
    import opencohost.api.main as main_mod

    def _raise_timeout(*args, **kwargs):
        raise TimeoutError("ollama discovery timed out")

    monkeypatch.setattr(main_mod, "_discover_ollama_models", _raise_timeout)

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["discovered"] == []
        assert body["catalog"]  # catalog still present


def test_models_discovery_error_degrades_never_500(monkeypatch):
    import opencohost.api.main as main_mod

    class _BrokenOllama:
        def Client(self, *a, **kw):
            raise ConnectionError("ollama unreachable")

    monkeypatch.setattr(main_mod, "ollama", _BrokenOllama())

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["discovered"] == []


# ──────────────────────────────────────────────────────────────────────────
# GET /api/models — provider-aware (multi_provider_llm_20260723 Phase 5)
# ──────────────────────────────────────────────────────────────────────────


def test_models_cloud_active_returns_active_profile_model_and_no_catalog(monkeypatch):
    """Cloud active: current_model/discovered come from the ACTIVE profile
    only, and no download/install catalog is exposed (spec D).

    F4 (1.3): `_display_model` now reads the ENGINE's live provider state,
    not this disk-backed `load_provider_config()` — so the fake motor's
    `_provider_config` must agree with it (the normal non-fallback case:
    engine config mirrors disk)."""
    import opencohost.api.main as main_mod

    cfg = {
        "active_provider": "openai",
        "profiles": {
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            "nvidia_nim": {"base_url": "https://integrate.api.nvidia.com/v1", "model": "deepseek-r1"},
        },
    }
    monkeypatch.setattr(main_mod, "load_provider_config", lambda: cfg)

    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._provider_config = cfg
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["current_model"] == "gpt-4o-mini"
        assert body["discovered"] == ["gpt-4o-mini"]
        assert body["catalog"] == {}  # no download/install affordance on cloud
        assert body["tiers"] == {}


def test_models_cloud_active_skips_ollama_discovery(monkeypatch):
    """No `ollama.list` discovery call on cloud (spec D)."""
    import opencohost.api.main as main_mod

    calls = []

    def _track_discover(*a, **kw):
        calls.append(1)
        return []

    monkeypatch.setattr(main_mod, "_discover_ollama_models", _track_discover)
    monkeypatch.setattr(
        main_mod,
        "load_provider_config",
        lambda: {
            "active_provider": "openai",
            "profiles": {"openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"}},
        },
    )

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        assert calls == []


def test_models_cloud_active_never_leaks_other_profile_model(monkeypatch):
    """Profile isolation: the active profile's model is used, never a
    differently-configured profile's."""
    import opencohost.api.main as main_mod

    cfg = {
        "active_provider": "nvidia_nim",
        "profiles": {
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            "nvidia_nim": {"base_url": "https://integrate.api.nvidia.com/v1", "model": "deepseek-r1"},
        },
    }
    monkeypatch.setattr(main_mod, "load_provider_config", lambda: cfg)

    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._provider_config = cfg
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["current_model"] == "deepseek-r1"
        assert "gpt-4o-mini" not in body["discovered"]


def test_models_explicit_local_provider_byte_identical(monkeypatch):
    """An explicit `active_provider: local` config keeps the exact
    pre-track shape — still calls Ollama discovery, still exposes the full
    catalog (byte-identical local-path guarantee)."""
    import opencohost.api.main as main_mod
    from opencohost.config.settings import MODELS_CATALOG

    monkeypatch.setattr(
        main_mod, "load_provider_config", lambda: {"active_provider": "local", "profiles": {}}
    )

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["catalog"].keys()) == set(MODELS_CATALOG.keys())
        assert body["current_model"] == "qwen3:8b"
        assert body["active_tier"] == "balanced"


def test_models_cloud_active_provider_absent_from_profiles_degrades(monkeypatch):
    """Cloud active but the active_provider has NO entry in `profiles`
    (misconfig / deleted profile): the read must still 200 with an empty
    catalog/discovered and a null current_model — never a 500/crash."""
    import opencohost.api.main as main_mod

    cfg = {"active_provider": "openai", "profiles": {}}
    monkeypatch.setattr(main_mod, "load_provider_config", lambda: cfg)

    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._provider_config = cfg
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["catalog"] == {}
        assert body["discovered"] == []
        assert body["current_model"] is None
        assert body["tiers"] == {}
        assert body["active_tier"] == "cloud"


# ──────────────────────────────────────────────────────────────────────────
# GET /api/status — provider-aware current_model (display-bug fix)
#
# Bug: get_status() reported host.motor.current_model unconditionally, even
# when a cloud provider was active — so the UI kept showing the stale local
# model name (e.g. "gemma4:e4b") while the engine was actually generating
# with the cloud profile's model (e.g. "GLM 5.2"). get_models() already had
# the correct active_provider branch; this shares that same logic via
# `_display_model` so /api/status and /api/models can never drift again.
#
# F4 (runtime_findings_batch_20260731 1.3): `_display_model`/get_status now
# read `host.motor.provider_runtime_state()` — the engine's LIVE effective
# posture — instead of `load_provider_config()` (disk). Tests below set the
# fake motor's `_provider_config`/`_cloud_fallback_active` directly (same
# pattern as `app.state.host.motor._current_profile_name = ...` elsewhere in
# this suite) rather than monkeypatching the now-unused disk read.
# ──────────────────────────────────────────────────────────────────────────


def test_status_local_provider_reports_engine_model_unchanged():
    """Local provider active (default/explicit): /api/status keeps reporting
    the engine's own current_model — behavior must not change."""
    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._provider_config = {"active_provider": "local", "profiles": {}}
        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["current_model"] == "qwen3:8b"
        assert body["provider"] == "local"
        assert body["transport"] == "local"
        assert body["fallback_active"] is False


def test_status_cloud_active_reports_active_profile_model():
    """Cloud active: /api/status must report the ACTIVE profile's own model,
    never the engine's stale local current_model bookkeeping attr."""
    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._provider_config = {
            "active_provider": "glm",
            "profiles": {
                "glm": {"base_url": "https://api.z.ai/v1", "model": "glm-5.2"},
            },
        }
        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["current_model"] == "glm-5.2"
        assert body["current_model"] != "qwen3:8b"
        assert body["provider"] == "glm"
        assert body["transport"] == "cloud"
        assert body["fallback_active"] is False


def test_status_cloud_active_provider_absent_from_profiles_degrades():
    """Cloud active but the active_provider has no entry in `profiles`
    (misconfig/deleted profile): current_model degrades to null, same as
    /api/models — never a stale local model, never a 500."""
    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._provider_config = {"active_provider": "glm", "profiles": {}}
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["current_model"] is None


def test_status_cloud_active_profile_empty_model_degrades():
    """Cloud active, profile present but its `model` field is blank: same
    graceful null degradation as /api/models."""
    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._provider_config = {
            "active_provider": "glm",
            "profiles": {"glm": {"base_url": "https://api.z.ai/v1", "model": ""}},
        }
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["current_model"] is None


def test_status_fallback_active_reports_local_transport_despite_cloud_config(monkeypatch):
    """The exact F4 defect: disk (and the engine's persisted `active_provider`)
    still say cloud, but `_cloud_fallback_active` is set — /api/status must
    report the LIVE effective posture (local, fallback_active=True), never
    the stale cloud provider/model `_display_model` used to keep reporting
    for the rest of the process."""
    import opencohost.api.main as main_mod

    cloud_cfg = {
        "active_provider": "glm",
        "profiles": {"glm": {"base_url": "https://api.z.ai/v1", "model": "glm-5.2"}},
    }
    # Disk stays on cloud — the stale-file trap this unit removes as a source
    # of truth for /api/status.
    monkeypatch.setattr(main_mod, "load_provider_config", lambda: cloud_cfg)

    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._provider_config = cloud_cfg
        app.state.host.motor._cloud_fallback_active = True

        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["transport"] == "local"
        assert body["fallback_active"] is True
        assert body["provider"] == "local"
        # The two-lies bug: display must say the engine's real local model,
        # never the disk-configured cloud model.
        assert body["current_model"] == app.state.host.motor.current_model
        assert body["current_model"] != "glm-5.2"


# ──────────────────────────────────────────────────────────────────────────
# GET /api/status — resource numbers (F9/F14, 2.4)
# ──────────────────────────────────────────────────────────────────────────


def test_status_health_carries_vram_split_and_ollama_residency_estimates():
    """The new HealthState fields flow through MonitorState -> HealthState
    (HealthState(**dataclasses.asdict(...)) in main.py) with no route code
    needed beyond the model/dataclass field additions."""
    app = _app()
    with TestClient(app) as client:
        app.state.host.monitor.state = MonitorState(
            vram_status="normal",
            rtf_status="normal",
            ollama_status="healthy",
            qwen_status="healthy",
            overall_status="green",
            ollama_lifecycle="ready",
            qwen_lifecycle="ready",
            free_vram_mb=3892.0,
            rtf_rolling_avg=0.4,
            last_updated=123.0,
            total_vram_mb=8192.0,
            used_vram_mb=4300.0,
            model_resident_mb_est=6300.0,
            model_vram_mb_est=4200.0,
            model_ram_spill_mb_est=2100.0,
            model_processor_split="67% GPU / 33% CPU",
        )
        resp = client.get("/api/status")
        assert resp.status_code == 200
        health = resp.json()["health"]
        assert health["total_vram_mb"] == 8192.0
        assert health["used_vram_mb"] == 4300.0
        assert health["model_resident_mb_est"] == 6300.0
        assert health["model_vram_mb_est"] == 4200.0
        assert health["model_ram_spill_mb_est"] == 2100.0
        assert health["model_processor_split"] == "67% GPU / 33% CPU"


def test_status_health_residency_absent_when_no_model_loaded():
    """No model loaded / Ollama unreachable: residency fields are None, not
    stale — never a fabricated non-zero number."""
    app = _app()
    with TestClient(app) as client:
        app.state.host.monitor.state = MonitorState(
            vram_status="normal",
            rtf_status="normal",
            ollama_status="down",
            qwen_status="healthy",
            overall_status="yellow",
            ollama_lifecycle="degraded",
            qwen_lifecycle="ready",
            free_vram_mb=3892.0,
            rtf_rolling_avg=0.4,
            last_updated=123.0,
        )
        resp = client.get("/api/status")
        assert resp.status_code == 200
        health = resp.json()["health"]
        assert health["model_resident_mb_est"] is None
        assert health["model_vram_mb_est"] is None
        assert health["model_ram_spill_mb_est"] is None
        assert health["model_processor_split"] is None


def test_models_and_status_agree_on_display_model_cloud(monkeypatch):
    """Regression guard for the reported bug: the same active provider must
    yield the SAME current_model on both endpoints — they can never drift
    apart again, since both route through the shared helper."""
    import opencohost.api.main as main_mod

    cfg = {
        "active_provider": "glm",
        "profiles": {"glm": {"base_url": "https://api.z.ai/v1", "model": "glm-5.2"}},
    }
    monkeypatch.setattr(main_mod, "load_provider_config", lambda: cfg)

    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._provider_config = cfg
        status_model = client.get("/api/status").json()["current_model"]
        models_model = client.get("/api/models").json()["current_model"]
        assert status_model == models_model == "glm-5.2"


# ──────────────────────────────────────────────────────────────────────────
# GET /api/tts/config
# ──────────────────────────────────────────────────────────────────────────


def test_tts_config_shape_from_accessors(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "load_piper_voice", lambda **kwargs: "es_kira")
    monkeypatch.setattr(main_mod, "load_tts_local_only", lambda: True)
    monkeypatch.setattr(main_mod, "load_tts_speed", lambda: 1.25)
    monkeypatch.setattr(main_mod, "load_interrupt_on_direct", lambda: False)
    monkeypatch.setattr(main_mod, "EXPERIMENTAL_HEAVY_TTS_ENABLED", True)
    from opencohost.api.routers import i18n_tts as i18n_tts_mod
    from opencohost.i18n import active as i18n_active

    monkeypatch.setattr(i18n_tts_mod, "_piper_unavailable_reason", lambda voice_key: None)

    app = _app()
    with TestClient(app) as client:
        app.state.host.motor.motor_tts = "pesado"
        resp = client.get("/api/tts/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "piper_voice": "es_kira",
            "local_only": True,
            "speed": 1.25,
            "engine": "pesado",
            "heavy_available": True,
            "piper_available": False,
            "piper_unavailable_reason": None,
            "edge_tts_offline": False,
            "edge_tts_degraded": False,
            "edge_tts_last_error": None,
            # Default is ON since 98c22c7 (feat(speech): enable discard residue by default).
            "discard_speech_residue": True,
            # ADR-069 (additive): opt-in cut at direct-message arrival, default off.
            "interrupt_on_direct": False,
            # voice-catalog-selection-20261118 T2 (additive). "es_kira" is not a registry key.
            "piper_voice_id": None,
            "piper_speaker_id": None,
            "piper_switch_failure_reason": None,
            "edge_voice": i18n_active.edge_voice(),
            "edge_voice_failure_reason": None,
        }


@pytest.mark.parametrize(
    "package_ok, file_present, expected",
    [(False, True, "package_missing"), (False, False, "package_missing"),
     (True, False, "voice_file_missing"), (True, True, None)],
)
def test_piper_unavailable_reason_distinguishes_package_from_voice_file(
    monkeypatch, tmp_path, package_ok, file_present, expected
):
    from opencohost.api.routers import i18n_tts as i18n_tts_mod
    from opencohost.core.speech.backends import tts_piper

    voice = tmp_path / "voice.onnx"
    if file_present:
        voice.write_bytes(b"x")
    monkeypatch.setattr(tts_piper, "_PIPER_AVAILABLE", package_ok)
    monkeypatch.setattr(i18n_tts_mod, "piper_voice_path", lambda key: str(voice))
    assert i18n_tts_mod._piper_unavailable_reason("english") == expected


def test_tts_config_reports_voice_file_missing_when_piper_unavailable(monkeypatch):
    from opencohost.api.routers import i18n_tts as i18n_tts_mod

    monkeypatch.setattr(i18n_tts_mod, "_piper_unavailable_reason", lambda key: "voice_file_missing")
    app = _app()
    with TestClient(app) as client:
        class _StubPiper:
            def is_available(self):
                return False

        app.state.host.motor._piper = _StubPiper()
        body = client.get("/api/tts/config").json()
        assert body["piper_available"] is False
        assert body["piper_unavailable_reason"] == "voice_file_missing"

        class _OkPiper:
            def is_available(self):
                return True

        app.state.host.motor._piper = _OkPiper()
        assert client.get("/api/tts/config").json()["piper_unavailable_reason"] is None


def test_tts_config_surfaces_piper_and_edge_tts_status():
    app = _app()
    with TestClient(app) as client:
        class _StubPiper:
            def is_available(self):
                return True

        app.state.host.motor._piper = _StubPiper()
        app.state.host.motor._edge_tts_offline = True

        resp = client.get("/api/tts/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["piper_available"] is True
        assert body["edge_tts_offline"] is True


def test_tts_config_surfaces_edge_breaker_degraded_state():
    from opencohost.core.speech.speech_rate import EdgeBreaker

    app = _app()
    with TestClient(app) as client:
        breaker = EdgeBreaker(3, 120, 900)
        app.state.host.motor._edge_breaker = breaker
        body = client.get("/api/tts/config").json()
        assert body["edge_tts_degraded"] is False
        assert body["edge_tts_last_error"] is None

        for _ in range(3):
            breaker.record_failure("http_403")
        body = client.get("/api/tts/config").json()
        assert body["edge_tts_degraded"] is True
        assert body["edge_tts_last_error"] == "http_403"

        breaker.record_success()
        body = client.get("/api/tts/config").json()
        assert body["edge_tts_degraded"] is False
        assert body["edge_tts_last_error"] is None


def test_tts_config_offline_reports_fixed_error_code():
    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._edge_tts_offline = True
        body = client.get("/api/tts/config").json()
        assert body["edge_tts_offline"] is True
        assert body["edge_tts_last_error"] == "offline"


def test_tts_config_prefers_offline_over_stale_breaker_error():
    from opencohost.core.speech.speech_rate import EdgeBreaker

    app = _app()
    with TestClient(app) as client:
        breaker = EdgeBreaker(3, 120, 900)
        for _ in range(3):
            breaker.record_failure("http_403")
        app.state.host.motor._edge_breaker = breaker
        app.state.host.motor._edge_tts_offline = True
        body = client.get("/api/tts/config").json()
        assert body["edge_tts_last_error"] == "offline"


def test_tts_config_does_not_touch_command_queue():
    app = _app()
    with TestClient(app) as client:
        client.get("/api/tts/config")
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


# ──────────────────────────────────────────────────────────────────────────
# GET /api/memoria/stats (R8-CRITICAL)
# ──────────────────────────────────────────────────────────────────────────

_FORBIDDEN_SUBSTRINGS = ("content", "transcript", "dialogue", "persona", "prompt", "title")


def test_memoria_stats_shape_counts_only():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "session_turns",
            "digest_entries",
            "saved_memorias",
            "pinned",
            "saved_memorias_total",
            "pinned_total",
            "editorial_cards_by_status",
        }
        for key in (
            "session_turns",
            "digest_entries",
            "saved_memorias",
            "pinned",
            "saved_memorias_total",
            "pinned_total",
        ):
            assert isinstance(body[key], int)


def test_memoria_stats_never_leaks_text_fields():
    """R8: strong negative assertion — no text/persona/transcript anywhere
    in the response, including keys and string values."""
    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._memory_snapshot = {
            "entries": [
                {"turn_index": 0, "role": "user", "source": "direct", "content_chars": 12, "content": "secret text"},
                {"turn_index": 1, "role": "assistant", "source": "ptt", "content_chars": 20, "content": "Kira's own words"},
            ],
            "source_breakdown": {"direct": 1, "ptt": 1},
            "digest": {"line_count": 3, "total_chars": 90, "max_chars": 4000},
        }
        resp = client.get("/api/memoria/stats")
        assert resp.status_code == 200
        raw = resp.text.lower()
        assert "secret text" not in raw
        assert "kira's own words" not in raw
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in raw
        assert resp.json()["session_turns"] == 2
        assert resp.json()["digest_entries"] == 3


def test_memoria_stats_reuses_memory_inspector_snapshot_gate():
    """Must call the engine's memory_inspector_snapshot() (the ONLY
    provenance gate) rather than re-deriving _DIGEST_CAPTURE_SOURCES
    filtering in the API layer."""
    app = _app()
    calls = {"n": 0}
    with TestClient(app) as client:
        original = app.state.host.motor.memory_inspector_snapshot

        def _spy():
            calls["n"] += 1
            return original()

        app.state.host.motor.memory_inspector_snapshot = _spy
        client.get("/api/memoria/stats")
        assert calls["n"] == 1


def test_memoria_stats_does_not_touch_command_queue():
    app = _app()
    with TestClient(app) as client:
        client.get("/api/memoria/stats")
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


def test_memoria_stats_memorias_enabled_true_counts_real_rows(tmp_path, monkeypatch):
    import sqlite3

    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memorias (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO memorias (id, pinned) VALUES ('a', 1)")
    conn.execute("INSERT INTO memorias (id, pinned) VALUES ('b', 0)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        body = resp.json()
        assert body["saved_memorias"] == 2
        assert body["pinned"] == 1
        # No profile_id -> per-profile figures coincide with the global totals.
        assert body["saved_memorias_total"] == 2
        assert body["pinned_total"] == 1


def test_memoria_stats_profile_id_splits_per_profile_vs_global(tmp_path, monkeypatch):
    """FIX-A: with ?profile_id=, saved_memorias/pinned filter to that profile
    (parity with MemoriaStore.count_all/count_all_pinned) while
    saved_memorias_total/pinned_total stay global."""
    import sqlite3

    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memorias (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, "
        "pinned INTEGER NOT NULL DEFAULT 0)"
    )
    # Profile "akira": 3 rows, 2 pinned. Profile "bravo": 1 row, 0 pinned.
    conn.execute("INSERT INTO memorias (id, profile_id, pinned) VALUES ('a1', 'akira', 1)")
    conn.execute("INSERT INTO memorias (id, profile_id, pinned) VALUES ('a2', 'akira', 1)")
    conn.execute("INSERT INTO memorias (id, profile_id, pinned) VALUES ('a3', 'akira', 0)")
    conn.execute("INSERT INTO memorias (id, profile_id, pinned) VALUES ('b1', 'bravo', 0)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats", params={"profile_id": "akira"})
        assert resp.status_code == 200
        body = resp.json()
        # Per-profile figures for "akira".
        assert body["saved_memorias"] == 3
        assert body["pinned"] == 2
        # Global totals across every profile.
        assert body["saved_memorias_total"] == 4
        assert body["pinned_total"] == 2

        # A profile with a single, unpinned row.
        resp_b = client.get("/api/memoria/stats", params={"profile_id": "bravo"})
        body_b = resp_b.json()
        assert body_b["saved_memorias"] == 1
        assert body_b["pinned"] == 0
        assert body_b["saved_memorias_total"] == 4
        assert body_b["pinned_total"] == 2

        # An unknown profile -> zero per-profile, totals unchanged.
        resp_x = client.get("/api/memoria/stats", params={"profile_id": "ghost"})
        body_x = resp_x.json()
        assert body_x["saved_memorias"] == 0
        assert body_x["pinned"] == 0
        assert body_x["saved_memorias_total"] == 4


def test_memoria_stats_memorias_disabled_returns_zeros_gracefully(tmp_path, monkeypatch):
    import sqlite3

    import opencohost.api.main as main_mod

    db_path = tmp_path / "memorias.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memorias (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO memorias (id, pinned) VALUES ('a', 1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", False)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["saved_memorias"] == 0
        assert body["pinned"] == 0


def test_memoria_stats_editorial_cards_by_status(tmp_path, monkeypatch):
    import sqlite3

    import opencohost.api.main as main_mod

    db_path = tmp_path / "cards.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE editorial_cards (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    conn.execute("INSERT INTO editorial_cards (id, status) VALUES ('c1', 'armed')")
    conn.execute("INSERT INTO editorial_cards (id, status) VALUES ('c2', 'armed')")
    conn.execute("INSERT INTO editorial_cards (id, status) VALUES ('c3', 'used')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(db_path))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        body = resp.json()
        assert body["editorial_cards_by_status"] == {"armed": 2, "used": 1}


def test_memoria_stats_missing_db_files_return_zero_not_500(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(main_mod, "MEMORIAS_DB", str(tmp_path / "nope.db"))
    monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "also-nope.db"))

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/memoria/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["saved_memorias"] == 0
        assert body["pinned"] == 0
        assert body["editorial_cards_by_status"] == {}


def test_update_model_reasoning_endpoint(tmp_path, monkeypatch):
    import opencohost.config.model_parameters as mp

    cfg_file = str(tmp_path / "model_params.json")
    monkeypatch.setattr(mp, "MODEL_PARAMETERS_CONFIG_FILE", cfg_file)

    app = _app()
    with TestClient(app) as client:
        # Update reasoning config via PUT
        resp = client.put(
            "/api/models/reasoning",
            json={"enabled": True, "budget_tokens": 1024, "model": "qwen3:8b"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True, "budget_tokens": 1024}

        # Verify GET /api/models reflects it
        models_resp = client.get("/api/models")
        assert models_resp.status_code == 200
        data = models_resp.json()
        assert data["is_reasoning_active"] is True
        assert data["reasoning_config"] == {"enabled": True, "budget_tokens": 1024}


def test_update_model_reasoning_endpoint_null_budget_clears_to_auto(tmp_path, monkeypatch):
    """T2 (controls-cleanup-adaptive-reasoning-20260928): PUT with
    ``budget_tokens: null`` explicitly (present in the JSON body, not just
    omitted) clears a previously-set manual cap back to Auto -- mirrors
    PUT /api/models/context's own null-clears-to-Auto convention."""
    import opencohost.config.model_parameters as mp

    cfg_file = str(tmp_path / "model_params.json")
    monkeypatch.setattr(mp, "MODEL_PARAMETERS_CONFIG_FILE", cfg_file)

    app = _app()
    with TestClient(app) as client:
        client.put(
            "/api/models/reasoning",
            json={"enabled": True, "budget_tokens": 1024, "model": "qwen3:8b"},
        )
        resp = client.put(
            "/api/models/reasoning",
            json={"budget_tokens": None, "model": "qwen3:8b"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True, "budget_tokens": None}

        models_resp = client.get("/api/models")
        assert models_resp.json()["reasoning_config"] == {"enabled": True, "budget_tokens": None}


def test_update_model_reasoning_endpoint_omitted_budget_leaves_it_unchanged(tmp_path, monkeypatch):
    """Omitting `budget_tokens` from the PUT body entirely (never present in
    the JSON, distinct from an explicit null) must NOT clear it -- only
    `enabled` is being toggled here, mirroring the pre-existing partial-update
    convention for `update_model_reasoning_settings`."""
    import opencohost.config.model_parameters as mp

    cfg_file = str(tmp_path / "model_params.json")
    monkeypatch.setattr(mp, "MODEL_PARAMETERS_CONFIG_FILE", cfg_file)

    app = _app()
    with TestClient(app) as client:
        client.put(
            "/api/models/reasoning",
            json={"enabled": True, "budget_tokens": 1024, "model": "qwen3:8b"},
        )
        resp = client.put(
            "/api/models/reasoning",
            json={"enabled": False, "model": "qwen3:8b"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "budget_tokens": 1024}


def test_update_model_context_endpoint(tmp_path, monkeypatch):
    """T2 (model_context_auto_config_20260928): PUT persists an explicit
    override (auto-flips policy to "requested") and GET /api/models reflects
    it immediately."""
    import opencohost.config.model_parameters as mp

    cfg_file = str(tmp_path / "model_params.json")
    monkeypatch.setattr(mp, "MODEL_PARAMETERS_CONFIG_FILE", cfg_file)

    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/models/context",
            json={"model": "qwen3:8b", "context_tokens": 32768},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == "qwen3:8b"
        assert body["context_tokens"] == 32768
        assert body["policy"] == "requested"
        assert body["auto_context_tokens"] == 16384

        models_resp = client.get("/api/models")
        assert models_resp.status_code == 200
        ctx_cfg = models_resp.json()["context_config"]
        assert ctx_cfg["context_tokens"] == 32768
        assert ctx_cfg["policy"] == "requested"


def test_update_model_context_none_clears_to_auto(tmp_path, monkeypatch):
    import opencohost.config.model_parameters as mp

    cfg_file = str(tmp_path / "model_params.json")
    monkeypatch.setattr(mp, "MODEL_PARAMETERS_CONFIG_FILE", cfg_file)

    app = _app()
    with TestClient(app) as client:
        client.put("/api/models/context", json={"model": "qwen3:8b", "context_tokens": 32768})
        resp = client.put("/api/models/context", json={"model": "qwen3:8b", "context_tokens": None})
        assert resp.status_code == 200
        assert resp.json()["context_tokens"] is None


def test_update_model_context_out_of_range_clamps_not_422(tmp_path, monkeypatch):
    """Convention follows PUT /api/models/reasoning: silent clamp, no 422."""
    import opencohost.config.model_parameters as mp

    cfg_file = str(tmp_path / "model_params.json")
    monkeypatch.setattr(mp, "MODEL_PARAMETERS_CONFIG_FILE", cfg_file)

    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/models/context",
            json={"model": "qwen3:8b", "context_tokens": 999_999_999},
        )
        assert resp.status_code == 200
        assert resp.json()["context_tokens"] == 131072  # MAX_CONTEXT_TOKENS


def test_update_model_context_reload_glitch_never_500s(tmp_path, monkeypatch):
    """FakeMotor has no `request_model_context_reload`: the PUT still
    persists and returns 200, it just cannot force a live reload."""
    import opencohost.config.model_parameters as mp

    cfg_file = str(tmp_path / "model_params.json")
    monkeypatch.setattr(mp, "MODEL_PARAMETERS_CONFIG_FILE", cfg_file)

    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/models/context",
            json={"model": "qwen3:8b", "context_tokens": 8192},
        )
        assert resp.status_code == 200
        assert resp.json()["context_tokens"] == 8192


def test_tts_config_exposes_registry_voice_id(monkeypatch):
    import opencohost.api.main as main_mod
    from opencohost.config import settings

    monkeypatch.setattr(main_mod, "load_piper_voice", lambda **kwargs: "english")
    app = _app()
    with TestClient(app) as client:
        body = client.get("/api/tts/config").json()
    assert body["piper_voice_id"] == settings.PIPER_VOICES["english"]["file"]
    assert body["piper_speaker_id"] is None
