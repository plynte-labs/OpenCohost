"""Tests for smart_aggregator.url_parser.parse_chat_url()."""

import pytest
from smart_aggregator.url_parser import parse_chat_url


class TestParseChatUrl:
    """URL parser for multi-platform chat sources (REQ-9..13)."""

    # ── YouTube URLs ──────────────────────────────────────────────

    @pytest.mark.parametrize("url,expected_id", [
        ("https://www.youtube.com/watch?v=abc123def45", "abc123def45"),
        ("https://youtube.com/watch?v=abc123def45", "abc123def45"),
        ("youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=a1b2c3d4e5f", "a1b2c3d4e5f"),
    ])
    def test_youtube_watch_url(self, url, expected_id):
        """REQ-10: Extract video ID from youtube.com/watch?v=ID."""
        result = parse_chat_url(url)
        assert result["platform"] == "youtube"
        assert result["source_id"] == expected_id

    @pytest.mark.parametrize("url,expected_id", [
        ("https://www.youtube.com/live/abc123def45", "abc123def45"),
        ("https://youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("youtube.com/live/xyz987abc12", "xyz987abc12"),
    ])
    def test_youtube_live_url(self, url, expected_id):
        """REQ-10: Extract video ID from youtube.com/live/ID."""
        result = parse_chat_url(url)
        assert result["platform"] == "youtube"
        assert result["source_id"] == expected_id

    @pytest.mark.parametrize("url,expected_id", [
        ("https://youtu.be/abc123def45", "abc123def45"),
        ("youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ])
    def test_youtube_short_url(self, url, expected_id):
        """REQ-10: Extract video ID from youtu.be/ID."""
        result = parse_chat_url(url)
        assert result["platform"] == "youtube"
        assert result["source_id"] == expected_id

    # ── Twitch URLs ───────────────────────────────────────────────

    @pytest.mark.parametrize("url,expected_channel", [
        ("https://twitch.tv/streamerName", "streamerName"),
        ("https://www.twitch.tv/grandPOOBear", "grandPOOBear"),
        ("twitch.tv/mychannel", "mychannel"),
    ])
    def test_twitch_channel_url(self, url, expected_channel):
        """REQ-11: Extract channel name from twitch.tv/channel."""
        result = parse_chat_url(url)
        assert result["platform"] == "twitch"
        assert result["source_id"] == expected_channel

    @pytest.mark.parametrize("url,expected_channel", [
        ("https://www.twitch.tv/popout/streamerName/chat", "streamerName"),
        ("twitch.tv/popout/grandPOOBear/chat", "grandPOOBear"),
    ])
    def test_twitch_popout_url(self, url, expected_channel):
        """REQ-11: Extract channel from twitch.tv/popout/channel/chat."""
        result = parse_chat_url(url)
        assert result["platform"] == "twitch"
        assert result["source_id"] == expected_channel

    # ── Bare 11-char IDs ──────────────────────────────────────────

    @pytest.mark.parametrize("bare_id", [
        "abc123def45",
        "dQw4w9WgXcQ",
        "0123456789a",
        "a_b-c1d2e3f",
    ])
    def test_bare_11char_id_defaults_youtube(self, bare_id):
        """REQ-12: Bare 11-char IDs default to YouTube for backward compat."""
        result = parse_chat_url(bare_id)
        assert result["platform"] == "youtube"
        assert result["source_id"] == bare_id

    # ── Invalid / unsupported ─────────────────────────────────────

    @pytest.mark.parametrize("bad_input", [
        "",
        "   ",
        "hello",
        "abc",  # too short
        "abc123def456",  # 12 chars, not URL
    ])
    def test_invalid_input_raises(self, bad_input):
        """REQ-13: Empty/whitespace/garbage raises ValueError."""
        with pytest.raises(ValueError, match=r"URL no valida"):
            parse_chat_url(bad_input)

    @pytest.mark.parametrize("unsupported_url", [
        "https://kick.com/somechannel",
        "https://www.facebook.com/streamer/videos/123",
        "https://dlive.tv/streamer",
        "https://tiktok.com/@user/live",
    ])
    def test_unsupported_platform_raises(self, unsupported_url):
        """REQ-13: Unsupported platforms raise ValueError."""
        with pytest.raises(ValueError, match=r"URL no valida"):
            parse_chat_url(unsupported_url)
