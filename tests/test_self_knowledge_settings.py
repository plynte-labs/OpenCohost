"""self-knowledge K2: persisted toggles/identity, read live per turn."""
import json
import os

import pytest

from opencohost.config import self_knowledge_settings as sks


@pytest.fixture()
def path(tmp_path, monkeypatch):
    p = str(tmp_path / "sk.json")
    monkeypatch.setattr(sks, "SELF_KNOWLEDGE_FILE", p)
    sks.reset_cache()
    return p


def test_defaults_when_file_missing(path):
    s = sks.load_settings()
    assert s.enabled is True and s.viewers is False
    assert (s.creator, s.repository, s.description) == ("", "", "")


def test_values_are_read_live_when_the_file_changes(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"enabled": False, "viewers": True, "creator": "acme"}, f)
    s = sks.load_settings()
    assert s.enabled is False and s.viewers is True and s.creator == "acme"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"enabled": True}, f)
    os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
    assert sks.load_settings().enabled is True


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '{"enabled": "yes", "viewers": 1}'])
def test_corrupt_or_wrongly_typed_file_falls_back_to_defaults(path, raw):
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
    s = sks.load_settings()
    assert s.enabled is True and s.viewers is False


def test_identity_strings_are_clamped_and_single_line(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"creator": "a\nb\x00" + "x" * 500, "description": "d" * 5000}, f)
    s = sks.load_settings()
    assert "\n" not in s.creator and "\x00" not in s.creator
    assert len(s.creator) <= sks.CREATOR_MAX and len(s.description) <= sks.DESCRIPTION_MAX
