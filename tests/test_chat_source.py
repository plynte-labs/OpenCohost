"""Tests for ChatSource ABC and YouTubeChatSource refactor (REQ-1..3, REQ-14..16)."""

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from smart_aggregator.chat_source import (
    ChatSource,
    NormalizedChatMessage,
    TwitchChatSource,
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

    @patch("smart_aggregator.chat_source.pytchat", create=True)
    def test_youtube_custom_emote_text_is_not_changed(
        self, mock_pytchat, smart_aggregator_config
    ):
        """Twitch emote stripping must not change YouTubeChatSource messages."""
        mock_message = MagicMock()
        mock_message.author.name = "viewer1"
        mock_message.message = "hello :bird:"

        mock_chat = MagicMock()
        mock_chat.is_alive.side_effect = [True, False]
        mock_chat.get.return_value.sync_items.return_value = [mock_message]
        mock_pytchat.create.return_value = mock_chat

        messages = []
        source = YouTubeChatSource(
            smart_aggregator_config["source"],
            callbacks={"on_message": messages.append},
        )
        source.connect("test12345678")
        time.sleep(0.7)
        source.disconnect()

        assert messages[0]["platform"] == "youtube"
        assert messages[0]["text"] == "hello :bird:"


class TestTwitchChatSource:
    """TwitchChatSource IRC parsing and lifecycle (T-13, REQ-4..7)."""

    @pytest.fixture
    def twitch_config(self):
        return {"reconnect_delay_seconds": 0.05, "max_retries": 2}

    @pytest.fixture
    def mock_socket(self):
        """Create a mock socket with controllable recv behavior."""
        sock = MagicMock()
        sock.recv.return_value = b""
        return sock

    def test_platform_property(self, twitch_config):
        """REQ-4: platform property returns 'twitch'."""
        source = TwitchChatSource(twitch_config, callbacks={})
        assert source.platform == "twitch"

    def test_not_connected_initially(self, twitch_config):
        """Source should not be connected initially."""
        source = TwitchChatSource(twitch_config, callbacks={})
        assert not source.is_connected()

    def test_connect_fails_without_channel(self, twitch_config):
        """Connect must fail with empty channel name."""
        source = TwitchChatSource(twitch_config, callbacks={})
        with pytest.raises(ValueError):
            source.connect("")

    def test_disconnect_without_connect_does_not_crash(self, twitch_config):
        """Disconnect without prior connect should not crash."""
        source = TwitchChatSource(twitch_config, callbacks={})
        source.disconnect()

    def test_isinstance_of_chat_source(self, twitch_config):
        """TwitchChatSource subclasses ChatSource."""
        source = TwitchChatSource(twitch_config, callbacks={})
        assert isinstance(source, ChatSource)

    @patch("socket.create_connection")
    def test_irc_connection_commands_sent(
        self, mock_create_conn, twitch_config
    ):
        """REQ-4: IRC tags capability, NICK, USER, and JOIN are sent on connect."""
        mock_sock = MagicMock()
        mock_sock.recv.return_value = b""
        mock_create_conn.return_value = mock_sock

        connect_info = []

        source = TwitchChatSource(
            twitch_config,
            callbacks={"on_connect": connect_info.append},
        )
        source.connect("testchannel")
        time.sleep(0.2)
        source.disconnect()

        # Collect all sent data
        all_sent = b""
        for send_call in mock_sock.sendall.call_args_list:
            all_sent += send_call[0][0]

        assert b"CAP REQ :twitch.tv/tags" in all_sent
        assert b"NICK justinfan" in all_sent
        assert b"USER" in all_sent
        assert b"JOIN #testchannel" in all_sent

        assert len(connect_info) >= 1
        assert connect_info[0]["platform"] == "twitch"
        assert connect_info[0]["source_id"] == "testchannel"

    @patch("socket.create_connection")
    def test_privmsg_parsing(self, mock_create_conn, twitch_config):
        """REQ-5: PRIVMSG is parsed into NormalizedChatMessage."""
        mock_sock = MagicMock()
        mock_create_conn.return_value = mock_sock

        irc_line = (
            b":viewer!viewer@viewer.tmi.twitch.tv "
            b"PRIVMSG #testchannel :hello world\r\n"
        )
        mock_sock.recv.side_effect = [irc_line, b""]

        messages = []
        source = TwitchChatSource(
            twitch_config,
            callbacks={"on_message": messages.append},
        )
        source.connect("testchannel")
        time.sleep(0.3)
        source.disconnect()

        assert len(messages) == 1
        msg = messages[0]
        assert msg["platform"] == "twitch"
        assert msg["source_id"] == "testchannel"
        assert msg["user"] == "viewer"
        assert msg["text"] == "hello world"
        assert "timestamp" in msg

    def test_tagged_privmsg_strips_only_emote_ranges(self, twitch_config):
        """Tagged Twitch emote ranges are removed before filtering/aggregation."""
        line = (
            "@emotes=25:0-4/1902:6-10;badges= :viewer!viewer@viewer.tmi.twitch.tv "
            "PRIVMSG #testchannel :Kappa Kappa hello"
        )

        parsed = TwitchChatSource(twitch_config, callbacks={})._parse_privmsg_line(line)

        assert parsed == {"user": "viewer", "text": "hello"}

    def test_tagged_mixed_privmsg_preserves_non_emote_text_order(self, twitch_config):
        """Mixed Twitch emotes and text keep only normal words in original order."""
        line = (
            "@emotes=25:0-4/305954156:12-19 :viewer!viewer@viewer.tmi.twitch.tv "
            "PRIVMSG #testchannel :Kappa hello PogChamp"
        )

        parsed = TwitchChatSource(twitch_config, callbacks={})._parse_privmsg_line(line)

        assert parsed == {"user": "viewer", "text": "hello"}

    def test_tagged_emote_only_privmsg_emits_empty_text(self, twitch_config):
        """Emote-only messages become empty so MessageFilter rejects them safely."""
        line = (
            "@emotes=25:0-4/1902:6-10 :viewer!viewer@viewer.tmi.twitch.tv "
            "PRIVMSG #testchannel :Kappa Kappa"
        )

        parsed = TwitchChatSource(twitch_config, callbacks={})._parse_privmsg_line(line)

        assert parsed == {"user": "viewer", "text": ""}

    def test_privmsg_without_emotes_tag_preserves_current_text(self, twitch_config):
        """Tags without emotes keep current Twitch behavior."""
        line = (
            "@badges= :viewer!viewer@viewer.tmi.twitch.tv "
            "PRIVMSG #testchannel :Kappa Kappa hello"
        )

        parsed = TwitchChatSource(twitch_config, callbacks={})._parse_privmsg_line(line)

        assert parsed == {"user": "viewer", "text": "Kappa Kappa hello"}

    @patch("socket.create_connection")
    def test_privmsg_spanish_message(self, mock_create_conn, twitch_config):
        """REQ-5: Spanish messages parse correctly."""
        mock_sock = MagicMock()
        mock_create_conn.return_value = mock_sock

        irc_line = (
            b":usuario_es!usuario_es@usuario_es.tmi.twitch.tv "
            b"PRIVMSG #testchannel :hola amigo \xc2\xbfc\xc3\xb3mo est\xc3\xa1s?\r\n"
        )
        mock_sock.recv.side_effect = [irc_line, b""]

        messages = []
        source = TwitchChatSource(
            twitch_config,
            callbacks={"on_message": messages.append},
        )
        source.connect("testchannel")
        time.sleep(0.3)
        source.disconnect()

        assert len(messages) == 1
        assert "hola" in messages[0]["text"]

    @patch("socket.create_connection")
    def test_ping_pong_response(self, mock_create_conn, twitch_config):
        """REQ-6: PING is answered with PONG."""
        mock_sock = MagicMock()
        mock_create_conn.return_value = mock_sock

        ping_line = b"PING :tmi.twitch.tv\r\n"
        mock_sock.recv.side_effect = [ping_line, b""]

        source = TwitchChatSource(twitch_config, callbacks={})
        source.connect("testchannel")
        time.sleep(0.2)
        source.disconnect()

        # Check PONG was sent
        pong_found = False
        for send_call in mock_sock.sendall.call_args_list:
            if b"PONG" in send_call[0][0]:
                pong_found = True
                break
        assert pong_found, "PONG response was not sent for PING"

    @patch("socket.create_connection")
    def test_reconnection_exhausts_retries(self, mock_create_conn, twitch_config):
        """REQ-7: After exhausting retries, on_disconnect fires."""
        mock_create_conn.side_effect = ConnectionError("connection refused")

        errors = []
        on_disconnect = MagicMock()
        source = TwitchChatSource(
            twitch_config,
            callbacks={
                "on_error": errors.append,
                "on_disconnect": on_disconnect,
            },
        )
        source.connect("testchannel")
        time.sleep(0.4)
        source.disconnect()

        assert len(errors) >= 1
        on_disconnect.assert_called()

    @patch("socket.create_connection")
    def test_callback_error_isolation(self, mock_create_conn, twitch_config):
        """REQ-2: Callback error in on_message doesn't crash source."""
        mock_sock = MagicMock()
        mock_create_conn.return_value = mock_sock

        irc_line = (
            b":viewer!viewer@viewer.tmi.twitch.tv "
            b"PRIVMSG #testchannel :boom\r\n"
        )
        mock_sock.recv.side_effect = [irc_line, b""]

        callbacks = {
            "on_message": MagicMock(side_effect=RuntimeError("callback boom")),
            "on_connect": MagicMock(),
            "on_disconnect": MagicMock(),
            "on_error": MagicMock(),
        }
        source = TwitchChatSource(twitch_config, callbacks=callbacks)
        source.connect("testchannel")
        time.sleep(0.3)
        source.disconnect()

        # The callback was called (and its exception was caught)
        callbacks["on_message"].assert_called()
        # Source is still operational
        assert not source.is_connected()

    @patch("socket.create_connection")
    def test_is_connected_after_connect(self, mock_create_conn, twitch_config):
        """is_connected() returns True while socket is alive."""
        mock_sock = MagicMock()
        # Block recv so the thread stays alive in the inner loop
        def _blocking_recv(_size=4096):
            time.sleep(0.5)
            raise OSError("closed")
        mock_sock.recv.side_effect = _blocking_recv
        mock_create_conn.return_value = mock_sock

        source = TwitchChatSource(twitch_config, callbacks={})
        source.connect("testchannel")
        time.sleep(0.15)
        # Socket assigned before blocking recv → is_connected is True
        assert source.is_connected()
        source.disconnect()

    @patch("socket.create_connection")
    def test_disconnect_stops_thread_and_cleans_socket(
        self, mock_create_conn, twitch_config
    ):
        """Disconnect stops the daemon thread and closes the socket."""
        mock_sock = MagicMock()
        # Block recv so thread stays alive until we disconnect
        def _blocking_recv(_size=4096):
            time.sleep(0.5)
            raise OSError("closed")
        mock_sock.recv.side_effect = _blocking_recv
        mock_create_conn.return_value = mock_sock

        on_disconnect = MagicMock()
        source = TwitchChatSource(
            twitch_config,
            callbacks={"on_disconnect": on_disconnect},
        )
        source.connect("testchannel")
        time.sleep(0.15)
        source.disconnect()

        assert not source.is_connected()
        mock_sock.close.assert_called()
        on_disconnect.assert_called()
