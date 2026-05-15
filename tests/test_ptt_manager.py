"""Comprehensive tests for PTT (Push-to-Talk) Manager.

Tests the PTTManager class (extracted from ui/app.py into ui/ptt_manager.py)
covering hotkey configuration, mapping, validation, thread safety, and pynput integration.
"""

import json
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest


# ── Mock pynput before any import of ui.ptt_manager ──
mock_keyboard_key = MagicMock()
for i in range(1, 13):
    setattr(mock_keyboard_key, f"f{i}", MagicMock())
mock_keyboard_key.scroll_lock = MagicMock()
mock_keyboard_key.insert = MagicMock()
mock_keyboard_key.pause = MagicMock()

mock_mouse_button = MagicMock()
mock_mouse_button.x1 = MagicMock()
mock_mouse_button.x2 = MagicMock()

mock_keyboard_module = MagicMock()
mock_keyboard_module.Key = mock_keyboard_key

mock_mouse_module = MagicMock()
mock_mouse_module.Button = mock_mouse_button

mock_pynput = MagicMock()
mock_pynput.keyboard = mock_keyboard_module
mock_pynput.mouse = mock_mouse_module


def _build_expected_kb_map():
    """Build expected keyboard map from mocks."""
    kb = {f"F{i}": getattr(mock_keyboard_key, f"f{i}") for i in range(1, 13)}
    kb.update({
        "ScrollLock": mock_keyboard_key.scroll_lock,
        "Insert": mock_keyboard_key.insert,
        "Pause": mock_keyboard_key.pause,
    })
    return kb


def _build_expected_mouse_map():
    """Build expected mouse map from mocks."""
    return {
        "Mouse4": mock_mouse_button.x2,
        "Mouse5": mock_mouse_button.x1,
    }


@pytest.fixture
def pynput_mocks():
    """Mock pynput module globally before importing PTTManager."""
    with patch.dict("sys.modules", {
        "pynput": mock_pynput,
        "pynput.keyboard": mock_keyboard_module,
        "pynput.mouse": mock_mouse_module,
    }):
        # Remove cached import if present
        for mod in list(sys.modules.keys()):
            if mod == "ui.ptt_manager" or mod.startswith("ui.ptt_manager."):
                del sys.modules[mod]
        yield mock_keyboard_module, mock_mouse_module


@pytest.fixture
def ptt_config_file(temp_dir):
    """Return a path to a PTT config file in a temp directory."""
    return os.path.join(temp_dir, "ptt_settings.json")


@pytest.fixture
def default_hotkey():
    """Return the default PTT hotkey."""
    return "F10"


@pytest.fixture
def hotkey_list():
    """Return the list of supported PTT hotkeys."""
    return [
        "F1", "F2", "F3", "F4", "F5", "F6",
        "F7", "F8", "F9", "F10", "F11", "F12",
        "Mouse4", "Mouse5",
        "ScrollLock", "Insert", "Pause"
    ]


@pytest.fixture
def ptt_manager_class(pynput_mocks):
    """Import and return the PTTManager class with mocked pynput."""
    from ui.ptt_manager import PTTManager
    return PTTManager


@pytest.fixture
def ptt_manager(ptt_manager_class, ptt_config_file, default_hotkey, hotkey_list, mock_logger):
    """Create a PTTManager instance with temp config."""
    return ptt_manager_class(
        config_file=ptt_config_file,
        default_hotkey=default_hotkey,
        hotkey_list=hotkey_list,
        logger=mock_logger,
    )


