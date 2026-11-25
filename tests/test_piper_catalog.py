"""voice-catalog-selection-20261118 T1: Piper voice catalog (pure domain, synthetic files only).

A voice is a ``X.onnx`` model paired with ``X.onnx.json`` (Piper's own rule). Only the json is
ever read; the model bytes are never opened. No real model, no network.
"""
import json

import pytest

from opencohost.config import settings
from opencohost.core.speech import piper_catalog


def _write_voice(folder, name, config=None, *, raw_config=None):
    (folder / name).write_bytes(b"not-a-real-onnx")
    if raw_config is not None:
        (folder / (name + ".json")).write_text(raw_config, encoding="utf-8")
    elif config is not None:
        (folder / (name + ".json")).write_text(json.dumps(config), encoding="utf-8")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Isolated saved-folder file, default folder, env and bundled registry."""
    monkeypatch.setattr(settings, "PIPER_VOICE_DIR_FILE", str(tmp_path / "cfg" / "piper_voice_dir.json"))
    default_dir = tmp_path / "default"
    default_dir.mkdir()
    monkeypatch.setattr(piper_catalog, "_default_dir", lambda: default_dir)
    monkeypatch.delenv("OPENCOHOST_PIPER_VOICE_DIR", raising=False)
    monkeypatch.setattr(settings, "PIPER_VOICES", {})
    return tmp_path


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "voices"
    d.mkdir()
    _write_voice(
        d,
        "es_AR-multi.onnx",
        {
            "language": {"code": "es_ES"},
            "num_speakers": 2,
            "speaker_id_map": {"M": 0, "F": 1},
            "audio": {"sample_rate": 22050},
        },
    )
    _write_voice(d, "foo.onnx.onnx", {"language": {"code": "en-us"}, "audio": {"sample_rate": 16000}})
    _write_voice(d, "orphan.onnx")  # no json
    _write_voice(d, "broken.onnx", raw_config="{not json")
    (d / "notes.txt").write_text("ignore me")
    return d


def _by_id(entries):
    return {e.id: e for e in entries}


def test_scan_pairs_model_with_json_and_reads_metadata(folder):
    multi = _by_id(piper_catalog.scan_folder(folder))["es_AR-multi.onnx"]
    assert multi.status == "ok" and multi.reason is None
    assert multi.lang == "es"
    assert multi.num_speakers == 2
    assert [tuple(s) for s in multi.speakers] == [("M", 0), ("F", 1)]
    assert multi.sample_rate == 22050
    assert multi.bundled is False


def test_scan_handles_double_onnx_extension_and_dash_language(folder):
    foo = _by_id(piper_catalog.scan_folder(folder))["foo.onnx.onnx"]
    assert foo.status == "ok"
    assert foo.lang == "en"
    assert foo.num_speakers == 1
    assert foo.speakers == ()
    assert foo.sample_rate == 16000


def test_scan_marks_missing_and_invalid_config_incomplete(folder):
    voices = _by_id(piper_catalog.scan_folder(folder))
    assert (voices["orphan.onnx"].status, voices["orphan.onnx"].reason) == ("incomplete", "missing_config")
    assert (voices["broken.onnx"].status, voices["broken.onnx"].reason) == ("incomplete", "invalid_config")


def test_scan_ignores_non_model_files_and_is_sorted(folder):
    ids = [e.id for e in piper_catalog.scan_folder(folder)]
    assert "notes.txt" not in ids and "es_AR-multi.onnx.json" not in ids
    assert ids == sorted(ids)


def test_scan_does_not_recurse(folder):
    sub = folder / "nested"
    sub.mkdir()
    _write_voice(sub, "deep.onnx", {"language": {"code": "es_ES"}})
    assert "deep.onnx" not in _by_id(piper_catalog.scan_folder(folder))


def test_scan_never_opens_the_model(folder, monkeypatch):
    real_open = open
    opened = []

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    piper_catalog.scan_folder(folder)
    assert not [p for p in opened if p.endswith(".onnx")]


def test_scan_unreadable_folder_is_empty(tmp_path):
    assert piper_catalog.scan_folder(tmp_path / "missing") == []


@pytest.mark.parametrize(
    "config, expected_lang",
    [
        ({"language": {"code": "es_ES"}}, "es"),
        ({"language": {"code": "en-us"}}, "en"),
        ({"language": {"code": "PT_br"}}, "pt"),
        ({"language": {"family": "de"}}, "de"),
        ({"language": {}}, None),
        ({}, None),
        ({"language": "es_ES"}, None),
    ],
)
def test_language_normalization(tmp_path, config, expected_lang):
    _write_voice(tmp_path, "v.onnx", config)
    assert piper_catalog.scan_folder(tmp_path)[0].lang == expected_lang


def test_bad_metadata_values_fall_back_to_defaults(tmp_path):
    _write_voice(
        tmp_path,
        "v.onnx",
        {"num_speakers": "many", "speaker_id_map": ["x"], "audio": {"sample_rate": "fast"}},
    )
    v = piper_catalog.scan_folder(tmp_path)[0]
    assert (v.num_speakers, v.speakers, v.sample_rate) == (1, (), None)


def test_multi_speaker_without_map_gets_numbered_speakers(tmp_path):
    _write_voice(tmp_path, "v.onnx", {"num_speakers": 3, "speaker_id_map": {}})
    v = piper_catalog.scan_folder(tmp_path)[0]
    assert [tuple(s) for s in v.speakers] == [("0", 0), ("1", 1), ("2", 2)]


def test_oversized_config_is_invalid(tmp_path):
    _write_voice(tmp_path, "v.onnx", raw_config=" " * (2 * 1024 * 1024))
    assert piper_catalog.scan_folder(tmp_path)[0].reason == "invalid_config"


@pytest.mark.parametrize(
    "name",
    ["../x.onnx", "..\\x.onnx", "C:x.onnx", "/x.onnx", "x.onnx:evil", "a\x00.onnx", "x.txt", ""],
)
def test_voice_path_rejects_unsafe_ids(isolated, name):
    with pytest.raises(piper_catalog.PiperVoiceError) as exc:
        piper_catalog.voice_path(name)
    assert exc.value.code == "invalid_voice_id"


def test_voice_path_resolves_inside_folder_only(isolated, folder):
    piper_catalog.save_voice_dir(folder)
    assert piper_catalog.voice_path("es_AR-multi.onnx") == (folder / "es_AR-multi.onnx").resolve()
    with pytest.raises(piper_catalog.PiperVoiceError) as exc:
        piper_catalog.voice_path("absent.onnx")
    assert exc.value.code == "voice_not_found"


def test_voice_path_refuses_symlink_escape(isolated, folder, tmp_path):
    outside = tmp_path / "outside.onnx"
    outside.write_bytes(b"x")
    try:
        (folder / "link.onnx").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted")
    piper_catalog.save_voice_dir(folder)
    with pytest.raises(piper_catalog.PiperVoiceError):
        piper_catalog.voice_path("link.onnx")
    assert "link.onnx" not in _by_id(piper_catalog.scan_folder(folder))


def test_resolution_order_saved_env_default(isolated, folder, monkeypatch, tmp_path):
    default_dir = tmp_path / "default"
    assert piper_catalog.resolve_voice_dir() == (default_dir, "default")
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    monkeypatch.setenv("OPENCOHOST_PIPER_VOICE_DIR", str(env_dir))
    assert piper_catalog.resolve_voice_dir() == (env_dir, "env")
    piper_catalog.save_voice_dir(folder)
    assert piper_catalog.resolve_voice_dir() == (folder.resolve(), "saved")
    piper_catalog.clear_voice_dir()
    assert piper_catalog.resolve_voice_dir() == (env_dir, "env")


def test_saved_folder_that_vanished_falls_back(isolated, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    piper_catalog.save_voice_dir(gone)
    gone.rmdir()
    assert piper_catalog.resolve_voice_dir()[1] == "default"


def test_corrupt_saved_file_falls_back(isolated, tmp_path):
    cfg = tmp_path / "cfg" / "piper_voice_dir.json"
    cfg.parent.mkdir()
    cfg.write_text("{broken")
    assert piper_catalog.resolve_voice_dir()[1] == "default"


def test_save_voice_dir_rejects_invalid_path(isolated):
    with pytest.raises(piper_catalog.PiperFolderError) as exc:
        piper_catalog.save_voice_dir("relative/path")
    assert exc.value.code == "path_not_absolute"


def test_bundled_voices_listed_first_and_win_by_id(isolated, folder, monkeypatch, tmp_path):
    bundled_dir = tmp_path / "bundled"
    bundled_dir.mkdir()
    _write_voice(bundled_dir, "es_AR-multi.onnx", {"language": {"code": "es_MX"}, "audio": {"sample_rate": 22050}})
    monkeypatch.setattr(
        settings,
        "PIPER_VOICES",
        {
            "neutral": {"label": "Neutral", "file": "es_AR-multi.onnx", "lang": "es"},
            "english": {"label": "English", "file": "en_missing.onnx", "lang": "en"},
        },
    )
    monkeypatch.setattr(settings, "_resolve_piper_voice_file", lambda f: str(bundled_dir / f))
    piper_catalog.save_voice_dir(folder)
    voices = piper_catalog.list_voices()
    assert voices[0].id == "es_AR-multi.onnx" and voices[0].bundled is True
    assert voices[0].label == "Neutral" and voices[0].lang == "es"
    assert [v.id for v in voices].count("es_AR-multi.onnx") == 1  # folder copy loses
    assert "en_missing.onnx" not in [v.id for v in voices]  # unresolved bundled file is not listed


def test_list_voices_lang_filter_keeps_unknown_language(isolated, folder):
    piper_catalog.save_voice_dir(folder)
    ids = {v.id for v in piper_catalog.list_voices("es")}
    assert "es_AR-multi.onnx" in ids
    assert "foo.onnx.onnx" not in ids
    assert "orphan.onnx" in ids  # language unknown: kept, shown as incomplete


def test_count_voices(folder):
    # Pairs only: the orphan (no json) is not counted; the broken json still is (never parsed).
    assert piper_catalog.count_voices(folder) == 3
    assert piper_catalog.count_voices(folder / "nope") == 0


def test_count_voices_never_opens_any_file(folder, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("count_voices must not open files")

    monkeypatch.setattr("builtins.open", forbidden)
    assert piper_catalog.count_voices(folder) == 3


def test_scan_is_capped_and_deterministic(tmp_path, monkeypatch):
    # The cap counts directory entries (models AND sidecars), taken in name order.
    monkeypatch.setattr(piper_catalog, "_MAX_SCAN_ENTRIES", 4)
    d = tmp_path / "capped"
    d.mkdir()
    for i in range(10):
        _write_voice(d, f"v{i}.onnx", {"language": {"code": "en"}})
    ids = [v.id for v in piper_catalog.scan_folder(d)]
    assert ids == ["v0.onnx", "v1.onnx"]
    assert ids == [v.id for v in piper_catalog.scan_folder(d)]
    assert piper_catalog.count_voices(d) == 2


def test_sidecar_outside_folder_is_not_parsed(tmp_path):
    root = tmp_path / "voices"
    root.mkdir()
    (root / "v.onnx").write_bytes(b"x")
    # The sidecar resolves outside the folder (confinement check refuses it).
    monkeypatch_target = piper_catalog._confined

    def refuse_sidecar(r, name):
        return None if name.endswith(".json") else monkeypatch_target(r, name)

    (root / "v.onnx.json").write_text(json.dumps({"language": {"code": "en"}}))
    import unittest.mock as mock

    with mock.patch.object(piper_catalog, "_confined", refuse_sidecar):
        voice = piper_catalog.scan_folder(root)[0]
    assert voice.status == "incomplete" and voice.reason == "missing_config"


def test_config_read_is_hard_capped_not_stat_based(tmp_path, monkeypatch):
    _write_voice(tmp_path, "v.onnx", {"language": {"code": "en"}})
    reads = []
    real_open = open

    class Spy:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()

        def read(self, n=-1):
            reads.append(n)
            return self._fh.read(n)

    def spy_open(path, *a, **k):
        fh = real_open(path, *a, **k)
        return Spy(fh) if str(path).endswith(".json") else fh

    monkeypatch.setattr("builtins.open", spy_open)
    voice = piper_catalog.scan_folder(tmp_path)[0]
    assert voice.status == "ok"
    assert reads == [piper_catalog._CONFIG_MAX_BYTES + 1]


def test_to_dict_shape(folder):
    d = _by_id(piper_catalog.scan_folder(folder))["es_AR-multi.onnx"].to_dict()
    assert set(d) == {"id", "label", "lang", "num_speakers", "speakers", "sample_rate", "bundled", "status", "reason"}
    assert d["speakers"] == [{"name": "M", "id": 0}, {"name": "F", "id": 1}]
    assert d["label"] == "es_AR-multi"


def test_default_piper_voices_labels_contain_no_emojis():
    import re

    emoji_re = re.compile(r"[\U0001F1E0-\U0001FAFF\u2600-\u27BF\uFE0F]")
    assert set(settings.PIPER_VOICES) >= {"neutral", "english"}
    for key, entry in settings.PIPER_VOICES.items():
        label = entry["label"]
        assert not emoji_re.search(label), f"PIPER_VOICES[{key!r}] label has emoji: {label!r}"
    assert settings.PIPER_VOICES["neutral"]["label"] == "Neutral"
    assert settings.PIPER_VOICES["english"]["label"] == "English"
