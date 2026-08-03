"""Phase 0 - T0.1: APP_ID contract pin (regression for the silent heavy-TTS fallback bug).

Reads server_qwen.py's APP_ID WITHOUT importing the heavy module (it instantiates the
Qwen model at import time), so this stays collectable in the CI interpreter.
"""
from __future__ import annotations

import re
from pathlib import Path

from opencohost.core.observability.health_monitor import QwenProcessManager


def _server_app_id() -> str:
    server = Path(__file__).resolve().parents[1] / "opencohost" / "server_qwen.py"
    text = server.read_text(encoding="utf-8")
    m = re.search(r'^APP_ID\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    assert m, "APP_ID assignment not found in server_qwen.py"
    return m.group(1)


def test_app_id_contract_pinned():
    """server_qwen.APP_ID must equal QwenProcessManager.APP_ID.

    The drift between these two constants (server said 'voiceai-qwen-tts', the manager
    expected 'opencohost-qwen-tts') made every heavy-TTS request silently fall back to
    Edge. This pins them together so the regression cannot return.
    """
    assert _server_app_id() == QwenProcessManager.APP_ID == "opencohost-qwen-tts"
