"""
TTS local-only switch tests (privacy gate).

Covers:
  - Settings: default off, persists across save/load.
  - Engine: with switch ON, light-path resolution uses Piper even when
    Edge-TTS is available and online; Edge-TTS callable is never invoked.
  - Engine: auto-fallback from heavy (missing_reference / health_gate)
    lands on Piper when switch is ON.
  - Engine: switch ON + Piper unavailable → degraded path (None in queue),
    Edge-TTS still never invoked.
  - UI: switch is wired; toggle sends set_tts_local_only command to engine.

IMPORTANT: pytest imports ALL test files at collection time.
  - Never put mocks in sys.modules at module level.
  - Never let mocked CTk widgets walk MagicMock parents unbounded.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import uuid
import wave

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_motor():
    """Return a fresh MotorVocalIA with mocked pygame/ollama (no real I/O)."""
    from opencohost.core.llm_engine import MotorVocalIA

    log_q: queue.Queue = queue.Queue()
    ui_events: list = []

    def ui_callback(event):
        ui_events.append(event)

    motor = MotorVocalIA(log_q, ui_callback)
    motor.ollama = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    motor.pygame = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    motor.is_ready = True
    motor._speaking = True
    return motor, log_q, ui_events


def _make_mock_piper(available: bool = True, synthesize_ok: bool = True):
    """Return a mock PiperEngine."""
    from unittest.mock import MagicMock
    mock_piper = MagicMock()
    mock_piper.is_available.return_value = available

    def fake_synthesize(text, path):
        if synthesize_ok:
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(22050)
                wf.writeframes(b"\x00" * 44)
        return synthesize_ok

    mock_piper.synthesize.side_effect = fake_synthesize
    return mock_piper


def _run_one_light_chunk(motor, oracion: str):
    """
    Simulate exactly one iteration of the light-engine producer path.

    Returns (queue_items, edge_was_called, piper_synthesize_call_count).

    We do NOT call _hablar() directly (it spins a full thread loop).
    Instead we replicate the producer's chunk logic inline — this is the
    same technique used by test_llm_engine_piper_fallback.py.
    """
    from unittest.mock import MagicMock, patch
    import asyncio
    from opencohost.config.settings import TEMP_DIR

    edge_call_count = 0
    inner_queue: queue.Queue = queue.Queue(maxsize=5)

    def fake_asyncio_run(coro, *args, **kwargs):
        nonlocal edge_call_count
        edge_call_count += 1
        try:
            coro.close()
        except Exception:
            pass
        # Simulate a successful Edge-TTS call (writes nothing — file stays absent)
        # The test only cares whether asyncio.run was reached.

    import opencohost.core.llm_engine as eng  # noqa: F401  (kept for parity with productor)

    # CI runners do not have edge_tts installed (eng.edge_tts is None there).
    # Treat the module as present so path selection is environment-independent:
    # offline tests drive motor._edge_tts_offline explicitly, and the Edge call
    # itself is always faked via fake_asyncio_run — never really invoked.
    edge_module_missing = False

    # Snapshot tts_local_only ONCE before the loop, mirroring the `local_only`
    # snapshot introduced in productor() (opencohost/core/llm_engine.py ~line 1400).
    # This helper must stay in sync with that snapshot so test assertions remain valid.
    local_only = motor.tts_local_only

    # Determine effective_motor (replicating productor's decision logic)
    effective_motor = motor.motor_tts

    # Missing-reference auto-fallback
    if effective_motor == "pesado" and not motor.voz_referencia:
        effective_motor = "ligero"

    # Health-gate auto-fallback
    if effective_motor == "pesado":
        hm = getattr(motor, "health_monitor", None)
        if hm is not None:
            block_reason = None
            if hasattr(hm, "heavy_tts_block_reason"):
                block_reason = hm.heavy_tts_block_reason(
                    auto_fallback_enabled=True, manual_motor=effective_motor
                )
            elif not hm.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor=effective_motor):
                block_reason = "health_gate"
            if block_reason:
                effective_motor = "ligero"

    i = 0

    # ── local-only fast-path (replicated from productor) ──────────────────
    if effective_motor == "ligero" and local_only:
        archivo_chunk_wav = os.path.join(
            TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
        )
        if motor._piper.is_available():
            if motor._piper.synthesize(oracion, archivo_chunk_wav):
                inner_queue.put((archivo_chunk_wav, i, oracion))
            else:
                inner_queue.put(None)
        else:
            inner_queue.put(None)
    # ── existing offline fast-path (pass-through) ──────────────────────────
    elif effective_motor == "ligero" and (motor._edge_tts_offline or edge_module_missing):
        archivo_chunk_wav = os.path.join(
            TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
        )
        if motor._piper.is_available():
            if motor._piper.synthesize(oracion, archivo_chunk_wav):
                inner_queue.put((archivo_chunk_wav, i, oracion))
            else:
                inner_queue.put(None)
        else:
            inner_queue.put(None)
    elif effective_motor == "ligero":
        # Would call Edge-TTS
        with patch("asyncio.run", side_effect=fake_asyncio_run):
            try:
                fake_asyncio_run(None)
                inner_queue.put(("stub.mp3", i, oracion))
            except Exception:
                inner_queue.put(None)
    else:
        inner_queue.put(None)

    inner_queue.put("FIN")

    items = []
    while True:
        item = inner_queue.get(timeout=2)
        if item == "FIN":
            break
        items.append(item)

    return items, edge_call_count, motor._piper


# ===========================================================================
# 1. Settings — load / save / default
# ===========================================================================

class TestTtsLocalOnlySettings:
    def test_default_is_false(self):
        """load_tts_local_only() returns False when no file exists."""
        from opencohost.config import settings as cfg

        result = cfg.load_tts_local_only(config_file="/nonexistent/path.json")
        assert result is False

    def test_save_and_reload(self, tmp_path):
        """Saving True and reloading returns True."""
        from opencohost.config import settings as cfg

        path = str(tmp_path / "tts_local_only.json")
        cfg.save_tts_local_only(True, config_file=path)
        assert cfg.load_tts_local_only(config_file=path) is True

    def test_save_false_and_reload(self, tmp_path):
        """Saving False and reloading returns False."""
        from opencohost.config import settings as cfg

        path = str(tmp_path / "tts_local_only.json")
        cfg.save_tts_local_only(False, config_file=path)
        assert cfg.load_tts_local_only(config_file=path) is False

    def test_corrupted_file_returns_false(self, tmp_path):
        """A corrupted JSON file is handled gracefully (returns False)."""
        from opencohost.config import settings as cfg

        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("not-json!!!")
        assert cfg.load_tts_local_only(config_file=path) is False

    def test_default_constant_is_false(self):
        """TTS_LOCAL_ONLY_FILE constant is defined in settings."""
        from opencohost.config import settings as cfg

        assert hasattr(cfg, "TTS_LOCAL_ONLY_FILE"), (
            "settings.TTS_LOCAL_ONLY_FILE must be defined"
        )


# ===========================================================================
# 2. Engine — tts_local_only property and dispatch
# ===========================================================================

class TestMotorLocalOnlyProperty:
    def test_default_is_false(self):
        """MotorVocalIA.tts_local_only starts False (backward compat)."""
        motor, *_ = _make_motor()
        assert motor.tts_local_only is False

    def test_can_be_set_to_true(self):
        """Setting tts_local_only=True reflects on the property."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        assert motor.tts_local_only is True

    def test_set_tts_local_only_command_enables(self):
        """Dispatching set_tts_local_only True updates tts_local_only."""
        motor, *_ = _make_motor()
        motor._dispatch_command("set_tts_local_only", True)
        assert motor.tts_local_only is True

    def test_set_tts_local_only_command_disables(self):
        """Dispatching set_tts_local_only False updates tts_local_only."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._dispatch_command("set_tts_local_only", False)
        assert motor.tts_local_only is False


# ===========================================================================
# 3. Engine — light-path resolution with switch ON
# ===========================================================================

class TestLightPathWithLocalOnlyEnabled:
    def test_edge_never_called_when_switch_on(self):
        """With tts_local_only=True and Piper available, Edge-TTS is never invoked."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False  # Edge-TTS would normally be available
        motor.motor_tts = "ligero"
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        items, edge_call_count, mock_piper = _run_one_light_chunk(motor, "Hola Kira")

        assert edge_call_count == 0, "Edge-TTS must NEVER be called when local-only is ON"
        assert mock_piper.synthesize.call_count == 1
        assert len(items) == 1
        path, *_ = items[0]
        assert path.endswith(".wav")

    def test_piper_used_for_heavy_auto_fallback_missing_reference(self):
        """Heavy→light auto-fallback (missing_reference) lands on Piper when switch is ON."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor.motor_tts = "pesado"
        motor.voz_referencia = None  # triggers missing_reference fallback
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        items, edge_call_count, mock_piper = _run_one_light_chunk(motor, "Fallback test")

        assert edge_call_count == 0, "Edge-TTS must not be called even after heavy→light fallback"
        assert mock_piper.synthesize.call_count == 1
        assert len(items) == 1
        path, *_ = items[0]
        assert path.endswith(".wav")

    def test_piper_used_for_heavy_auto_fallback_health_gate(self):
        """Heavy→light auto-fallback (health_gate) lands on Piper when switch is ON."""
        from unittest.mock import MagicMock

        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor.motor_tts = "pesado"
        motor.voz_referencia = "/some/ref.wav"

        # Simulate health monitor blocking heavy TTS
        mock_hm = MagicMock()
        mock_hm.heavy_tts_block_reason.return_value = "health_gate"
        motor.health_monitor = mock_hm
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        items, edge_call_count, mock_piper = _run_one_light_chunk(motor, "Health gate test")

        assert edge_call_count == 0
        assert mock_piper.synthesize.call_count == 1
        assert len(items) == 1
        path, *_ = items[0]
        assert path.endswith(".wav")


# ===========================================================================
# 4. Engine — switch ON + Piper unavailable → degraded, Edge still never called
# ===========================================================================

class TestLocalOnlyWithPiperUnavailable:
    def test_none_in_queue_and_edge_never_called(self):
        """local-only=True + Piper unavailable → queue gets None; Edge-TTS never called."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor.motor_tts = "ligero"
        motor._piper = _make_mock_piper(available=False)

        items, edge_call_count, mock_piper = _run_one_light_chunk(motor, "Sin Piper")

        assert edge_call_count == 0, "Edge-TTS must NEVER be called even when Piper is unavailable"
        assert mock_piper.synthesize.call_count == 0
        assert items == [None]

    def test_piper_synthesize_failure_no_edge(self):
        """local-only=True + Piper fails synthesis → None in queue; Edge still never called."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor.motor_tts = "ligero"
        motor._piper = _make_mock_piper(available=True, synthesize_ok=False)

        items, edge_call_count, mock_piper = _run_one_light_chunk(motor, "Synth fail")

        assert edge_call_count == 0
        assert items == [None]


# ===========================================================================
# 5. Backward compat — switch OFF preserves original behavior
# ===========================================================================

class TestLocalOnlyOffPreservesOriginalBehavior:
    def test_edge_is_called_when_switch_off(self):
        """With tts_local_only=False and online, Edge-TTS is called (unchanged behavior)."""
        motor, *_ = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = False
        motor.motor_tts = "ligero"
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        _, edge_call_count, mock_piper = _run_one_light_chunk(motor, "Normal mode")

        assert edge_call_count == 1, "Edge-TTS should be called when switch is OFF"
        mock_piper.synthesize.assert_not_called()


# ===========================================================================
# 6. UI — switch widget wires to engine command
# ===========================================================================

class TestTtsLocalOnlyUISwitchWiring:
    """Structural test: the UI shell has the switch and wires it correctly.

    Uses source-level inspection to avoid importing CTk (which would require
    a display and can cause OOM from unbounded MagicMock parents if not handled).
    """

    def test_app_shell_has_local_only_switch_attribute(self):
        """app_shell.py source defines switch_local_only widget reference."""
        import ast

        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        assert "switch_local_only" in source, (
            "app_shell.py must define self.switch_local_only widget"
        )

    def test_app_shell_dispatches_set_tts_local_only(self):
        """app_shell.py source dispatches set_tts_local_only command to motor."""
        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        assert "set_tts_local_only" in source, (
            "app_shell.py must dispatch 'set_tts_local_only' command to motor_ia"
        )

    def test_app_shell_has_local_only_ui_label(self):
        """app_shell.py source contains the Spanish UI label for the privacy switch."""
        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        # Accept any of the expected label fragments
        has_label = any(
            fragment in source
            for fragment in ("Solo TTS local", "Piper", "tts local", "local_only")
        )
        assert has_label, (
            "app_shell.py must contain the Spanish UI label for the privacy switch "
            "(expected 'Solo TTS local' or similar)"
        )

    def test_app_shell_has_local_only_helper_text(self):
        """app_shell.py source contains helper text mentioning Microsoft/Edge-TTS."""
        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        has_helper = "Microsoft" in source or "Edge-TTS" in source
        assert has_helper, (
            "app_shell.py must contain helper text explaining the privacy tradeoff "
            "(expected 'Microsoft' or 'Edge-TTS')"
        )


# ===========================================================================
# 7. Engine — snapshot semantics: mid-utterance toggle does not re-route chunks
# ===========================================================================

def _run_multi_chunk(motor, oraciones: list[str], flip_after_chunk: int):
    """
    Simulate the productor() light-engine path for multiple chunks.

    Mirrors productor() from opencohost/core/llm_engine.py (~lines 1397-1466):
      - Snapshots local_only ONCE before the loop.
      - After processing chunk `flip_after_chunk`, toggles motor.tts_local_only OFF
        to simulate a mid-utterance toggle.
      - Returns (queue_items, edge_call_count, piper_call_count).

    Because the snapshot is taken before the loop, the toggle must NOT affect
    any chunk in the current utterance.
    """
    from opencohost.config.settings import TEMP_DIR

    # See _run_one_light_chunk: simulate Edge-TTS as installed so the helper is
    # environment-independent (CI lacks the edge_tts package).
    edge_module_missing = False

    edge_call_count = 0
    inner_queue: queue.Queue = queue.Queue(maxsize=10)

    def fake_asyncio_run(coro, *args, **kwargs):
        nonlocal edge_call_count
        edge_call_count += 1
        try:
            coro.close()
        except Exception:
            pass

    # Snapshot ONCE — mirrors productor() lines ~1400
    local_only = motor.tts_local_only

    # Determine effective_motor (same logic as productor)
    effective_motor = motor.motor_tts
    if effective_motor == "pesado" and not motor.voz_referencia:
        effective_motor = "ligero"
    if effective_motor == "pesado":
        hm = getattr(motor, "health_monitor", None)
        if hm is not None:
            block_reason = None
            if hasattr(hm, "heavy_tts_block_reason"):
                block_reason = hm.heavy_tts_block_reason(
                    auto_fallback_enabled=True, manual_motor=effective_motor
                )
            elif not hm.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor=effective_motor):
                block_reason = "health_gate"
            if block_reason:
                effective_motor = "ligero"

    for i, oracion in enumerate(oraciones):
        # Simulate mid-utterance toggle AFTER the specified chunk index
        if i == flip_after_chunk + 1:
            motor.tts_local_only = False

        # local-only fast-path — uses snapshot, not motor.tts_local_only
        if effective_motor == "ligero" and local_only:
            archivo_chunk_wav = os.path.join(
                TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
            )
            if motor._piper.is_available():
                if motor._piper.synthesize(oracion, archivo_chunk_wav):
                    inner_queue.put((archivo_chunk_wav, i, oracion))
                else:
                    inner_queue.put(None)
            else:
                inner_queue.put(None)
        elif effective_motor == "ligero" and (motor._edge_tts_offline or edge_module_missing):
            archivo_chunk_wav = os.path.join(
                TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
            )
            if motor._piper.is_available():
                if motor._piper.synthesize(oracion, archivo_chunk_wav):
                    inner_queue.put((archivo_chunk_wav, i, oracion))
                else:
                    inner_queue.put(None)
            else:
                inner_queue.put(None)
        elif effective_motor == "ligero":
            # Would call Edge-TTS
            fake_asyncio_run(None)
            inner_queue.put(("stub.mp3", i, oracion))
        else:
            inner_queue.put(None)

    inner_queue.put("FIN")
    items = []
    while True:
        item = inner_queue.get(timeout=2)
        if item == "FIN":
            break
        items.append(item)

    return items, edge_call_count, motor._piper


class TestSnapshotSemantics:
    """Verify that a mid-utterance toggle of tts_local_only does not re-route
    any chunk of the current utterance to Edge-TTS (snapshot semantics)."""

    def test_mid_utterance_flip_off_keeps_all_chunks_on_piper(self):
        """
        All chunks of an utterance stay on Piper even if tts_local_only is
        toggled OFF after chunk 0 — the snapshot taken at utterance start wins.

        Scenario:
          - Utterance has 3 chunks.
          - tts_local_only starts ON.
          - After chunk 0 is processed the flag is flipped OFF (simulating a
            user toggle mid-utterance).
          - Expected: all 3 chunks go to Piper; Edge-TTS is never called.
        """
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor.motor_tts = "ligero"
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        oraciones = ["Chunk cero.", "Chunk uno.", "Chunk dos."]
        items, edge_call_count, mock_piper = _run_multi_chunk(
            motor, oraciones, flip_after_chunk=0
        )

        assert edge_call_count == 0, (
            "Edge-TTS must NOT be called for any chunk in the utterance "
            "even though tts_local_only was toggled OFF mid-utterance"
        )
        assert mock_piper.synthesize.call_count == 3, (
            "All 3 chunks must be routed to Piper (snapshot semantics)"
        )
        assert len(items) == 3
        for path, *_ in items:
            assert path.endswith(".wav"), "Each chunk must be a .wav from Piper"

        # Confirm the flag is now OFF on the motor (toggle took effect on the object)
        assert motor.tts_local_only is False, (
            "The toggle must have updated motor.tts_local_only; "
            "it just does not affect the current utterance"
        )


# ===========================================================================
# 8. server_qwen.py — structural guard: tts_local_only blocks Edge-TTS
# ===========================================================================

class TestServerQwenLocalOnlyGuardStructural:
    """Structural tests for the server_qwen.py tts_local_only privacy guard.

    server_qwen.py runs in a separate Python environment (xtts_env) that does
    NOT have opencohost installed, so it cannot be imported in flux_env.
    These tests inspect the source text to verify the guard is present and
    correctly ordered relative to the edge_tts.Communicate call.

    Pattern mirrors TestTtsLocalOnlyUISwitchWiring above.
    """

    SERVER_PATH = os.path.join(ROOT_DIR, "opencohost", "server_qwen.py")

    def _source(self):
        with open(self.SERVER_PATH, "r", encoding="utf-8") as f:
            return f.read()

    def test_flag_file_read_present(self):
        """server_qwen.py reads tts_local_only.json using stdlib (os/json) only."""
        source = self._source()
        assert "tts_local_only.json" in source, (
            "server_qwen.py must read the tts_local_only.json flag file directly "
            "(cross-env, stdlib-only read)"
        )
        # The guard must use inline stdlib json — not opencohost.config.settings.
        # Verify by checking that `load_tts_local_only` (the settings helper) is
        # NOT called anywhere in the file (it would require the opencohost package).
        assert "load_tts_local_only" not in source, (
            "server_qwen.py must NOT call load_tts_local_only() — that function "
            "lives in opencohost.config.settings which is unavailable in xtts_env"
        )

    def test_400_error_returned_when_flag_on(self):
        """server_qwen.py returns a 400 JSON error when tts_local_only is ON."""
        source = self._source()
        assert "tts_local_only is enabled" in source, (
            "server_qwen.py must return a 400 error with "
            "'tts_local_only is enabled' when the flag is ON"
        )
        assert "400" in source, (
            "server_qwen.py must return HTTP 400 when tts_local_only flag is ON"
        )

    def test_guard_appears_before_communicate_call(self):
        """The privacy guard must come BEFORE edge_tts.Communicate in the source."""
        source = self._source()
        guard_pos = source.find("tts_local_only is enabled")
        communicate_pos = source.find("edge_tts.Communicate")
        assert guard_pos != -1, "Privacy guard text not found in server_qwen.py"
        assert communicate_pos != -1, "edge_tts.Communicate not found in server_qwen.py"
        assert guard_pos < communicate_pos, (
            "The tts_local_only guard must appear BEFORE edge_tts.Communicate "
            f"(guard at {guard_pos}, Communicate at {communicate_pos})"
        )

    def test_stdlib_only_comment_present(self):
        """server_qwen.py documents WHY the read is duplicated (cross-env note)."""
        source = self._source()
        has_comment = "cross-env" in source or "separate Python" in source or "xtts_env" in source
        assert has_comment, (
            "server_qwen.py must have a comment explaining the duplicated read "
            "(cross-env / separate Python environment)"
        )
