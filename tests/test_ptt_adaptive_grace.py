"""Adaptive PTT grace (ptt-adaptive-grace-20261013). Numbers only, no transcript."""
import json
import logging

import pytest

import opencohost.api.ptt_session as pm
from opencohost.api.ptt_session import PttController, resolve_ptt_grace


def _cfg(tmp_path, payload):
    p = tmp_path / "config.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return str(p)


def test_override_wins(tmp_path):
    cfg = _cfg(tmp_path, {"silence_timeout": 1.3})
    g, src, st = resolve_ptt_grace(environ={"OPENCOHOST_PTT_GRACE_S": "2.5"}, config_path=cfg)
    assert (g, src) == (2.5, "override")


def test_config_1_3_gives_2_1(tmp_path):
    g, src, st = resolve_ptt_grace(environ={}, config_path=_cfg(tmp_path, {"silence_timeout": 1.3}))
    assert g == pytest.approx(2.1) and src == "liveaudio_config" and st == pytest.approx(1.3)


def test_margin_env(tmp_path):
    g, _, _ = resolve_ptt_grace(
        environ={"OPENCOHOST_PTT_ASR_MARGIN_S": "0.4"},
        config_path=_cfg(tmp_path, {"silence_timeout": 1.3}),
    )
    assert g == pytest.approx(1.7)


def test_missing_config_default(tmp_path):
    g, src, st = resolve_ptt_grace(environ={}, config_path=str(tmp_path / "nope.json"))
    assert src == "default" and st == pytest.approx(0.8) and g == pytest.approx(1.6)


@pytest.mark.parametrize("payload", ["{bad", [1], {"silence_timeout": "x"},
                                     {"silence_timeout": 9.0}, {"silence_timeout": 0.1},
                                     {"silence_timeout": True}])
def test_invalid_config_default(tmp_path, payload):
    g, src, st = resolve_ptt_grace(environ={}, config_path=_cfg(tmp_path, payload))
    assert src == "default" and st == pytest.approx(0.8)


def test_floor_and_ceiling(tmp_path):
    g, _, _ = resolve_ptt_grace(environ={}, config_path=_cfg(tmp_path, {"silence_timeout": 0.3}))
    assert g == pytest.approx(1.2)  # 1.1 floored to current default
    g, _, _ = resolve_ptt_grace(
        environ={"OPENCOHOST_PTT_ASR_MARGIN_S": "5"},
        config_path=_cfg(tmp_path, {"silence_timeout": 2.0}),
    )
    assert g == pytest.approx(3.0)


def test_invalid_override_ignored(tmp_path):
    g, src, _ = resolve_ptt_grace(
        environ={"OPENCOHOST_PTT_GRACE_S": "abc"},
        config_path=_cfg(tmp_path, {"silence_timeout": 1.3}),
    )
    assert src == "liveaudio_config"


def test_controller_session_uses_effective_grace(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        pm, "resolve_ptt_grace", lambda *a, **k: (2.1, "liveaudio_config", 1.3)
    )
    seen = {}

    class _S:
        session_id = "s"
        state = "listening"

        def __init__(self, uri, **kw):
            seen.update(kw)

        def start(self):
            pass

    c = PttController("ws://127.0.0.1:1", lambda *a, **k: None, None, session_factory=_S)
    with caplog.at_level(logging.INFO):
        try:
            c.start()
        except Exception:
            pass
    assert seen.get("grace") == 2.1
    # Must reach the session log (OpenCohost logger tree), not a stdlib logger.
    assert any("[PTT] grace effective=2.10s source=liveaudio_config silence_timeout=1.30" in r.getMessage()
               for r in caplog.records
               if r.name == "OpenCohost" or r.name.startswith("OpenCohost."))


# ── configurable grace (ptt-live-dictation-20261125 T2) ────────────────────

from opencohost.config import settings as _settings


