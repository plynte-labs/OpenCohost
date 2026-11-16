"""Vision settings: defaults, clamp, load/save/apply (vision-on-demand V1.5)."""
from __future__ import annotations

import json

import pytest

from opencohost.config import settings
from opencohost.config import vision_settings as vs


@pytest.fixture(autouse=True)
def _restore_settings():
    saved = vs.current()
    yield
    vs.apply(saved)


def test_code_defaults_match_the_plan():
    assert settings.VISION_ENABLED is False
    assert settings.VISION_LOOK_ON_PTT is False
    assert settings.VISION_CAPTURE_DELAY_S == 0.0  # V1.11: the burst replaces the alt-tab delay
    assert settings.VISION_FRAMES == 3
    assert settings.VISION_FRAME_INTERVAL_S == 1.0
    assert settings.VISION_SHOW_THUMBNAILS is True
    assert settings.VISION_RULE_GATE_ENABLED is True
    assert settings.VISION_PORT == 11435
    assert settings.VISION_MODEL == "qwen3-vl:4b-instruct"
    assert settings.VISION_DESCRIBE_TIMEOUT_S == 15.0
    assert settings.VISION_SETTINGS_FILE.endswith("vision_settings.json")


def test_current_reflects_module_attrs():
    cur = vs.current()
    assert cur == vs.VisionSettings(
        enabled=settings.VISION_ENABLED,
        look_on_ptt=settings.VISION_LOOK_ON_PTT,
        capture_delay_s=settings.VISION_CAPTURE_DELAY_S,
        rule_gate_enabled=settings.VISION_RULE_GATE_ENABLED,
        port=settings.VISION_PORT,
        model=settings.VISION_MODEL,
        describe_timeout_s=settings.VISION_DESCRIBE_TIMEOUT_S,
        frames=settings.VISION_FRAMES,
        frame_interval_s=settings.VISION_FRAME_INTERVAL_S,
        show_thumbnails=settings.VISION_SHOW_THUMBNAILS,
    )


@pytest.mark.parametrize("raw,expected", [(-1, 0.0), (0, 0.0), (2.5, 2.5), (10, 10.0), (99, 10.0)])
def test_capture_delay_is_clamped(raw, expected):
    assert vs.with_updates(vs.current(), {"capture_delay_s": raw}).capture_delay_s == expected


def test_describe_timeout_is_clamped():
    base = vs.current()
    assert vs.with_updates(base, {"describe_timeout_s": 0}).describe_timeout_s == vs.DESCRIBE_TIMEOUT_MIN_S
    assert vs.with_updates(base, {"describe_timeout_s": 999}).describe_timeout_s == vs.DESCRIBE_TIMEOUT_MAX_S


@pytest.mark.parametrize(
    "updates",
    [
        {"enabled": "yes"},
        {"enabled": 1},
        {"capture_delay_s": True},
        {"capture_delay_s": "3"},
        {"port": 80},
        {"port": 70000},
        {"port": 11435.5},
        {"model": ""},
        {"model": "bad model; rm -rf"},
        {"model": 42},
        {"unknown_key": True},
        {"frames": "3"},
        {"frames": True},
        {"frames": 2.5},
        {"frame_interval_s": "1"},
        {"frame_interval_s": False},
        {"show_thumbnails": 1},
        {"show_thumbnails": "no"},
    ],
)
def test_invalid_values_keep_the_base_value(updates):
    base = vs.current()
    assert vs.with_updates(base, updates) == base


def test_valid_updates_apply():
    base = vs.current()
    new = vs.with_updates(
        base,
        {"enabled": True, "look_on_ptt": True, "rule_gate_enabled": True,
         "port": 11500, "model": "qwen3-vl:8b", "capture_delay_s": 1.5},
    )
    assert new.enabled and new.look_on_ptt and new.rule_gate_enabled
    assert new.port == 11500
    assert new.model == "qwen3-vl:8b"
    assert new.capture_delay_s == 1.5


def test_load_missing_file_returns_base(tmp_path):
    base = vs.current()
    assert vs.load(str(tmp_path / "nope.json"), base=base) == base


def test_load_corrupt_file_fails_closed_to_base(tmp_path):
    path = tmp_path / "vision_settings.json"
    path.write_text("{not json", encoding="utf-8")
    base = vs.current()
    assert vs.load(str(path), base=base) == base
    assert vs.load(str(path), base=base).enabled is False


def test_load_non_dict_fails_closed(tmp_path):
    path = tmp_path / "vision_settings.json"
    path.write_text("[true]", encoding="utf-8")
    base = vs.current()
    assert vs.load(str(path), base=base) == base


def test_save_then_load_round_trips(tmp_path):
    path = str(tmp_path / "config" / "vision_settings.json")
    wanted = vs.with_updates(vs.current(), {"enabled": True, "capture_delay_s": 0.0, "look_on_ptt": True})

    assert vs.save(wanted, path) is True
    assert vs.load(path, base=vs.current()) == wanted
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["enabled"] is True
    assert not (tmp_path / "config" / "vision_settings.json.tmp").exists()


