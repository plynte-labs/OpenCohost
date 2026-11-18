"""voice-catalog-selection-20261118 T3: per-locale Piper voice persistence (piper_voice.json).

Format: ``{"by_locale": {"es": {"voice": id_or_key, "speaker_id": n|null}, "en": {...}}}``.
The old single-key format ``{"piper_voice": "english"}`` migrates on read into its registry lang slot.
"""
import json

import pytest

from opencohost.config import settings


@pytest.fixture
def vfile(tmp_path):
    return str(tmp_path / "piper_voice.json")


def _raw(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_round_trip_one_locale(vfile):
    assert settings.save_piper_voice_for_locale("es", "my_voice.onnx", 3, config_file=vfile) is True
    assert settings.load_piper_voice_for_locale("es", config_file=vfile) == {"voice": "my_voice.onnx", "speaker_id": 3}
    assert settings.load_piper_voice_for_locale("en", config_file=vfile) is None


def test_saving_one_locale_does_not_clobber_the_other(vfile):
    settings.save_piper_voice_for_locale("es", "a.onnx", None, config_file=vfile)
    settings.save_piper_voice_for_locale("en", "b.onnx", 1, config_file=vfile)
    settings.save_piper_voice_for_locale("es", "c.onnx", 2, config_file=vfile)
    assert settings.load_piper_voice_for_locale("en", config_file=vfile) == {"voice": "b.onnx", "speaker_id": 1}
    assert settings.load_piper_voice_for_locale("es", config_file=vfile) == {"voice": "c.onnx", "speaker_id": 2}
    assert set(_raw(vfile)["by_locale"]) == {"es", "en"}


def test_locale_code_is_normalized_to_primary_subtag(vfile):
    settings.save_piper_voice_for_locale("en-US", "b.onnx", None, config_file=vfile)
    assert settings.load_piper_voice_for_locale("EN", config_file=vfile)["voice"] == "b.onnx"


@pytest.mark.parametrize("legacy_key, slot", [("english", "en"), ("neutral", "es")])
def test_legacy_single_key_migrates_to_its_registry_lang_slot(vfile, legacy_key, slot):
    with open(vfile, "w", encoding="utf-8") as fh:
        json.dump({"piper_voice": legacy_key}, fh)
    assert settings.load_piper_voice_for_locale(slot, config_file=vfile) == {"voice": legacy_key, "speaker_id": None}
    other = "es" if slot == "en" else "en"
    assert settings.load_piper_voice_for_locale(other, config_file=vfile) is None


def test_saving_after_legacy_read_keeps_the_migrated_slot(vfile):
    with open(vfile, "w", encoding="utf-8") as fh:
        json.dump({"piper_voice": "english"}, fh)
    settings.save_piper_voice_for_locale("es", "a.onnx", None, config_file=vfile)
    data = _raw(vfile)
    assert "piper_voice" not in data
    assert data["by_locale"]["en"] == {"voice": "english", "speaker_id": None}
    assert data["by_locale"]["es"]["voice"] == "a.onnx"


@pytest.mark.parametrize(
    "payload",
    ["{not json", "[]", '{"by_locale": []}', '{"piper_voice": "ghost"}', '{"by_locale": {"es": {"voice": 5}}}'],
)
def test_corrupt_or_unknown_content_reads_as_empty(vfile, payload):
    with open(vfile, "w", encoding="utf-8") as fh:
        fh.write(payload)
    assert settings.load_piper_voice_for_locale("es", config_file=vfile) is None


def test_bad_speaker_value_is_dropped_not_the_voice(vfile):
    with open(vfile, "w", encoding="utf-8") as fh:
        json.dump({"by_locale": {"es": {"voice": "a.onnx", "speaker_id": "x"}, "en": {"voice": "b.onnx", "speaker_id": -1}}}, fh)
    assert settings.load_piper_voice_for_locale("es", config_file=vfile) == {"voice": "a.onnx", "speaker_id": None}
    assert settings.load_piper_voice_for_locale("en", config_file=vfile) == {"voice": "b.onnx", "speaker_id": None}


def test_unusable_voice_ids_are_refused_on_save(vfile):
    assert settings.save_piper_voice_for_locale("es", "", None, config_file=vfile) is False
    assert settings.save_piper_voice_for_locale("es", "..\\x.onnx", None, config_file=vfile) is False
    assert settings.save_piper_voice_for_locale("", "a.onnx", None, config_file=vfile) is False


def test_write_failure_keeps_previous_file_and_reports_false(vfile, monkeypatch):
    settings.save_piper_voice_for_locale("es", "a.onnx", None, config_file=vfile)
    before = open(vfile, encoding="utf-8").read()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(settings, "atomic_write_text", boom)
    assert settings.save_piper_voice_for_locale("es", "z.onnx", None, config_file=vfile) is False
    assert open(vfile, encoding="utf-8").read() == before


def test_fill_missing_slot_never_overwrites(vfile):
    settings.save_piper_voice_for_locale("en", "mine.onnx", 4, config_file=vfile)
    assert settings.fill_piper_voice_for_locale("en", config_file=vfile) is False
    assert settings.load_piper_voice_for_locale("en", config_file=vfile) == {"voice": "mine.onnx", "speaker_id": 4}
    assert settings.fill_piper_voice_for_locale("es", config_file=vfile) is True
    assert settings.load_piper_voice_for_locale("es", config_file=vfile) == {"voice": "neutral", "speaker_id": None}


def test_legacy_save_and_load_still_work_for_registry_keys(vfile):
    settings.save_piper_voice("english", config_file=vfile)
    assert settings.load_piper_voice(config_file=vfile, default="english") == "english"
    settings.save_piper_voice("neutral", config_file=vfile)
    # the legacy writer targets the key's own language slot, so it cannot clobber the other locale
    assert settings.load_piper_voice_for_locale("en", config_file=vfile)["voice"] == "english"
    assert settings.load_piper_voice(config_file=vfile, default="neutral") == "neutral"
    assert settings.load_piper_voice(config_file=vfile, default="english") == "english"


def test_concurrent_saves_of_different_locales_never_lose_an_update(vfile, monkeypatch):
    """The read-modify-write is serialized: a slow read cannot make one save drop the other."""
    import threading
    import time

    real_read = settings._read_piper_voices_by_locale

    def slow_read(path):
        data = real_read(path)
        time.sleep(0.05)  # widen the race window between read and write
        return data

    monkeypatch.setattr(settings, "_read_piper_voices_by_locale", slow_read)
    threads = [
        threading.Thread(target=settings.save_piper_voice_for_locale, args=(loc, f"{loc}.onnx", None), kwargs={"config_file": vfile})
        for loc in ("es", "en", "fr", "de")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(_raw(vfile)["by_locale"]) == {"es", "en", "fr", "de"}


@pytest.mark.parametrize("speaker, expected", [(4096, 4096), (4097, None), (10**9, None)])
def test_speaker_id_is_capped_on_save_and_read(vfile, speaker, expected):
    settings.save_piper_voice_for_locale("es", "a.onnx", speaker, config_file=vfile)
    assert settings.load_piper_voice_for_locale("es", config_file=vfile)["speaker_id"] == expected
    with open(vfile, "w", encoding="utf-8") as fh:
        json.dump({"by_locale": {"es": {"voice": "a.onnx", "speaker_id": speaker}}}, fh)
    assert settings.load_piper_voice_for_locale("es", config_file=vfile)["speaker_id"] == expected
