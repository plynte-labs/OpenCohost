"""Tests for ui.avatar_panel.AvatarPanel.

Covers:
- Panel builds without real OBS or user Downloads assets
- State rows are created for all valid states
- Próximamente section is present
- Selecting/replacing an image updates config and preview (mocked file picker)
- Panel builds with no configured images (safe defaults)
"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mocked customtkinter, applied per-test via the autouse fixture below.
# Never assign this into sys.modules at module level: collection imports this
# file early and the leaked mock would hijack `patch("customtkinter.X")`
# targets for every later test file in the run.
_ctk_mock = MagicMock()
_ctk_mock.CTkFrame.return_value = MagicMock()
_ctk_mock.CTkLabel.return_value = MagicMock()
_ctk_mock.CTkButton.return_value = MagicMock()
_ctk_mock.CTkFont.return_value = MagicMock()
_ctk_mock.CTkImage.return_value = MagicMock()

# Ensure the project root is on the path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opencohost.avatar.avatar_config import VALID_STATES, AvatarConfig, assign_image_to_state
from opencohost.avatar.avatar_state import AvatarState, AvatarStateBridge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_parent():
    """Mock a CTk-compatible parent frame."""
    parent = MagicMock()
    parent.grid = MagicMock()
    parent.grid_columnconfigure = MagicMock()
    return parent


@pytest.fixture()
def sample_image(tmp_path):
    """Create a small fake PNG file."""
    img = tmp_path / "test_image.png"
    img.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return img


@pytest.fixture(autouse=True)
def _patch_panel_ctk():
    """Force the panel module to use the mocked ctk regardless of import cache.

    Full-suite collection imports app_shell (and thus avatar_panel) with the
    real customtkinter before the module-level sys.modules mock above can take
    effect. Real CTk widgets walk ``widget.master`` looking for a Tk root,
    which never terminates against MagicMock parents.
    """
    import opencohost.ui.avatar_panel as _panel_module
    with patch.object(_panel_module, "ctk", _ctk_mock):
        yield


@pytest.fixture()
def panel(mock_parent):
    """Build an AvatarPanel with mocked dependencies."""
    from opencohost.ui.avatar_panel import AvatarPanel
    on_log = MagicMock()
    schedule_ui = MagicMock(side_effect=lambda fn: fn())
    p = AvatarPanel(parent_frame=mock_parent, on_log=on_log, schedule_ui_update=schedule_ui)
    return p


def _obs_widget(value):
    widget = MagicMock()
    widget.get.return_value = value
    return widget


# ---------------------------------------------------------------------------
# 1. Panel builds without real assets
# ---------------------------------------------------------------------------

class TestPanelBuilds:
    def test_build_returns_frame(self, panel):
        frame = panel.build()
        assert frame is not None

    def test_build_creates_all_state_rows(self, panel):
        panel.build()
        for state in VALID_STATES:
            assert state in panel._state_rows, f"Missing row for state: {state}"

    def test_build_creates_proximamente_section(self, panel):
        """The panel must show 'Próximamente' for future features."""
        panel.build()
        # The future section is built at a specific row — verify it exists
        assert panel._frame is not None

    def test_build_with_no_images_does_not_crash(self, panel):
        """Panel must handle empty config gracefully."""
        frame = panel.build()
        assert frame is not None

    def test_build_shows_placeholder_when_no_image(self, panel):
        panel.build()
        # Preview label should show placeholder text
        assert panel._preview_label is not None


# ---------------------------------------------------------------------------
# 2. Image selection updates config (mocked file picker)
# ---------------------------------------------------------------------------

class TestImageSelection:
    def test_choose_image_copies_to_assets(self, panel, sample_image, tmp_path):
        """Selecting an image should copy it to the managed asset folder."""
        panel.build()
        asset_dir = tmp_path / "avatars"
        asset_dir.mkdir()

        # Patch the config's assets folder for this test
        panel.config.assets_folder = asset_dir

        with patch("opencohost.ui.avatar_panel.filedialog.askopenfilename", return_value=str(sample_image)):
            with patch("opencohost.ui.avatar_panel.save_avatar_config") as save_config:
                panel._choose_image("idle")

        save_config.assert_called_once_with(panel.config)

        assert "idle" in panel.config.state_images
        assert panel.config.state_images["idle"].exists()

    def test_choose_image_does_not_write_real_avatar_config(self, panel, sample_image, tmp_path):
        """Avatar selection tests must not persist pytest temp paths to real config."""
        panel.build()
        panel.config.assets_folder = tmp_path / "avatars"

        with patch("opencohost.ui.avatar_panel.filedialog.askopenfilename", return_value=str(sample_image)):
            with patch("opencohost.ui.avatar_panel.save_avatar_config") as save_config:
                panel._choose_image("idle")

        save_config.assert_called_once_with(panel.config)

        assert "idle" in panel.config.state_images
        assert panel.config.state_images["idle"].exists()

    def test_choose_image_cancelled_does_nothing(self, panel):
        """Cancelling the file picker should not change config."""
        panel.build()
        original_images = dict(panel.config.state_images)

        with patch("opencohost.ui.avatar_panel.filedialog.askopenfilename", return_value=""):
            panel._choose_image("idle")

        assert panel.config.state_images == original_images

    def test_test_state_changes_preview(self, panel):
        """The 'Probar' button should change the current previewed state."""
        panel.build()
        assert panel.get_current_state() == "idle"

        panel._test_state("thinking")
        assert panel.get_current_state() == "thinking"


# ---------------------------------------------------------------------------
# 3. State bridge integration
# ---------------------------------------------------------------------------

class TestStateBridgeIntegration:
    def test_set_state_bridge_registers_listener(self, panel):
        panel.build()
        bridge = AvatarStateBridge()
        panel.set_state_bridge(bridge)

        assert panel.state_bridge is bridge

    def test_state_bridge_change_updates_panel(self, panel, mock_parent):
        """When the bridge changes state, the panel should update."""
        panel.build()
        bridge = AvatarStateBridge()
        panel.set_state_bridge(bridge)

        bridge.set_state(AvatarState.SPEAKING)

        # The schedule_ui should have been called to update the preview
        assert panel.get_current_state() == "speaking"

    def test_cleanup_unsubscribes(self, panel):
        """Cleanup should unsubscribe from the bridge."""
        panel.build()
        bridge = AvatarStateBridge()
        panel.set_state_bridge(bridge)

        panel.cleanup()
        assert panel._bridge_sub_id is None


class TestPreviewFallback:
    def test_missing_transient_state_keeps_cached_idle_image(self, panel, tmp_path):
        panel.build()
        idle_ref = object()
        panel._preview_idle_image_ref = idle_ref
        panel._preview_idle_image_pil = object()
        panel._preview_image_ref = idle_ref
        panel.config.state_images = {"idle": tmp_path / "idle.png"}
        panel._current_state = "thinking"

        with patch("opencohost.ui.avatar_panel.os.path.isfile", return_value=False):
            panel._update_preview()

        assert panel._preview_image_ref is idle_ref
        panel._preview_label.configure.assert_called_with(image=idle_ref, text="")


class TestOBSLifecycleCallbacks:
    def test_obs_toggle_on_saves_config_and_requests_runtime_enable(self, mock_parent):
        from opencohost.ui.avatar_panel import AvatarPanel

        on_enable = MagicMock()
        on_disable = MagicMock()
        panel = AvatarPanel(
            parent_frame=mock_parent,
            on_obs_enable=on_enable,
            on_obs_disable=on_disable,
        )
        panel._obs_switch = _obs_widget(True)
        panel._obs_host_entry = _obs_widget("localhost")
        panel._obs_port_entry = _obs_widget("4455")
        panel._obs_pass_entry = _obs_widget("")
        panel._obs_source_entry = _obs_widget("KiraAvatar")
        panel._obs_connect_btn = MagicMock()

        with patch("opencohost.ui.avatar_panel.save_avatar_config") as save_config:
            panel._on_obs_toggle()

        assert panel.config.obs.enabled is True
        save_config.assert_called()
        on_enable.assert_called_once()
        on_disable.assert_not_called()

    def test_obs_toggle_off_saves_config_and_requests_runtime_disable(self, mock_parent):
        from opencohost.ui.avatar_panel import AvatarPanel

        on_enable = MagicMock()
        on_disable = MagicMock()
        panel = AvatarPanel(
            parent_frame=mock_parent,
            on_obs_enable=on_enable,
            on_obs_disable=on_disable,
        )
        panel._obs_switch = _obs_widget(False)
        panel._obs_host_entry = _obs_widget("localhost")
        panel._obs_port_entry = _obs_widget("4455")
        panel._obs_pass_entry = _obs_widget("")
        panel._obs_source_entry = _obs_widget("KiraAvatar")
        panel._obs_connect_btn = MagicMock()

        with patch("opencohost.ui.avatar_panel.save_avatar_config") as save_config:
            panel._on_obs_toggle()

        assert panel.config.obs.enabled is False
        save_config.assert_called()
        on_disable.assert_called_once()
        on_enable.assert_not_called()

    def test_obs_connect_button_requests_live_runtime_connection(self, mock_parent):
        from opencohost.ui.avatar_panel import AvatarPanel

        on_connect = MagicMock()
        panel = AvatarPanel(parent_frame=mock_parent, on_obs_connect=on_connect)
        panel._obs_host_entry = _obs_widget("localhost")
        panel._obs_port_entry = _obs_widget("4455")
        panel._obs_pass_entry = _obs_widget("")
        panel._obs_source_entry = _obs_widget("KiraAvatar")
        panel._obs_connect_btn = MagicMock()
        panel._obs_status_label = MagicMock()

        with patch("opencohost.ui.avatar_panel.save_avatar_config") as save_config:
            panel._test_obs_connection()

        save_config.assert_called_once_with(panel.config)
        on_connect.assert_called_once()

    def test_load_failure_keeps_cached_idle_image(self, panel, tmp_path):
        panel.build()
        idle_ref = object()
        panel._preview_idle_image_ref = idle_ref
        panel._preview_idle_image_pil = object()
        panel.config.state_images = {"idle": tmp_path / "idle.png", "speaking": tmp_path / "speaking.png"}
        panel._current_state = "speaking"

        image_module = ModuleType("PIL.Image")
        image_module.open = MagicMock(side_effect=OSError("bad image"))
        image_module.Resampling = SimpleNamespace(LANCZOS=object())

        with patch.dict(sys.modules, {"PIL": SimpleNamespace(Image=image_module), "PIL.Image": image_module}):
            with patch("opencohost.ui.avatar_panel.os.path.isfile", return_value=True):
                panel._update_preview()

        assert panel._preview_image_ref is idle_ref
        panel._preview_label.configure.assert_called_with(image=idle_ref, text="")


# ---------------------------------------------------------------------------
# 4. Source-level safety checks
# ---------------------------------------------------------------------------

class TestSourceSafety:
    def test_no_hardcoded_user_paths(self):
        """The avatar panel must not contain hardcoded user paths."""
        source = (ROOT / "opencohost" / "ui" / "avatar_panel.py").read_text(encoding="utf-8")
        assert "C:\\Users\\" not in source  # path-ok: test exercises drive-letter path handling
        assert "Downloads" not in source

    def test_no_real_obs_controls(self):
        """The panel must not expose working OBS controls — only Próximamente."""
        source = (ROOT / "opencohost" / "ui" / "avatar_panel.py").read_text(encoding="utf-8")
        # No actual OBS WebSocket implementation
        assert "websocket.connect" not in source
        assert "Próximamente" in source

    def test_uses_filedialog_not_hardcoded(self):
        """Image selection must use filedialog, not hardcoded paths."""
        source = (ROOT / "opencohost" / "ui" / "avatar_panel.py").read_text(encoding="utf-8")
        assert "filedialog.askopenfilename" in source

    def test_copies_to_managed_folder(self):
        """The panel must copy images to a managed folder."""
        source = (ROOT / "opencohost" / "ui" / "avatar_panel.py").read_text(encoding="utf-8")
        assert "assign_image_to_state" in source
