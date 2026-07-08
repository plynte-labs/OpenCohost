"""P5 — Qwen heavy-TTS language slot wiring (kira_bilingual_e2e_20260705).

server_qwen.py instantiates the Qwen model at import time (see
test_qwen_startup_selfcheck.py's docstring), so this stays collectable in the
CI interpreter by reading the source text directly rather than importing the
module -- same trick already used by that file for its APP_ID contract pin.
"""
from __future__ import annotations

import re
from pathlib import Path


def _server_source() -> str:
    server = Path(__file__).resolve().parents[1] / "opencohost" / "server_qwen.py"
    return server.read_text(encoding="utf-8")


def test_qwen_language_wired_to_active_accessor():
    """The generate_voice_clone call must read the active locale's
    tts.qwen_language slot, not the hard-coded 'Spanish' literal this
    regression previously carried."""
    source = _server_source()
    assert "language=i18n_active.qwen_language()" in source
    assert 'language="Spanish"' not in source


def test_qwen_language_call_site_still_inside_generate_voice_clone():
    """Pin the call site so a future refactor cannot silently move/duplicate
    the accessor call away from the real Qwen synthesis call."""
    source = _server_source()
    m = re.search(
        r"generate_voice_clone\(\s*text=texto,\s*language=i18n_active\.qwen_language\(\),",
        source,
    )
    assert m, "language=i18n_active.qwen_language() must be the generate_voice_clone() argument"
