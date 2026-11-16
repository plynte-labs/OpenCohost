"""tts-first-chunk-boost-20261015: flag resolution and TTFA log field."""

import logging

from opencohost.config import settings


def test_first_chunk_boost_defaults_on_and_env_zero_is_the_kill_switch(monkeypatch):
    monkeypatch.delenv("OPENCOHOST_TTS_FIRST_CHUNK_BOOST", raising=False)
    assert settings._resolve_tts_first_chunk_boost() is True

    monkeypatch.setenv("OPENCOHOST_TTS_FIRST_CHUNK_BOOST", "0")
    assert settings._resolve_tts_first_chunk_boost() is False

    for still_on in ("1", "", "true", "off"):
        monkeypatch.setenv("OPENCOHOST_TTS_FIRST_CHUNK_BOOST", still_on)
        assert settings._resolve_tts_first_chunk_boost() is True, still_on


def test_module_constant_exists():
    assert isinstance(settings.TTS_FIRST_CHUNK_BOOST, bool)
