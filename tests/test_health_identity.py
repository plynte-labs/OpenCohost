"""S6 (release-050-stabilization): /api/health reports build_version + install_id.

The Tauri supervisor compares them with its own install's runtime manifest to
refuse reusing a stale or foreign backend. Field names are a shared contract
with the Rust side: exactly `build_version` and `install_id`. Additive only.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import opencohost
from opencohost.core import install_identity
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _fresh_cache():
    install_identity.reset_cache()
    yield
    install_identity.reset_cache()


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def test_build_version_is_the_package_version():
    assert install_identity.build_version() == opencohost.__version__


def test_install_id_comes_from_the_runtime_manifest_when_present(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "runtime-manifest.json").write_text(
        json.dumps({"install_id": "abc-123", "product_version": "0.5.0"}), encoding="utf-8"
    )
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", str(tmp_path))
    assert install_identity.install_id() == "abc-123"


def test_manifest_missing_means_no_install_id_and_nothing_written(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", str(tmp_path))
    assert install_identity.install_id() is None
    assert not (tmp_path / "state").exists()


def test_blank_manifest_id_is_none(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "runtime-manifest.json").write_text(json.dumps({"install_id": "  "}), encoding="utf-8")
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", str(tmp_path))
    assert install_identity.install_id() is None


def test_corrupt_manifest_omits_the_field_and_does_not_raise(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "runtime-manifest.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", str(tmp_path))
    assert install_identity.install_id() is None


def test_no_data_root_means_no_install_id_and_no_files(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOHOST_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert install_identity.install_id() is None
    assert not (tmp_path / "state").exists()


def _manifest(tmp_path, monkeypatch, install="inst-42"):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "runtime-manifest.json").write_text(
        json.dumps({"install_id": install, "product_version": "0.5.0a1"}), encoding="utf-8"
    )
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", str(tmp_path))


def test_health_reports_both_fields_from_the_manifest(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch)
    with TestClient(_app()) as client:
        body = client.get("/api/health").json()
    assert body["build_version"] == opencohost.__version__
    assert body["install_id"] == "inst-42"
    assert body["status"] == "ok" and "engine_alive" in body and "engine_state" in body


def test_health_with_missing_manifest_reports_null_install_id(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", str(tmp_path))
    with TestClient(_app()) as client:
        body = client.get("/api/health").json()
    assert body["install_id"] is None and body["build_version"] == opencohost.__version__


def test_health_with_corrupt_manifest_does_not_crash(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "runtime-manifest.json").write_text("[1, 2,", encoding="utf-8")
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", str(tmp_path))
    with TestClient(_app()) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200 and resp.json()["install_id"] is None
