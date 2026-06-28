"""Physical VK resolution + GetAsyncKeyState probe tests — REAL pynput.

Per proposal D4 (ptt_keyup_reconcile_20260627): the `pynput_mocks` fixture in
`tests/test_ptt_manager.py` replaces pynput via `sys.modules`, so
`keyboard.Key.f8.value.vk` becomes a MagicMock rather than an int. VK-resolution
and signed-short (E1) tests therefore CANNOT use that fixture — they must run
against real pynput (installed). This module imports the real package and never
touches the sys.modules mock.

Scope: Phase 1 deliverables only —
`_PTT_MOUSE_VK_MAP`, `_resolve_get_async_key_state`, `_GET_ASYNC_KEY_STATE`,
`_is_vk_physically_down`, `_resolve_target_vk`. The reconcile step is exercised
on the patched boolean seam in `tests/test_ptt_manager.py` (Phase 2).
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest

# Real pynput — explicitly NOT mocked here (the whole point of this module).
from pynput import keyboard, mouse


HOTKEY_LIST = [
    "F1", "F2", "F3", "F4", "F5", "F6",
    "F7", "F8", "F9", "F10", "F11", "F12",
    "Mouse4", "Mouse5",
    "ScrollLock", "Insert", "Pause",
]


@pytest.fixture
def ptt_module():
    """Import the real-pynput build of opencohost.ui.ptt_manager.

    Pops any cached (possibly mock-built) copy first so this module is
    order-independent relative to tests/test_ptt_manager.py.
    """
    sys.modules.pop("opencohost.ui.ptt_manager", None)
    return importlib.import_module("opencohost.ui.ptt_manager")


@pytest.fixture
def make_manager(ptt_module, tmp_path):
    """Factory building a PTTManager backed by a temp (missing) config file."""
    def _make(hotkey: str = "F10"):
        return ptt_module.PTTManager(
            config_file=str(tmp_path / "ptt_settings.json"),
            default_hotkey=hotkey,
            hotkey_list=list(HOTKEY_LIST),
            logger=MagicMock(),
        )
    return _make


class TestVKResolution:
    """VK is keyed off the stored pynput object, never the display label."""

    def test_mouse_target_resolves_correct_vk(self, ptt_module, make_manager):
        # Direct map: x1 -> VK_XBUTTON1 (0x05), x2 -> VK_XBUTTON2 (0x06).
        assert ptt_module._PTT_MOUSE_VK_MAP[mouse.Button.x1] == 0x05
        assert ptt_module._PTT_MOUSE_VK_MAP[mouse.Button.x2] == 0x06

        m = make_manager()
        m._target = ("mouse", mouse.Button.x1)
        assert m._resolve_target_vk() == 0x05
        m._target = ("mouse", mouse.Button.x2)
        assert m._resolve_target_vk() == 0x06

        # Inverted DISPLAY chain must still land on the right physical VK,
        # because resolution keys off the pynput Button object, not the label:
        #   Mouse4 -> Button.x2 -> 0x06 ;  Mouse5 -> Button.x1 -> 0x05
        m._target = m.build_pynput_target("Mouse4")
        assert m._resolve_target_vk() == 0x06
        m._target = m.build_pynput_target("Mouse5")
        assert m._resolve_target_vk() == 0x05

    def test_keyboard_target_resolves_vk(self, make_manager):
        m = make_manager()
        m._target = ("keyboard", keyboard.Key.f8)
        assert m._resolve_target_vk() == 0x77
        m._target = ("keyboard", keyboard.Key.pause)
        assert m._resolve_target_vk() == 0x13

    def test_unset_target_resolves_none(self, make_manager):
        m = make_manager()
        m._target = None
        assert m._resolve_target_vk() is None


class TestSignedShortPredicate:
    """E1 trap-pin: a held key returns a NEGATIVE c_short; & 0x8000 is correct."""

    def test_signed_short_high_bit_means_down(self, ptt_module, monkeypatch):
        held = -32768  # signed c_short value Windows returns for a held key
        # The correct predicate reports DOWN; a naive `> 0` would be WRONG.
        assert bool(held & 0x8000) is True
        assert (held > 0) is False

        # Drive the same value through the real probe seam.
        monkeypatch.setattr(ptt_module, "_GET_ASYNC_KEY_STATE", lambda vk: -32768)
        assert ptt_module._is_vk_physically_down(0x77) is True
        monkeypatch.setattr(ptt_module, "_GET_ASYNC_KEY_STATE", lambda vk: 0)
        assert ptt_module._is_vk_physically_down(0x77) is False


class TestProbeFailOpen:
    """Any unprobeable condition yields None so the reconcile is a no-op."""

    def test_failopen_when_probe_unavailable(self, ptt_module, monkeypatch):
        # Probe function unavailable (non-Windows / missing windll).
        monkeypatch.setattr(ptt_module, "_GET_ASYNC_KEY_STATE", None)
        assert ptt_module._is_vk_physically_down(0x77) is None

        # VK unresolved even when the probe exists.
        monkeypatch.setattr(ptt_module, "_GET_ASYNC_KEY_STATE", lambda vk: -32768)
        assert ptt_module._is_vk_physically_down(None) is None

        # ctypes raising → swallowed → None.
        def boom(vk):
            raise OSError("simulated ctypes failure")
        monkeypatch.setattr(ptt_module, "_GET_ASYNC_KEY_STATE", boom)
        assert ptt_module._is_vk_physically_down(0x77) is None
