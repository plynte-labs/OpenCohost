"""Tests for the global personalization store (core/personalization.py).

Covers kira_personalization_onboarding_20260705 design §1/§2/§9:
- absent/corrupt file -> defaults, never raises
- clamp-at-load for oversized (hand-edited) fields
- save() atomic write + bool return, never raises, never logs content
- clear() resets to defaults
- mtime-cache invalidation
- build_injection_block: empty/disabled -> "", empty-field omission,
  per-field sanitizer, 900-char hard-truncation backstop

Mirrors tests/test_profiles_seeding.py's monkeypatch-the-module-attribute
pattern (PERSONALIZATION_FILE is looked up as a live module global on every
call, exactly like profiles.py's PROFILES_FILE).
"""

from __future__ import annotations

import json
import logging

import pytest

import opencohost.core.personalization as personalization
from opencohost.config import settings


@pytest.fixture(autouse=True)
def _pin_es_locale():
    """Pin the active locale to es so the delimiter block renders
    deterministically (same precedent as tests/test_memorias_retrieval.py)."""
    from opencohost.i18n import active

    from opencohost.i18n.startup import resolve_active_bundle

    active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    active.reset_active_bundle()


def _sanitize_passthrough(text: str) -> str:
    return text


# ---------------------------------------------------------------------------
# load_personalization — absent / corrupt file -> defaults, never raises
# ---------------------------------------------------------------------------