def test_save_failure_returns_false(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert vs.save(vs.current(), str(blocker / "sub" / "vision.json")) is False


def test_load_clamps_file_values(tmp_path):
    path = tmp_path / "vision_settings.json"
    path.write_text(json.dumps({"capture_delay_s": 50, "enabled": True}), encoding="utf-8")
    loaded = vs.load(str(path), base=vs.current())
    assert loaded.capture_delay_s == 10.0
    assert loaded.enabled is True


def test_apply_sets_module_attrs():
    new = vs.with_updates(vs.current(), {"enabled": True, "capture_delay_s": 4.0})
    vs.apply(new)
    assert settings.VISION_ENABLED is True
    assert settings.VISION_CAPTURE_DELAY_S == 4.0
    assert vs.current() == new


def test_to_dict_is_json_ready():
    data = vs.current().to_dict()
    assert set(data) == {
        "enabled", "look_on_ptt", "capture_delay_s", "rule_gate_enabled",
        "port", "model", "describe_timeout_s",
        "frames", "frame_interval_s", "show_thumbnails", "cloud_direct_allowed",
        "vision_source", "vision_cloud_preset", "vision_cloud_model",
        "vision_cloud_use_chat_key",
    }
    json.dumps(data)


@pytest.mark.parametrize(
    "env,value,attr,expected",
    [
        ("OPENCOHOST_VISION_ENABLED", "1", "VISION_ENABLED", True),
        ("OPENCOHOST_VISION_CAPTURE_DELAY_S", "42", "VISION_CAPTURE_DELAY_S", 10.0),
        ("OPENCOHOST_VISION_CAPTURE_DELAY_S", "garbage", "VISION_CAPTURE_DELAY_S", 0.0),
        ("OPENCOHOST_VISION_PORT", "11440", "VISION_PORT", 11440),
        ("OPENCOHOST_VISION_MODEL", "qwen3-vl:2b", "VISION_MODEL", "qwen3-vl:2b"),
    ],
)
def test_env_overrides_resolve_at_import(monkeypatch, env, value, attr, expected):
    monkeypatch.setenv(env, value)
    assert settings._resolve_vision_defaults()[attr] == expected


# ------------------------------------------------------------ V1.11 burst keys


@pytest.mark.parametrize("raw,expected", [(0, 1), (1, 1), (2, 2), (3, 3), (9, 3), (-4, 1)])
def test_frames_is_an_int_clamped_to_1_3(raw, expected):
    assert vs.with_updates(vs.current(), {"frames": raw}).frames == expected


@pytest.mark.parametrize("raw,expected", [(0, 0.5), (0.5, 0.5), (1.25, 1.25), (3, 3.0), (10, 3.0)])
def test_frame_interval_is_clamped(raw, expected):
    assert vs.with_updates(vs.current(), {"frame_interval_s": raw}).frame_interval_s == expected


def test_show_thumbnails_toggles():
    assert vs.with_updates(vs.current(), {"show_thumbnails": False}).show_thumbnails is False


def test_a_file_saved_before_v111_loads_with_burst_defaults(tmp_path):
    path = tmp_path / "vision_settings.json"
    old = {"enabled": True, "look_on_ptt": False, "capture_delay_s": 1.5, "rule_gate_enabled": True,
           "port": 11435, "model": "qwen3-vl:4b-instruct", "describe_timeout_s": 8.0}
    path.write_text(json.dumps(old), encoding="utf-8")

    loaded = vs.load(str(path), base=vs.current())

    assert loaded.enabled is True and loaded.capture_delay_s == 1.5
    assert (loaded.frames, loaded.frame_interval_s, loaded.show_thumbnails) == (3, 1.0, True)


def test_burst_keys_round_trip(tmp_path):
    path = str(tmp_path / "v.json")
    wanted = vs.with_updates(vs.current(), {"frames": 2, "frame_interval_s": 0.75, "show_thumbnails": False})
    assert vs.save(wanted, path) is True
    assert vs.load(path, base=vs.current()) == wanted


def test_apply_sets_the_burst_attrs():
    vs.apply(vs.with_updates(vs.current(), {"frames": 1, "frame_interval_s": 2.0, "show_thumbnails": False}))
    assert settings.VISION_FRAMES == 1
    assert settings.VISION_FRAME_INTERVAL_S == 2.0
    assert settings.VISION_SHOW_THUMBNAILS is False


@pytest.mark.parametrize(
    "env,value,attr,expected",
    [
        ("OPENCOHOST_VISION_FRAMES", "2", "VISION_FRAMES", 2),
        ("OPENCOHOST_VISION_FRAMES", "7", "VISION_FRAMES", 3),
        ("OPENCOHOST_VISION_FRAMES", "x", "VISION_FRAMES", 3),
        ("OPENCOHOST_VISION_FRAME_INTERVAL_S", "0.1", "VISION_FRAME_INTERVAL_S", 0.5),
        ("OPENCOHOST_VISION_SHOW_THUMBNAILS", "0", "VISION_SHOW_THUMBNAILS", False),
    ],
)
def test_burst_env_overrides(monkeypatch, env, value, attr, expected):
    monkeypatch.setenv(env, value)
    assert settings._resolve_vision_defaults()[attr] == expected
