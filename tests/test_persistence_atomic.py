"""WU2 backend_reliability_fixes — atomic persistence + corrupt-file quarantine.

Covers (design id 2936, WU2):
- ``atomic_write_text``: an ``os.replace`` failure mid-save leaves the ORIGINAL
  file intact and leaves no ``*.tmp`` residue. Music, avatar, and cohost savers
  all route through this helper.
- Corrupt-load quarantine: a truncated json config is renamed to
  ``<file>.corrupt`` and the loader returns empty/defaults instead of silently
  swallowing the corruption.
- ``load_avatar_config(strict=True)``: distinguishes exists-but-unreadable from
  absent; the PUT /api/obs/config handler refuses to overwrite an unreadable
  config with defaults -> 503 ``config_unreadable``.

Seams mirror the design: ``monkeypatch.setattr("opencohost.config.storage.os.replace", ...)``
for the atomic path, garbage files for the quarantine/strict paths, and the
``AVATAR_CONFIG_FILE`` module-attribute patch (as tests/test_api_obs.py) for the
handler test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencohost.config.storage import atomic_write_text
from opencohost.core.music.music_library import MusicLibrary, MusicTrack
from opencohost.avatar.avatar_config import (
    AvatarConfig,
    AvatarConfigUnreadableError,
    load_avatar_config,
    save_avatar_config,
)
import opencohost.core.profiles.cohost_profiles as cohost_mod
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


def _raise_oserror(*_a, **_k):
    raise OSError("disk full (injected)")


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


# ──────────────────────────────────────────────────────────────────────────
# atomic_write_text — os.replace failure never truncates the target
# ──────────────────────────────────────────────────────────────────────────


def test_atomic_write_leaves_original_intact_on_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "data.txt"
    target.write_text("ORIGINAL", encoding="utf-8")
    monkeypatch.setattr("opencohost.config.storage.os.replace", _raise_oserror)

    with pytest.raises(OSError):
        atomic_write_text(target, "NEW CONTENT")

    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert list(tmp_path.glob("*.tmp")) == []


def test_music_save_atomic_failure_keeps_original(tmp_path, monkeypatch):
    cfg = tmp_path / "music_library.json"
    cfg.write_text('{"tracks": []}', encoding="utf-8")
    original = cfg.read_bytes()

    lib = MusicLibrary(library_dir=tmp_path / "music", config_file=cfg)
    lib.tracks["x"] = MusicTrack(
        id="x", original_name="a.wav", path="a.wav", mood="normal", label="normal"
    )
    monkeypatch.setattr("opencohost.config.storage.os.replace", _raise_oserror)

    with pytest.raises(OSError):
        lib.save()

    assert cfg.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_avatar_save_atomic_failure_keeps_original(tmp_path, monkeypatch):
    cfg = tmp_path / "avatar.yaml"
    cfg.write_text("avatar:\n  enabled: true\n", encoding="utf-8")
    original = cfg.read_bytes()

    monkeypatch.setattr("opencohost.config.storage.os.replace", _raise_oserror)

    with pytest.raises(OSError):
        save_avatar_config(AvatarConfig(), config_file=cfg)

    assert cfg.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_cohost_save_atomic_failure_keeps_original(tmp_path, monkeypatch):
    cfg = tmp_path / "cohost_profiles.json"
    cfg.write_text('{"Natural": {"style": "keep"}}', encoding="utf-8")
    original = cfg.read_bytes()

    monkeypatch.setattr(cohost_mod, "COHOST_PROFILES_FILE", str(cfg))
    monkeypatch.setattr("opencohost.config.storage.os.replace", _raise_oserror)

    # save_cohost_profiles keeps its never-raise contract (fail-open + log),
    # but the on-disk file must never be truncated and no temp must leak.
    cohost_mod.save_cohost_profiles({"Natural": {"style": "new"}})

    assert cfg.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


# ──────────────────────────────────────────────────────────────────────────
# Corrupt-load quarantine
# ──────────────────────────────────────────────────────────────────────────


def test_music_load_quarantines_corrupt_json(tmp_path):
    cfg = tmp_path / "music_library.json"
    cfg.write_bytes(b'{"tracks": [')  # truncated / invalid json

    lib = MusicLibrary(library_dir=tmp_path / "music", config_file=cfg)
    lib.load()

    assert lib.tracks == {}
    quarantined = tmp_path / "music_library.json.corrupt"
    assert quarantined.exists()
    assert not cfg.exists()


def test_cohost_load_quarantines_corrupt_json(tmp_path, monkeypatch):
    cfg = tmp_path / "cohost_profiles.json"
    cfg.write_bytes(b'{"Natural": ')  # truncated / invalid json
    monkeypatch.setattr(cohost_mod, "COHOST_PROFILES_FILE", str(cfg))

    result = cohost_mod.load_cohost_profiles()

    assert result == cohost_mod.DEFAULT_COHOST_PROFILES
    assert Path(str(cfg) + ".corrupt").exists()
    assert not cfg.exists()


# ──────────────────────────────────────────────────────────────────────────
# load_avatar_config(strict=True) — exists-but-unreadable vs absent
# ──────────────────────────────────────────────────────────────────────────


def test_load_avatar_strict_raises_on_non_mapping(tmp_path):
    cfg = tmp_path / "avatar.yaml"
    cfg.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(AvatarConfigUnreadableError):
        load_avatar_config(cfg, strict=True)


def test_load_avatar_strict_raises_on_yaml_error(tmp_path):
    cfg = tmp_path / "avatar.yaml"
    cfg.write_text("a: b: c\n", encoding="utf-8")  # yaml scanner error
    with pytest.raises(AvatarConfigUnreadableError):
        load_avatar_config(cfg, strict=True)


def test_load_avatar_non_strict_returns_defaults_on_unreadable(tmp_path):
    cfg = tmp_path / "avatar.yaml"
    cfg.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    assert load_avatar_config(cfg, strict=False) == AvatarConfig()


def test_load_avatar_strict_absent_file_returns_defaults(tmp_path):
    cfg = tmp_path / "does_not_exist.yaml"
    result = load_avatar_config(cfg, strict=True)
    assert result.enabled is True


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/obs/config refuses to overwrite an unreadable config -> 503
# ──────────────────────────────────────────────────────────────────────────


def test_put_obs_config_unreadable_returns_503_and_preserves_file(tmp_path, monkeypatch):
    import opencohost.avatar.avatar_config as avatar_config_mod
    import opencohost.api.main as main_mod

    garbage = tmp_path / "avatar.yaml"
    garbage.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    original = garbage.read_bytes()
    monkeypatch.setattr(avatar_config_mod, "AVATAR_CONFIG_FILE", garbage)

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        resp = client.put("/api/obs/config", json={"host": "attacker", "port": 4000})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "config_unreadable"

    # The unreadable file must NOT have been overwritten with defaults.
    assert garbage.read_bytes() == original


def test_put_avatar_config_unreadable_returns_503(tmp_path, monkeypatch):
    import opencohost.avatar.avatar_config as avatar_config_mod
    import opencohost.api.main as main_mod

    garbage = tmp_path / "avatar.yaml"
    garbage.write_text("a: b: c\n", encoding="utf-8")
    monkeypatch.setattr(avatar_config_mod, "AVATAR_CONFIG_FILE", garbage)

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        resp = client.put("/api/avatar/config", json={"enabled": False})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "config_unreadable"