class TestHotkeyMapping:
    """Test keyboard and mouse hotkey mappings."""

    def test_kb_map_contains_f1_through_f12(self, ptt_manager):
        """All F1-F12 keys must be in the keyboard map."""
        from ui.ptt_manager import _PTT_KB_MAP
        for i in range(1, 13):
            assert f"F{i}" in _PTT_KB_MAP

    def test_kb_map_contains_scroll_lock(self, ptt_manager):
        """ScrollLock must be in the keyboard map."""
        from ui.ptt_manager import _PTT_KB_MAP
        assert "ScrollLock" in _PTT_KB_MAP

    def test_kb_map_contains_insert(self, ptt_manager):
        """Insert must be in the keyboard map."""
        from ui.ptt_manager import _PTT_KB_MAP
        assert "Insert" in _PTT_KB_MAP

    def test_kb_map_contains_pause(self, ptt_manager):
        """Pause must be in the keyboard map."""
        from ui.ptt_manager import _PTT_KB_MAP
        assert "Pause" in _PTT_KB_MAP

    def test_mouse_map_contains_mouse4(self, ptt_manager):
        """Mouse4 must be in the mouse map."""
        from ui.ptt_manager import _PTT_MOUSE_MAP
        assert "Mouse4" in _PTT_MOUSE_MAP

    def test_mouse_map_contains_mouse5(self, ptt_manager):
        """Mouse5 must be in the mouse map."""
        from ui.ptt_manager import _PTT_MOUSE_MAP
        assert "Mouse5" in _PTT_MOUSE_MAP

    def test_reverse_map_contains_all_kb_keys(self, ptt_manager):
        """All keyboard keys must have reverse mappings."""
        from ui.ptt_manager import _PTT_KB_MAP, _PTT_REVERSE_MAP
        for display_name, pynput_key in _PTT_KB_MAP.items():
            assert pynput_key in _PTT_REVERSE_MAP
            assert _PTT_REVERSE_MAP[pynput_key] == display_name

    def test_reverse_map_contains_all_mouse_keys(self, ptt_manager):
        """All mouse buttons must have reverse mappings."""
        from ui.ptt_manager import _PTT_MOUSE_MAP, _PTT_REVERSE_MAP
        for display_name, pynput_button in _PTT_MOUSE_MAP.items():
            assert pynput_button in _PTT_REVERSE_MAP
            assert _PTT_REVERSE_MAP[pynput_button] == display_name

    def test_kb_map_values_are_not_none(self, ptt_manager):
        """Keyboard map values should be pynput keyboard.Key objects."""
        from ui.ptt_manager import _PTT_KB_MAP
        for display_name, pynput_key in _PTT_KB_MAP.items():
            assert pynput_key is not None

    def test_mouse_map_values_are_not_none(self, ptt_manager):
        """Mouse map values should be pynput mouse.Button objects."""
        from ui.ptt_manager import _PTT_MOUSE_MAP
        for display_name, pynput_button in _PTT_MOUSE_MAP.items():
            assert pynput_button is not None

    def test_reverse_map_total_count(self, ptt_manager):
        """Reverse map should contain all keys from both maps."""
        from ui.ptt_manager import _PTT_KB_MAP, _PTT_MOUSE_MAP, _PTT_REVERSE_MAP
        expected_count = len(_PTT_KB_MAP) + len(_PTT_MOUSE_MAP)
        assert len(_PTT_REVERSE_MAP) == expected_count


class TestHotkeyValidation:
    """Test hotkey validation logic."""

    def test_valid_f_key(self, ptt_manager):
        """F1 should be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("F1") is True

    def test_valid_f12(self, ptt_manager):
        """F12 should be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("F12") is True

    def test_valid_mouse4(self, ptt_manager):
        """Mouse4 should be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("Mouse4") is True

    def test_valid_mouse5(self, ptt_manager):
        """Mouse5 should be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("Mouse5") is True

    def test_valid_scroll_lock(self, ptt_manager):
        """ScrollLock should be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("ScrollLock") is True

    def test_valid_insert(self, ptt_manager):
        """Insert should be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("Insert") is True

    def test_valid_pause(self, ptt_manager):
        """Pause should be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("Pause") is True

    def test_invalid_hotkey(self, ptt_manager):
        """F13 should not be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("F13") is False

    def test_invalid_empty_string(self, ptt_manager):
        """Empty string should not be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("") is False

    def test_invalid_random_string(self, ptt_manager):
        """Random string should not be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey("Ctrl+A") is False

    def test_invalid_none(self, ptt_manager):
        """None should not be a valid hotkey."""
        assert ptt_manager.is_valid_hotkey(None) is False

    def test_case_sensitive(self, ptt_manager):
        """Hotkey validation should be case-sensitive."""
        assert ptt_manager.is_valid_hotkey("f1") is False
        assert ptt_manager.is_valid_hotkey("mouse4") is False


