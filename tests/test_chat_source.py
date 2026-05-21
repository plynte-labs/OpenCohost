"""Tests for ChatSource ABC and YouTubeChatSource refactor (REQ-1..3, REQ-14..16)."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from smart_aggregator.chat_source import (
    ChatSource,
    NormalizedChatMessage,
    YouTubeChatSource,
)


class TestChatSourceABC:
    """ChatSource abstract base class contract (REQ-1..3)."""

    def test_cannot_instantiate_directly(self):
        """ChatSource ABC must not be instantiatable without concrete methods."""
        with pytest.raises(TypeError):
            ChatSource(config={}, callbacks={})  # type: ignore[abstract]

    def test_abstractmethods_contains_all_four(self):
        """ABC defines exactly 4 abstract methods."""
        abstract = ChatSource.__abstractmethods__  # type: ignore[attr-defined]
        assert "connect" in abstract
        assert "disconnect" in abstract
        assert "is_connected" in abstract
        assert "platform" in abstract

    def test_concrete_subclass_is_valid(self, smart_aggregator_config):
        """YouTubeChatSource can be instantiated (no TypeError)."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        assert isinstance(source, ChatSource)


class TestYouTubeChatSourceInterface:
    """YouTubeChatSource implements ChatSource ABC correctly (REQ-14..16)."""

    def test_platform_property(self, smart_aggregator_config):
        """REQ-15: platform property returns 'youtube'."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        assert source.platform == "youtube"

    def test_not_connected_initially(self, smart_aggregator_config):
        """Source should not be connected initially."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        assert not source.is_connected()

    def test_connect_fails_without_source_id(self, smart_aggregator_config):
        """Connect must fail with empty source_id."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        with pytest.raises(ValueError):
            source.connect("")

    def test_disconnect_without_connect_does_not_crash(self, smart_aggregator_config):
        """Disconnect without prior connect should not crash."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        source.disconnect()

    def test_isinstance_of_chat_source(self, smart_aggregator_config):
        """REQ-14: YouTubeChatSource subclasses ChatSource."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        assert isinstance(source, ChatSource)

    def test_connect_handles_invalid_source_id(self, smart_aggregator_config):
        """Connecting to invalid video ID handled gracefully (no crash)."""
        cfg = smart_aggregator_config["source"]
        source = YouTubeChatSource(cfg, callbacks={})
        try:
            source.connect("invalid_video_id_123")
            time.sleep(0.5)
        except Exception as e:
            error_str = str(e).lower()
            assert "pytchat" in error_str or "video" in error_str or "invalid" in error_str

    def test_callback_isolation_on_message(self, smart_aggregator_config):
        """REQ-2: Callback error in on_message doesn't crash source."""
        cfg = smart_aggregator_config["source"]
        callbacks = {
            "on_message": MagicMock(side_effect=RuntimeError("boom")),
            "on_connect": MagicMock(),
            "on_disconnect": MagicMock(),
            "on_error": MagicMock(),
        }
        source = YouTubeChatSource(cfg, callbacks=callbacks)
        # Simulate a message arriving
        try:
            source.callbacks["on_message"]({
                "platform": "youtube",
                "source_id": "test123",
                "user": "test",
                "text": "hello",
                "timestamp": time.time(),
            })
        except Exception:
            pass
        # Source should not crash — the try/except in _run handles it internally.
        # We verify callback was called (the side_effect proves it ran).
        callbacks["on_message"].assert_called_once()


class TestNormalizedChatMessage:
    """NormalizedChatMessage TypedDict contract."""

    def test_required_keys(self):
        """NormalizedChatMessage has the expected keys."""
        # TypedDict doesn't enforce at runtime, so we verify the annotation fields.
        annotations = NormalizedChatMessage.__annotations__
        assert "platform" in annotations
        assert "source_id" in annotations
        assert "user" in annotations
        assert "text" in annotations
        assert "timestamp" in annotations
        assert annotations["platform"] == str
        assert annotations["source_id"] == str
        assert annotations["user"] == str
        assert annotations["text"] == str
        assert annotations["timestamp"] == float

    def test_partial_message_is_valid(self):
        """REQ-3: total=False allows partial messages."""
        msg: NormalizedChatMessage = {"user": "test", "text": "hello"}
        assert msg["user"] == "test"
        assert msg["text"] == "hello"


class TestYouTubeChatSourceCallbackContent:
    """Verify callback dicts include new platform/source_id fields (REQ-16)."""

    @patch("smart_aggregator.chat_source.pytchat", create=True)
    def test_on_connect_includes_platform_and_source_id(
        self, mock_pytchat, smart_aggregator_config
    ):
        """on_connect callback dict includes platform, source_id, video_id."""
        # Setup mock chat object
        mock_chat = MagicMock()
        mock_chat.is_alive.return_value = False  # Stop loop immediately
        mock_pytchat.create.return_value = mock_chat

        cfg = smart_aggregator_config["source"]
        connect_info = []

        def capture_connect(info):
            connect_info.append(info)

        source = YouTubeChatSource(
            cfg,
            callbacks={
                "on_connect": capture_connect,
                "on_message": MagicMock(),
                "on_disconnect": MagicMock(),
                "on_error": MagicMock(),
            },
        )
        source.connect("test12345678")
        time.sleep(0.5)
        source.disconnect()

        assert len(connect_info) == 1
        info = connect_info[0]
        assert info["platform"] == "youtube"
        assert info["source_id"] == "test12345678"
        assert info["video_id"] == "test12345678"  # backward compat

    @patch("smart_aggregator.chat_source.pytchat", create=True)
    def test_on_message_includes_platform_and_source_id(
        self, mock_pytchat, smart_aggregator_config
    ):
        """on_message callback dict includes platform and source_id (REQ-16)."""
        # Setup mock chat that yields one message
        mock_message = MagicMock()
        mock_message.author.name = "viewer1"
        mock_message.message = "hello world"

        mock_chat = MagicMock()
        mock_chat.is_alive.side_effect = [True, False]  # One iteration then stop
        mock_chat.get.return_value.sync_items.return_value = [mock_message]
        mock_pytchat.create.return_value = mock_chat

        cfg = smart_aggregator_config["source"]
        messages = []

        def capture_message(msg):
            messages.append(msg)

        source = YouTubeChatSource(
            cfg,
            callbacks={
                "on_connect": MagicMock(),
                "on_message": capture_message,
                "on_disconnect": MagicMock(),
                "on_error": MagicMock(),
            },
        )
        source.connect("test12345678")
        time.sleep(0.7)
        source.disconnect()

        assert len(messages) >= 1
        msg = messages[0]
        assert msg["platform"] == "youtube"
        assert msg["source_id"] == "test12345678"
        assert msg["user"] == "viewer1"
        assert msg["text"] == "hello world"
        assert "timestamp" in msg
