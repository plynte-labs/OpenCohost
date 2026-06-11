"""URL parser for multi-platform chat sources.

Extracts platform and source_id from chat URLs.
Supports YouTube (watch, live, short links, bare IDs) and Twitch (channel, popout).
"""

import re
from typing import Dict


def parse_chat_url(url: str) -> Dict[str, str]:
    """Parse a chat URL or bare video ID into platform and source_id.

    Args:
        url: YouTube URL, Twitch URL, or bare 11-char video ID.

    Returns:
        Dict with keys 'platform' ('youtube' or 'twitch') and 'source_id'.

    Raises:
        ValueError: If URL is invalid or from an unsupported platform.
    """
    if not url or not url.strip():
        raise ValueError("URL no valida o no soportada")

    url = url.strip()

    # YouTube: youtube.com/watch?v=ID
    m = re.search(r"(?:www\.)?youtube\.com/watch\?v=([\w-]{11})", url)
    if m:
        return {"platform": "youtube", "source_id": m.group(1)}

    # YouTube: youtube.com/live/ID
    m = re.search(r"(?:www\.)?youtube\.com/live/([\w-]{11})", url)
    if m:
        return {"platform": "youtube", "source_id": m.group(1)}

    # YouTube: youtu.be/ID
    m = re.search(r"youtu\.be/([\w-]{11})", url)
    if m:
        return {"platform": "youtube", "source_id": m.group(1)}

    # Twitch: twitch.tv/popout/channel/chat (check before general pattern)
    m = re.search(r"(?:www\.)?twitch\.tv/popout/(\w+)/chat", url)
    if m:
        return {"platform": "twitch", "source_id": m.group(1)}

    # Twitch: twitch.tv/channel (general pattern, skip 'popout' bare)
    m = re.search(r"(?:www\.)?twitch\.tv/(\w+)", url)
    if m:
        channel = m.group(1)
        if channel.lower() != "popout":
            return {"platform": "twitch", "source_id": channel}

    # Bare 11-char YouTube ID (backward compatibility)
    if re.match(r"^[\w-]{11}$", url):
        return {"platform": "youtube", "source_id": url}

    raise ValueError("URL no valida o no soportada")