class TestGetAllSupportedHotkeys:
    """Test retrieval of all supported hotkeys."""

    def test_returns_all_hotkeys(self, ptt_manager, hotkey_list):
        """get_all_supported_hotkeys should return the full list."""
        result = ptt_manager.get_all_supported_hotkeys()
        assert result == hotkey_list

    def test_returns_list_not_tuple(self, ptt_manager):
        """Should return a list, not a tuple."""
        result = ptt_manager.get_all_supported_hotkeys()
        assert isinstance(result, list)

    def test_returns_copy_not_reference(self, ptt_manager):
        """Should return a copy to prevent external mutation."""
        result = ptt_manager.get_all_supported_hotkeys()
        result.append("FAKE_KEY")
        assert "FAKE_KEY" not in ptt_manager.get_all_supported_hotkeys()


class TestConfigLoad:
    """Test loading PTT configuration from JSON file."""

    def test_load_returns_default_when_file_missing(self, ptt_manager, ptt_config_file):
        """Should return default hotkey when config file doesn't exist."""
        assert not os.path.exists(ptt_config_file)
        result = ptt_manager.load_config()
        assert result == "F10"

    def test_load_returns_saved_hotkey_when_file_exists(self, ptt_manager, ptt_config_file):
        """Should return saved hotkey when config file exists with valid data."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            json.dump({"hotkey": "F5"}, f)
        result = ptt_manager.load_config()
        assert result == "F5"

    def test_load_returns_default_when_hotkey_invalid(self, ptt_manager, ptt_config_file, mock_logger):
        """Should return default when file contains an invalid hotkey."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            json.dump({"hotkey": "F99"}, f)
        result = ptt_manager.load_config()
        assert result == "F10"
        mock_logger.debug.assert_called()

    def test_load_returns_default_when_json_invalid(self, ptt_manager, ptt_config_file, mock_logger):
        """Should return default when file contains invalid JSON."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            f.write("{invalid json}")
        result = ptt_manager.load_config()
        assert result == "F10"
        mock_logger.debug.assert_called()

    def test_load_returns_default_when_file_empty(self, ptt_manager, ptt_config_file):
        """Should return default when file is empty."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            f.write("")
        result = ptt_manager.load_config()
        assert result == "F10"

    def test_load_returns_default_when_missing_hotkey_key(self, ptt_manager, ptt_config_file):
        """Should return default when JSON has no 'hotkey' key."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            json.dump({"other_key": "F5"}, f)
        result = ptt_manager.load_config()
        assert result == "F10"

    def test_load_handles_mouse_hotkey(self, ptt_manager, ptt_config_file):
        """Should correctly load a mouse hotkey."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            json.dump({"hotkey": "Mouse4"}, f)
        result = ptt_manager.load_config()
        assert result == "Mouse4"

    def test_load_handles_scroll_lock_hotkey(self, ptt_manager, ptt_config_file):
        """Should correctly load ScrollLock hotkey."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            json.dump({"hotkey": "ScrollLock"}, f)
        result = ptt_manager.load_config()
        assert result == "ScrollLock"


class TestConfigSave:
    """Test saving PTT configuration to JSON file."""

    def test_save_creates_valid_json(self, ptt_manager, ptt_config_file):
        """Should save correct JSON format with hotkey key."""
        result = ptt_manager.save_config("F3")
        assert result is True
        assert os.path.exists(ptt_config_file)
        with open(ptt_config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"hotkey": "F3"}

    def test_save_creates_directory_if_needed(self, ptt_manager, temp_dir):
        """Should create parent directory if it doesn't exist."""
        nested_config = os.path.join(temp_dir, "nested", "dir", "ptt.json")
        ptt_manager._config_file = nested_config
        result = ptt_manager.save_config("F7")
        assert result is True
        assert os.path.exists(nested_config)

    def test_save_returns_true_for_invalid_hotkey(self, ptt_manager):
        """_save_config saves regardless of validity; validation is caller's responsibility."""
        result = ptt_manager.save_config("F99")
        assert result is True

    def test_save_overwrites_existing_config(self, ptt_manager, ptt_config_file):
        """Should overwrite existing config file."""
        ptt_manager.save_config("F1")
        ptt_manager.save_config("F12")
        with open(ptt_config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["hotkey"] == "F12"

    def test_save_uses_utf8_encoding(self, ptt_manager, ptt_config_file):
        """Should save with UTF-8 encoding."""
        ptt_manager.save_config("F10")
        with open(ptt_config_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert '"hotkey"' in content

    def test_save_mouse_hotkey(self, ptt_manager, ptt_config_file):
        """Should correctly save a mouse hotkey."""
        result = ptt_manager.save_config("Mouse5")
        assert result is True
        with open(ptt_config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["hotkey"] == "Mouse5"


class TestGetHotkey:
    """Test getting the current hotkey."""

    def test_get_returns_current_hotkey(self, ptt_manager):
        """Should return the currently set hotkey."""
        assert ptt_manager.get_hotkey() == "F10"

    def test_get_after_set(self, ptt_manager):
        """Should return updated hotkey after set_hotkey."""
        ptt_manager.set_hotkey("F3")
        assert ptt_manager.get_hotkey() == "F3"

    def test_get_after_load(self, ptt_manager, ptt_config_file):
        """Should return loaded hotkey after load_config."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            json.dump({"hotkey": "Insert"}, f)
        ptt_manager.load_config()
        assert ptt_manager.get_hotkey() == "Insert"


class TestSetHotkey:
    """Test setting a new hotkey."""

    def test_set_valid_hotkey(self, ptt_manager):
        """Should return True when setting a valid hotkey."""
        assert ptt_manager.set_hotkey("F5") is True

    def test_set_invalid_hotkey(self, ptt_manager):
        """Should return False when setting an invalid hotkey."""
        assert ptt_manager.set_hotkey("F99") is False

    def test_set_none_hotkey(self, ptt_manager):
        """Should return False when setting None."""
        assert ptt_manager.set_hotkey(None) is False

    def test_set_empty_string(self, ptt_manager):
        """Should return False when setting empty string."""
        assert ptt_manager.set_hotkey("") is False


class TestBuildPynputTarget:
    """Test building pynput target tuples for listener creation."""

    def test_build_keyboard_target(self, ptt_manager):
        """Should return ('keyboard', Key) for keyboard hotkeys."""
        kind, target = ptt_manager.build_pynput_target("F1")
        assert kind == "keyboard"
        assert target is not None

    def test_build_mouse_target(self, ptt_manager):
        """Should return ('mouse', Button) for mouse hotkeys."""
        kind, target = ptt_manager.build_pynput_target("Mouse4")
        assert kind == "mouse"
        assert target is not None

    def test_build_invalid_target(self, ptt_manager):
        """Should return (None, None) for invalid hotkeys."""
        kind, target = ptt_manager.build_pynput_target("F99")
        assert kind is None
        assert target is None

    def test_build_all_f_keys(self, ptt_manager):
        """All F1-F12 should build keyboard targets."""
        for i in range(1, 13):
            kind, target = ptt_manager.build_pynput_target(f"F{i}")
            assert kind == "keyboard"
            assert target is not None

    def test_build_scroll_lock(self, ptt_manager):
        """ScrollLock should build a keyboard target."""
        kind, target = ptt_manager.build_pynput_target("ScrollLock")
        assert kind == "keyboard"
        assert target is not None

    def test_build_insert(self, ptt_manager):
        """Insert should build a keyboard target."""
        kind, target = ptt_manager.build_pynput_target("Insert")
        assert kind == "keyboard"
        assert target is not None

    def test_build_pause(self, ptt_manager):
        """Pause should build a keyboard target."""
        kind, target = ptt_manager.build_pynput_target("Pause")
        assert kind == "keyboard"
        assert target is not None

    def test_build_mouse4(self, ptt_manager):
        """Mouse4 should build a mouse target."""
        kind, target = ptt_manager.build_pynput_target("Mouse4")
        assert kind == "mouse"
        assert target is not None

    def test_build_mouse5(self, ptt_manager):
        """Mouse5 should build a mouse target."""
        kind, target = ptt_manager.build_pynput_target("Mouse5")
        assert kind == "mouse"
        assert target is not None


class TestPTTManagerInit:
    """Test PTTManager initialization."""

    def test_init_sets_defaults(self, ptt_manager):
        """Should initialize with default hotkey."""
        assert ptt_manager.hotkey == "F10"
        assert ptt_manager._config_file.endswith("ptt_settings.json")

    def test_init_custom_default_hotkey(self, ptt_manager_class, ptt_config_file, hotkey_list, mock_logger):
        """Should accept a custom default hotkey."""
        manager = ptt_manager_class(
            config_file=ptt_config_file,
            default_hotkey="F5",
            hotkey_list=hotkey_list,
            logger=mock_logger,
        )
        assert manager.hotkey == "F5"

    def test_init_stores_logger(self, ptt_manager, mock_logger):
        """Should store the logger reference."""
        assert ptt_manager._logger is mock_logger


class TestThreadSafety:
    """Test thread safety of concurrent config operations."""

    def test_concurrent_reads_dont_corrupt(self, ptt_manager, ptt_config_file):
        """Concurrent config reads should not corrupt data."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            json.dump({"hotkey": "F5"}, f)

        results = []
        errors = []

        def read_config():
            try:
                for _ in range(50):
                    result = ptt_manager.load_config()
                    results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=read_config) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(r == "F5" for r in results)

    def test_concurrent_writes_dont_corrupt(self, ptt_manager, ptt_config_file):
        """Concurrent config writes should not corrupt data."""
        errors = []

        def write_config(hotkey):
            try:
                for _ in range(20):
                    ptt_manager.save_config(hotkey)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=write_config, args=(f"F{i}",))
            for i in range(1, 6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        with open(ptt_config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "hotkey" in data
        assert data["hotkey"] in [f"F{i}" for i in range(1, 6)]

    def test_hotkey_change_during_load_is_thread_safe(self, ptt_manager, ptt_config_file):
        """Hotkey change during load should be thread-safe."""
        with open(ptt_config_file, "w", encoding="utf-8") as f:
            json.dump({"hotkey": "F1"}, f)

        errors = []

        def change_hotkey():
            try:
                for hotkey in ["F2", "F3", "F4", "F5"]:
                    ptt_manager.set_hotkey(hotkey)
                    ptt_manager.save_config(hotkey)
            except Exception as e:
                errors.append(e)

        def load_hotkey():
            try:
                for _ in range(50):
                    ptt_manager.load_config()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=change_hotkey)
        t2 = threading.Thread(target=load_hotkey)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0

    def test_get_hotkey_thread_safe(self, ptt_manager):
        """get_hotkey should be thread-safe under concurrent access."""
        results = []
        errors = []

        def get_repeatedly():
            try:
                for _ in range(100):
                    results.append(ptt_manager.get_hotkey())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=get_repeatedly) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 500


class TestCallbackRegistration:
    """Test callback registration for UI integration."""

    def test_set_status_callback(self, ptt_manager):
        """Should register status callback."""
        callback = MagicMock()
        ptt_manager.set_status_callback(callback)
        assert ptt_manager._on_status_change is callback

    def test_set_state_callback(self, ptt_manager):
        """Should register state callback."""
        callback = MagicMock()
        ptt_manager.set_state_callback(callback)
        assert ptt_manager._on_state_change is callback

    def test_set_log_callback(self, ptt_manager):
        """Should register log callback."""
        callback = MagicMock()
        ptt_manager.set_log_callback(callback)
        assert ptt_manager._on_log is callback


class TestStateProperties:
    """Test PTT state property accessors."""

    def test_enabled_default_false(self, ptt_manager):
        """PTT should be disabled by default."""
        assert ptt_manager.enabled is False

    def test_enabled_setter(self, ptt_manager):
        """Should enable PTT via setter."""
        ptt_manager.enabled = True
        assert ptt_manager.enabled is True

    def test_active_default_false(self, ptt_manager):
        """PTT should not be active by default."""
        assert ptt_manager.active is False

    def test_mapping_default_false(self, ptt_manager):
        """Should not be in mapping mode by default."""
        assert ptt_manager.mapping is False

    def test_target_default_none(self, ptt_manager):
        """Target should be None before listener starts."""
        assert ptt_manager.target is None

    def test_listener_default_none(self, ptt_manager):
        """Listener should be None before start."""
        assert ptt_manager.listener is None
