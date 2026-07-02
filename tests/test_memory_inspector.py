"""Tests for MotorVocalIA.memory_inspector_snapshot() — read-only, privacy-gated
snapshot of session memory for the "Memoria de Kira" UI inspector (Slice B,
cards_memory_readonly_panels_20260701).

Content policy (fail-closed, no cross-module string heuristics):
  - user-slot entries: 'content' key present ONLY when source == "direct"
  - assistant-slot entries: 'content' key present when source in {"direct", "ptt"}
  - everything else (chat/accumulated/kira-agenda*/unknown/missing source):
    NO 'content' key at all.

Snapshot-then-release: entries + digest stats are copied to plain dicts under
_history_lock, then the lock is released before formatting (precedent:
scout_digest in llm_engine.py). Digest exposes stats only — never line text.
"""

import queue
from collections import Counter
from unittest.mock import MagicMock

import pytest


def _make_motor():
    """Construct a MotorVocalIA with all heavy I/O mocked out.

    Mirrors the pattern in test_pipeline_memory.py / test_heavy_model_inference_recovery.py:
    create via constructor (no run()), then assign mock dependencies.
    """
    log_q = queue.Queue()

    def ui_cb(event):
        pass

    from opencohost.core.llm_engine import MotorVocalIA
    motor = MotorVocalIA(log_q, ui_cb)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor


class TestMemoryInspectorSnapshotEmpty:
    def test_empty_historial_returns_empty_entries(self):
        motor = _make_motor()
        snapshot = motor.memory_inspector_snapshot()
        assert snapshot["entries"] == []

    def test_empty_historial_source_breakdown_is_empty_counter(self):
        motor = _make_motor()
        snapshot = motor.memory_inspector_snapshot()
        assert snapshot["source_breakdown"] == Counter()

    def test_empty_digest_stats(self):
        motor = _make_motor()
        snapshot = motor.memory_inspector_snapshot()
        assert snapshot["digest"]["line_count"] == 0
        assert snapshot["digest"]["total_chars"] == 0


class TestMemoryInspectorContentPolicy:
    def test_direct_user_slot_has_content(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "hola kira", "source": "direct"})
        snapshot = motor.memory_inspector_snapshot()
        entry = snapshot["entries"][0]
        assert entry["content"] == "hola kira"
        assert entry["content_chars"] == len("hola kira")

    def test_direct_assistant_slot_has_content(self):
        motor = _make_motor()
        motor.historial.append({"role": "assistant", "content": "hola humano", "source": "direct"})
        snapshot = motor.memory_inspector_snapshot()
        entry = snapshot["entries"][0]
        assert entry["content"] == "hola humano"

    def test_ptt_assistant_slot_has_content(self):
        motor = _make_motor()
        motor.historial.append({"role": "assistant", "content": "kira on-air reply", "source": "ptt"})
        snapshot = motor.memory_inspector_snapshot()
        entry = snapshot["entries"][0]
        assert entry["content"] == "kira on-air reply"

    def test_ptt_user_slot_lacks_content(self):
        """PTT commits the full internal template as the user slot — never expose it."""
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "TAREA: respondé al aire...", "source": "ptt"})
        snapshot = motor.memory_inspector_snapshot()
        entry = snapshot["entries"][0]
        assert "content" not in entry

    def test_chat_entries_lack_content(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "viewer chat text", "source": "chat"})
        motor.historial.append({"role": "assistant", "content": "reply to chat", "source": "chat"})
        snapshot = motor.memory_inspector_snapshot()
        assert "content" not in snapshot["entries"][0]
        assert "content" not in snapshot["entries"][1]

    def test_accumulated_entries_lack_content(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "bundled viewer chat", "source": "accumulated"})
        motor.historial.append({"role": "assistant", "content": "reply", "source": "accumulated"})
        snapshot = motor.memory_inspector_snapshot()
        assert "content" not in snapshot["entries"][0]
        assert "content" not in snapshot["entries"][1]

    def test_kira_agenda_entries_lack_content(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "[agenda segura: prompt interno omitido]", "source": "kira-agenda"})
        motor.historial.append({"role": "assistant", "content": "agenda reply", "source": "kira-agenda"})
        snapshot = motor.memory_inspector_snapshot()
        assert "content" not in snapshot["entries"][0]
        assert "content" not in snapshot["entries"][1]

    def test_kira_agenda_variant_source_lacks_content(self):
        """kira-agenda* variants (e.g. kira-agenda-tick) are also fail-closed."""
        motor = _make_motor()
        motor.historial.append({"role": "assistant", "content": "agenda reply", "source": "kira-agenda-tick"})
        snapshot = motor.memory_inspector_snapshot()
        assert "content" not in snapshot["entries"][0]

    def test_unknown_source_lacks_content(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "mystery", "source": "future-x"})
        snapshot = motor.memory_inspector_snapshot()
        assert "content" not in snapshot["entries"][0]

    def test_missing_source_lacks_content(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "no source tag"})
        snapshot = motor.memory_inspector_snapshot()
        assert "content" not in snapshot["entries"][0]

    def test_direct_user_content_chars_computed_even_when_hidden(self):
        """content_chars must always be present regardless of the content-visibility gate."""
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "hidden text here", "source": "chat"})
        snapshot = motor.memory_inspector_snapshot()
        entry = snapshot["entries"][0]
        assert entry["content_chars"] == len("hidden text here")
        assert "content" not in entry