def test_grace_settings_defaults_and_round_trip(tmp_path):
    path = str(tmp_path / "ptt_settings.json")
    assert _settings.load_ptt_grace_mode(path) == "auto"
    assert _settings.load_ptt_grace_s(path) == pytest.approx(1.2)
    _settings.save_ptt_hotkey("F8", path)
    _settings.save_ptt_grace_mode("custom", path)
    _settings.save_ptt_grace_s(0.9, path)
    assert _settings.load_ptt_grace_mode(path) == "custom"
    assert _settings.load_ptt_grace_s(path) == pytest.approx(0.9)
    assert _settings.load_ptt_hotkey(path) == "F8"


def test_grace_settings_reject_invalid(tmp_path):
    path = str(tmp_path / "ptt_settings.json")
    with pytest.raises(ValueError):
        _settings.save_ptt_grace_mode("turbo", path)
    for bad in (0.2, 3.1, True, "1", float("nan")):
        with pytest.raises(ValueError):
            _settings.save_ptt_grace_s(bad, path)


def test_grace_settings_ignore_hand_edited_garbage(tmp_path):
    p = tmp_path / "ptt_settings.json"
    p.write_text('{"grace_mode": "warp", "grace_s": 99}', encoding="utf-8")
    assert _settings.load_ptt_grace_mode(str(p)) == "auto"
    assert _settings.load_ptt_grace_s(str(p)) == pytest.approx(1.2)


def test_fast_mode_uses_the_preset(tmp_path):
    g, src, _ = resolve_ptt_grace(
        environ={}, config_path=str(tmp_path / "nope.json"), grace_mode="fast"
    )
    assert (g, src) == (pytest.approx(0.5), "fast_preset")


def test_custom_mode_uses_grace_s(tmp_path):
    g, src, _ = resolve_ptt_grace(
        environ={}, config_path=str(tmp_path / "nope.json"), grace_mode="custom", grace_s=0.7
    )
    assert (g, src) == (pytest.approx(0.7), "custom")


def test_env_override_beats_fast_and_custom(tmp_path):
    for mode in ("fast", "custom"):
        g, src, _ = resolve_ptt_grace(
            environ={"OPENCOHOST_PTT_GRACE_S": "2.5"},
            config_path=str(tmp_path / "nope.json"),
            grace_mode=mode,
            grace_s=0.7,
        )
        assert (g, src) == (2.5, "override")


def _capture_controller(**kw):
    seen = {}

    class _S:
        session_id = "s"
        state = "listening"

        def __init__(self, uri, **k):
            seen.update(k)

        def start(self):
            pass

    c = PttController("ws://127.0.0.1:1", lambda *a, **k: None, None, session_factory=_S, **kw)
    return c, seen


def test_controller_fast_mode_sets_preset_timings(monkeypatch, caplog):
    monkeypatch.delenv("OPENCOHOST_PTT_GRACE_S", raising=False)
    c, seen = _capture_controller(grace_mode="fast")
    with caplog.at_level(logging.INFO):
        c.start()
    assert seen["grace"] == pytest.approx(0.5)
    assert seen["tail_extension"] == pytest.approx(0.3)
    assert seen["late_drain"] == pytest.approx(0.15)
    assert any("source=fast_preset" in r.getMessage() for r in caplog.records)


def test_controller_custom_mode_and_live_apply(monkeypatch, caplog):
    monkeypatch.delenv("OPENCOHOST_PTT_GRACE_S", raising=False)
    c, seen = _capture_controller()
    c.set_grace("custom", 0.7)
    assert (c.grace_mode, c.grace_s) == ("custom", pytest.approx(0.7))
    with caplog.at_level(logging.INFO):
        c.start()
    assert seen["grace"] == pytest.approx(0.7)
    assert seen["tail_extension"] == pytest.approx(0.7)  # min(0.8, grace)
    assert any("source=custom" in r.getMessage() for r in caplog.records)


def test_controller_set_grace_validates():
    c, _ = _capture_controller()
    with pytest.raises(ValueError):
        c.set_grace("turbo", None)
    with pytest.raises(ValueError):
        c.set_grace("custom", 9.0)
    assert c.grace_mode == "auto"