def test_load_returns_defaults_when_file_absent(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    data = personalization.load_personalization()

    assert data["enabled"] is True
    assert data["nickname"] == ""
    assert data["occupation"] == ""
    assert data["interests"] == ""
    assert data["custom_instructions"] == ""
    assert not file_path.exists()  # load must never create the file


def test_load_returns_defaults_on_corrupt_json(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    file_path.write_text("{this is: not valid json", encoding="utf-8")
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    data = personalization.load_personalization()

    assert data["enabled"] is True
    assert data["nickname"] == ""


def test_load_returns_defaults_when_file_is_not_a_dict(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    file_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    data = personalization.load_personalization()

    assert data["enabled"] is True
    assert data["nickname"] == ""


# ---------------------------------------------------------------------------
# load_personalization — clamp-at-load for oversized hand-edited fields
# ---------------------------------------------------------------------------


def test_load_clamps_oversized_fields_from_hand_edited_file(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    file_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "nickname": "N" * 200,
                "occupation": "O" * 300,
                "interests": "I" * 500,
                "custom_instructions": "C" * 999,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    data = personalization.load_personalization()

    assert len(data["nickname"]) == settings.PERSONALIZATION_NICKNAME_MAX
    assert len(data["occupation"]) == settings.PERSONALIZATION_OCCUPATION_MAX
    assert len(data["interests"]) == settings.PERSONALIZATION_INTERESTS_MAX
    assert len(data["custom_instructions"]) == settings.PERSONALIZATION_INSTRUCTIONS_MAX


def test_load_normalizes_non_string_updated_at_from_hand_edited_file(tmp_path, monkeypatch):
    """A hand-edited file with a non-string `updated_at` (e.g. a bare int)
    must be coerced to str, not passed through raw — otherwise the API's
    Pydantic response model (which types `updated_at: Optional[str]`) raises
    a ValidationError -> 500 on a value that is valid JSON but the wrong
    type (design §9: fail-open, never raise, on hand-edited files)."""
    file_path = tmp_path / "personalization.json"
    file_path.write_text(
        json.dumps({"enabled": True, "nickname": "Kira", "updated_at": 12345}),
        encoding="utf-8",
    )
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    data = personalization.load_personalization()

    assert data["updated_at"] == "12345"


# ---------------------------------------------------------------------------
# mtime cache invalidation
# ---------------------------------------------------------------------------


def test_load_uses_cache_until_mtime_changes(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    file_path.write_text(
        json.dumps({"enabled": True, "nickname": "Kira"}), encoding="utf-8"
    )
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    first = personalization.load_personalization()
    assert first["nickname"] == "Kira"

    calls = {"n": 0}
    real_load = json.load

    def counting_load(f):
        calls["n"] += 1
        return real_load(f)

    monkeypatch.setattr(personalization.json, "load", counting_load)

    second = personalization.load_personalization()
    assert second["nickname"] == "Kira"
    assert calls["n"] == 0  # cache hit — disk not re-read

    new_mtime = (file_path.stat().st_mtime) + 5
    file_path.write_text(
        json.dumps({"enabled": True, "nickname": "Ash"}), encoding="utf-8"
    )
    import os

    os.utime(file_path, (new_mtime, new_mtime))

    third = personalization.load_personalization()
    assert third["nickname"] == "Ash"
    assert calls["n"] == 1  # cache invalidated -> re-read from disk


# ---------------------------------------------------------------------------
# save_personalization — atomic write + bool return, never raises/logs content
# ---------------------------------------------------------------------------


def test_save_personalization_writes_atomically_and_returns_true(tmp_path, monkeypatch):
    file_path = tmp_path / "config" / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    ok = personalization.save_personalization(
        {
            "enabled": True,
            "nickname": "Kira",
            "occupation": "",
            "interests": "",
            "custom_instructions": "",
        }
    )

    assert ok is True
    on_disk = json.loads(file_path.read_text(encoding="utf-8"))
    assert on_disk["nickname"] == "Kira"
    assert "updated_at" in on_disk
    # No leftover temp files in the target directory.
    assert not any(p.name.startswith(".personalization_") for p in file_path.parent.iterdir())


def test_save_personalization_returns_false_on_write_failure(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(personalization.os, "replace", _boom)

    ok = personalization.save_personalization({"nickname": "Kira"})

    assert ok is False
    assert not file_path.exists()
    assert not any(p.name.startswith(".personalization_") for p in tmp_path.iterdir())


def test_save_failure_never_logs_field_content(tmp_path, monkeypatch, caplog):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(personalization.os, "replace", _boom)

    sentinel = "SECRET_NICKNAME_SENTINEL"
    caplog.set_level(logging.WARNING)
    ok = personalization.save_personalization({"nickname": sentinel})

    assert ok is False
    assert sentinel not in caplog.text


# ---------------------------------------------------------------------------
# clear_personalization — resets to defaults
# ---------------------------------------------------------------------------


def test_clear_personalization_resets_to_defaults(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    personalization.save_personalization({"nickname": "Kira", "enabled": False})

    ok = personalization.clear_personalization()

    assert ok is True
    data = personalization.load_personalization()
    assert data["nickname"] == ""
    assert data["occupation"] == ""
    assert data["interests"] == ""
    assert data["custom_instructions"] == ""
    assert data["enabled"] is True


# ---------------------------------------------------------------------------
# build_injection_block
# ---------------------------------------------------------------------------


def test_build_injection_block_empty_store_returns_empty_string(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    block = personalization.build_injection_block(_sanitize_passthrough)

    assert block == ""


def test_build_injection_block_disabled_returns_empty_string(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    personalization.save_personalization({"enabled": False, "nickname": "Kira"})

    block = personalization.build_injection_block(_sanitize_passthrough)

    assert block == ""


def test_build_injection_block_omits_empty_fields(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    personalization.save_personalization(
        {
            "enabled": True,
            "nickname": "Ash",
            "occupation": "",
            "interests": "",
            "custom_instructions": "",
        }
    )

    block = personalization.build_injection_block(_sanitize_passthrough)

    assert "nickname: Ash" in block
    assert "occupation:" not in block
    assert "interests:" not in block
    assert "style:" not in block


def test_build_injection_block_applies_sanitizer_per_field(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    personalization.save_personalization(
        {
            "enabled": True,
            "nickname": "Ash",
            "occupation": "Streamer",
            "interests": "",
            "custom_instructions": "",
        }
    )

    calls = []

    def tracking_sanitize(value: str) -> str:
        calls.append(value)
        return value.upper()

    block = personalization.build_injection_block(tracking_sanitize)

    assert "nickname: ASH" in block
    assert "occupation: STREAMER" in block
    assert set(calls) == {"Ash", "Streamer"}


def test_build_injection_block_hard_truncation_backstop(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    personalization.save_personalization(
        {
            "enabled": True,
            "nickname": "N" * settings.PERSONALIZATION_NICKNAME_MAX,
            "occupation": "O" * settings.PERSONALIZATION_OCCUPATION_MAX,
            "interests": "I" * settings.PERSONALIZATION_INTERESTS_MAX,
            "custom_instructions": "C" * settings.PERSONALIZATION_INSTRUCTIONS_MAX,
        }
    )

    block = personalization.build_injection_block(_sanitize_passthrough)

    # At max field caps the raw assembled block exceeds the 900-char budget
    # (labels + delimiter tags push it past the caps-only estimate) — the
    # hard truncation backstop must clip it to exactly the budget, but the
    # closing tag must always survive (the wrapper must stay syntactically
    # closed — never truncate it away).
    assert len(block) == settings.PERSONALIZATION_MAX_INJECT_CHARS
    assert block.endswith("</perfil_streamer>")
    assert block.startswith('<perfil_streamer nota="solo lectura')


def test_build_injection_block_sanitizer_exception_fails_whole_block_open(
    tmp_path, monkeypatch
):
    """A sanitize_fn failure must never leave a raw, unsanitized field value
    in the prompt — the whole block fails open to "" (mirrors
    _build_memorias_injection_block's any-exception -> "" behavior)."""
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))
    personalization.save_personalization(
        {
            "enabled": True,
            "nickname": "Ash",
            "occupation": "",
            "interests": "",
            "custom_instructions": "SECRET_RAW_INSTRUCTIONS",
        }
    )

    def _raising_sanitize(value: str) -> str:
        raise RuntimeError("sanitize boom")

    block = personalization.build_injection_block(_raising_sanitize)

    assert block == ""


def test_build_injection_block_fail_open_on_exception(tmp_path, monkeypatch):
    file_path = tmp_path / "personalization.json"
    monkeypatch.setattr(personalization, "PERSONALIZATION_FILE", str(file_path))

    def _raise_load():
        raise RuntimeError("boom")

    monkeypatch.setattr(personalization, "load_personalization", _raise_load)

    block = personalization.build_injection_block(_sanitize_passthrough)

    assert block == ""