class TestMemoryInspectorEntryShape:
    def test_entry_has_turn_index_role_source_content_chars(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "hola", "source": "direct"})
        motor.historial.append({"role": "assistant", "content": "hi", "source": "direct"})
        snapshot = motor.memory_inspector_snapshot()
        entries = snapshot["entries"]
        assert entries[0]["turn_index"] == 0
        assert entries[0]["role"] == "user"
        assert entries[0]["source"] == "direct"
        assert entries[1]["turn_index"] == 1
        assert entries[1]["role"] == "assistant"

    def test_turn_index_preserves_historial_order(self):
        motor = _make_motor()
        for i in range(4):
            motor.historial.append({"role": "user", "content": f"q{i}", "source": "direct"})
            motor.historial.append({"role": "assistant", "content": f"a{i}", "source": "direct"})
        snapshot = motor.memory_inspector_snapshot()
        indices = [e["turn_index"] for e in snapshot["entries"]]
        assert indices == list(range(8))


class TestMemoryInspectorSourceBreakdown:
    def test_counter_covers_all_entry_sources(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "a", "source": "direct"})
        motor.historial.append({"role": "assistant", "content": "b", "source": "direct"})
        motor.historial.append({"role": "user", "content": "c", "source": "ptt"})
        motor.historial.append({"role": "assistant", "content": "d", "source": "ptt"})
        motor.historial.append({"role": "user", "content": "e", "source": "chat"})
        snapshot = motor.memory_inspector_snapshot()
        assert snapshot["source_breakdown"] == Counter(
            {"direct": 2, "ptt": 2, "chat": 1}
        )

    def test_counter_includes_unexpected_sources(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "a", "source": "future-x"})
        snapshot = motor.memory_inspector_snapshot()
        assert snapshot["source_breakdown"]["future-x"] == 1

    def test_counter_includes_missing_source_as_none(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "a"})
        snapshot = motor.memory_inspector_snapshot()
        assert snapshot["source_breakdown"][None] == 1


class TestMemoryInspectorDigestStats:
    def test_digest_stats_only_no_line_text(self):
        motor = _make_motor()
        motor._memory_digest.append("contexto: something → Kira: reply.")
        motor._memory_digest.append("contexto: other → Kira: reply2.")
        snapshot = motor.memory_inspector_snapshot()
        digest = snapshot["digest"]
        assert digest["line_count"] == 2
        assert digest["total_chars"] == sum(len(l) for l in motor._memory_digest.lines)
        assert set(digest.keys()) == {"line_count", "total_chars", "max_chars"}

    def test_digest_max_chars_matches_configured_cap(self):
        motor = _make_motor()
        snapshot = motor.memory_inspector_snapshot()
        assert snapshot["digest"]["max_chars"] == motor._memory_digest._max_chars


class TestMemoryInspectorSnapshotSafety:
    def test_snapshot_does_not_mutate_historial(self):
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "hola", "source": "direct"})
        motor.historial.append({"role": "assistant", "content": "hi", "source": "direct"})
        before = list(motor.historial)
        motor.memory_inspector_snapshot()
        after = list(motor.historial)
        assert before == after

    def test_snapshot_does_not_mutate_digest(self):
        motor = _make_motor()
        motor._memory_digest.append("contexto: x → Kira: y.")
        before = list(motor._memory_digest.lines)
        motor.memory_inspector_snapshot()
        after = list(motor._memory_digest.lines)
        assert before == after

    def test_snapshot_entries_are_independent_copies(self):
        """Mutating a returned entry dict must not affect historial."""
        motor = _make_motor()
        motor.historial.append({"role": "user", "content": "hola", "source": "direct"})
        snapshot = motor.memory_inspector_snapshot()
        snapshot["entries"][0]["content"] = "tampered"
        assert motor.historial[0]["content"] == "hola"
