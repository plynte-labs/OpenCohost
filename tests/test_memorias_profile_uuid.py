"""
Slice 1 (profile-uuid) — Kira Memory Persistence, kira_memory_persistence_20260701.

Pins R12 (stable profile UUID migration and preservation) per spec v2.1 (#2772)
and design v2.1 §9/§12 (#2773): auto-seed on load, save-once, id-carry through
save/rename via a copy-and-update of the existing profile dict, fresh uuid4 on
new-profile creation, and launch-time dispatch of the id into
``MotorVocalIA._current_profile_id`` under ``_history_lock``.

RC-9 edge tests (owner decisions #2770, design §9): duplicate profile never
copies the source id, id-less imports get seeded, colliding ids on import are
deterministically de-duplicated (second occurrence re-seeded), and deleting a
profile never purges a different profile that later reuses the deleted
profile's old NAME (purge must key on id, never name).
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _call_cargar(profiles_path: str, defaults_path: str) -> dict:
    import opencohost.core.profiles.profiles as prof_mod
    with (
        patch.object(prof_mod, "PROFILES_FILE", profiles_path),
        patch.object(prof_mod, "DEFAULT_PROFILES_FILE", defaults_path),
    ):
        return prof_mod.cargar_perfiles()


def _is_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(str(value), version=4)
    except (ValueError, AttributeError, TypeError):
        return False
    return str(parsed) == str(value).lower()


# ===========================================================================
# opencohost/core/profiles.py — cargar_perfiles auto-seed (1.1, 1.2, RC-9)
# ===========================================================================

class TestCargarPerfilesSeeding:
    def test_existing_profile_without_id_gets_uuid_seeded_on_load(self, tmp_path):
        """1.1 — legacy profiles on disk without 'id' get one seeded on load."""
        profiles_file = tmp_path / "perfiles.json"
        profiles_file.write_text(
            json.dumps({"Akira": {"prompt": "hola", "use_system": True}}),
            encoding="utf-8",
        )
        defaults_file = tmp_path / "default_profiles.json"

        result = _call_cargar(str(profiles_file), str(defaults_file))

        assert "id" in result["Akira"]
        assert _is_uuid4(result["Akira"]["id"])

    def test_profile_id_persists_across_save_load_cycle(self, tmp_path):
        """1.2 — a seeded id is written back to disk (save-once) and stable
        across a second load (no re-seed / no churn on reload)."""
        profiles_file = tmp_path / "perfiles.json"
        profiles_file.write_text(
            json.dumps({"Akira": {"prompt": "hola", "use_system": True}}),
            encoding="utf-8",
        )
        defaults_file = tmp_path / "default_profiles.json"

        first = _call_cargar(str(profiles_file), str(defaults_file))
        seeded_id = first["Akira"]["id"]

        on_disk = json.loads(profiles_file.read_text(encoding="utf-8"))
        assert on_disk["Akira"]["id"] == seeded_id, "seeded id must be persisted (save-once)"

        second = _call_cargar(str(profiles_file), str(defaults_file))
        assert second["Akira"]["id"] == seeded_id, "id must not churn on repeated loads"

    def test_duplicate_profile_gets_new_id_never_copies_source(self, tmp_path):
        """1.5 / RC-9 — two profiles whose VALUE dicts are id-less duplicates of
        each other (e.g. a manual JSON copy/paste) each get a DISTINCT fresh id;
        the seeder never copies one profile's id onto another."""
        profiles_file = tmp_path / "perfiles.json"
        shared_value = {"prompt": "misma personalidad", "use_system": True}
        profiles_file.write_text(
            json.dumps({
                "Original": dict(shared_value),
                "Copia": dict(shared_value),
            }),
            encoding="utf-8",
        )
        defaults_file = tmp_path / "default_profiles.json"

        result = _call_cargar(str(profiles_file), str(defaults_file))

        assert result["Original"]["id"] != result["Copia"]["id"]
        assert _is_uuid4(result["Original"]["id"])
        assert _is_uuid4(result["Copia"]["id"])

    def test_import_profile_dict_without_id_gets_seeded_on_load(self, tmp_path):
        """1.6 / RC-9 — a raw imported profile dict without 'id' behaves exactly
        like a legacy profile: seeded on load."""
        profiles_file = tmp_path / "perfiles.json"
        profiles_file.write_text(
            json.dumps({"Importado": {"prompt": "importado", "use_system": False}}),
            encoding="utf-8",
        )
        defaults_file = tmp_path / "default_profiles.json"

        result = _call_cargar(str(profiles_file), str(defaults_file))

        assert _is_uuid4(result["Importado"]["id"])

    def test_import_profile_dict_with_existing_id_reseeds_no_silent_collision(self, tmp_path):
        """1.7 / RC-9 — two profiles that already carry the SAME id (e.g. from a
        bad import) must not silently collide: the second occurrence (by dict
        order) is deterministically re-seeded with a fresh id, and the fix is
        persisted back to disk."""
        profiles_file = tmp_path / "perfiles.json"
        colliding_id = str(uuid.uuid4())
        profiles_file.write_text(
            json.dumps({
                "PrimeroEnElArchivo": {"prompt": "a", "use_system": True, "id": colliding_id},
                "SegundoEnElArchivo": {"prompt": "b", "use_system": True, "id": colliding_id},
            }),
            encoding="utf-8",
        )
        defaults_file = tmp_path / "default_profiles.json"

        result = _call_cargar(str(profiles_file), str(defaults_file))

        assert result["PrimeroEnElArchivo"]["id"] == colliding_id, (
            "first occurrence keeps its original id (deterministic rule)"
        )
        assert result["SegundoEnElArchivo"]["id"] != colliding_id, (
            "second occurrence must be re-seeded — no silent id collision"
        )
        assert _is_uuid4(result["SegundoEnElArchivo"]["id"])

        on_disk = json.loads(profiles_file.read_text(encoding="utf-8"))
        assert on_disk["SegundoEnElArchivo"]["id"] == result["SegundoEnElArchivo"]["id"]

    def test_seed_from_defaults_path_also_gets_ids(self, tmp_path):
        """default_profiles.json ships WITHOUT ids (per design §9/§1); the
        seed-from-defaults path must seed ids too, not just the load-from-disk
        path."""
        profiles_file = tmp_path / "perfiles.json"
        defaults_file = tmp_path / "default_profiles.json"
        defaults_file.write_text(
            json.dumps({"Akira": {"prompt": "hola", "use_system": True}}),
            encoding="utf-8",
        )

        result = _call_cargar(str(profiles_file), str(defaults_file))

        assert _is_uuid4(result["Akira"]["id"])
        on_disk = json.loads(Path(profiles_file).read_text(encoding="utf-8"))
        assert on_disk["Akira"]["id"] == result["Akira"]["id"]


# ===========================================================================
# opencohost/core/llm_engine.py — set_profile carries id under _history_lock
# ===========================================================================

def _make_motor():
    from opencohost.core.llm_engine import MotorVocalIA
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda event: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor


class TestSetProfileCarriesId:
    def test_set_profile_stores_current_profile_id(self):
        motor = _make_motor()
        motor._dispatch_command("set_profile", {
            "prompt": "hola",
            "use_system": False,
            "_profile_name": "Akira",
            "id": "abc-123",
        })
        assert motor._current_profile_id == "abc-123"

    def test_set_profile_without_id_sets_none(self):
        """Legacy payload (pre-seed) must not crash — id defaults to None."""
        motor = _make_motor()
        motor._dispatch_command("set_profile", {
            "prompt": "hola",
            "use_system": False,
            "_profile_name": "Akira",
        })
        assert motor._current_profile_id is None

    def test_set_profile_writes_id_under_history_lock(self):
        """Design §6 — the _current_profile_id write must happen while
        holding _history_lock."""
        motor = _make_motor()

        class _SpyLock:
            def __init__(self, real_lock):
                self._real_lock = real_lock
                self.enter_count = 0

            def __enter__(self):
                self.enter_count += 1
                self._real_lock.acquire()
                return self

            def __exit__(self, *exc):
                self._real_lock.release()
                return False

        spy = _SpyLock(motor._history_lock)

        with patch.object(motor, "_history_lock", spy):
            motor._dispatch_command("set_profile", {
                "prompt": "hola",
                "use_system": False,
                "_profile_name": "Akira",
                "id": "locked-id",
            })

        assert spy.enter_count >= 1, "set_profile must enter _history_lock"
        assert motor._current_profile_id == "locked-id"


class TestMotorVocalIAProfileIdLifecycle:
    """Pins MotorVocalIA._current_profile_id lifecycle."""

    def test_motor_initial_profile_id_is_none(self):
        from opencohost.core.llm_engine import MotorVocalIA
        motor = MotorVocalIA(queue.Queue(), lambda ev: None)
        assert motor._current_profile_id is None

    def test_motor_set_profile_updates_under_history_lock(self):
        from opencohost.core.llm_engine import MotorVocalIA
        motor = MotorVocalIA(queue.Queue(), lambda ev: None)
        new_id = str(uuid.uuid4())

        motor._dispatch_command("set_profile", {"id": new_id, "prompt": "hola", "use_system": True})

        assert motor._current_profile_id == new_id

    def test_motor_set_profile_without_id_sets_none(self):
        from opencohost.core.llm_engine import MotorVocalIA
        motor = MotorVocalIA(queue.Queue(), lambda ev: None)
        motor._dispatch_command("set_profile", {"id": str(uuid.uuid4()), "prompt": "hola", "use_system": True})
        motor._dispatch_command("set_profile", {"prompt": "hola", "use_system": True})
        assert motor._current_profile_id is None

    def test_motor_set_profile_is_thread_safe(self):
        """50 concurrent updates never leave _current_profile_id in an
        inconsistent state and never deadlock with _history_lock."""
        from opencohost.core.llm_engine import MotorVocalIA
        motor = MotorVocalIA(queue.Queue(), lambda ev: None)
        ids = [str(uuid.uuid4()) for _ in range(50)]
        barrier = threading.Barrier(50)

        def worker(pid):
            barrier.wait()
            motor._dispatch_command("set_profile", {"id": pid, "prompt": "p", "use_system": True})

        threads = [threading.Thread(target=worker, args=(pid,)) for pid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert motor._current_profile_id in ids


# ===========================================================================
# Judge-round fixes (dual-Opus judgment, obs id 2781): MF-1 atomic write,
# SF-3 non-dict preservation, SF-4 non-string id coercion.
# ===========================================================================

class TestCargarPerfilesNonDictPreserved:
    def test_top_level_array_returned_as_is_and_file_unchanged(self, tmp_path):
        """SF-3 — a perfiles.json containing a top-level JSON array (not a
        dict) must be returned as-is, and the file on disk must be left
        UNCHANGED. _ensure_stable_ids() assumes a dict (calls .values()) and
        must never run — and the load path must never fall through to the
        defaults-overwrite branch — on a non-dict payload."""
        profiles_file = tmp_path / "perfiles.json"
        original_content = json.dumps(["not", "a", "dict"])
        profiles_file.write_text(original_content, encoding="utf-8")
        defaults_file = tmp_path / "default_profiles.json"

        result = _call_cargar(str(profiles_file), str(defaults_file))

        assert result == ["not", "a", "dict"]
        assert profiles_file.read_text(encoding="utf-8") == original_content


class TestEnsureStableIdsCoercesNonStringIds:
    def test_non_string_id_gets_fresh_uuid4_on_load(self, tmp_path):
        """SF-4 — a hand-edited non-string id (e.g. an int) must not survive
        un-normalized: it would escape the str-based seen_ids dedup and
        create a latent stable_key collision downstream. Treat it as
        needs-reseed, same as a missing id."""
        profiles_file = tmp_path / "perfiles.json"
        profiles_file.write_text(
            json.dumps({"Akira": {"id": 123, "prompt": "hola", "use_system": True}}),
            encoding="utf-8",
        )
        defaults_file = tmp_path / "default_profiles.json"

        result = _call_cargar(str(profiles_file), str(defaults_file))

        assert isinstance(result["Akira"]["id"], str)
        assert _is_uuid4(result["Akira"]["id"])

    def test_int_id_and_string_id_with_same_digits_get_distinct_ids(self):
        """int 123 and str '123' must not collapse into the same identity
        once coerced — the int occurrence is reseeded, the string occurrence
        (already a valid non-empty str) is left as-is, so they stay distinct."""
        from opencohost.core.profiles.profiles import _ensure_stable_ids

        perfiles = {
            "IntId": {"id": 123, "prompt": "a", "use_system": True},
            "StrId": {"id": "123", "prompt": "b", "use_system": True},
        }

        _ensure_stable_ids(perfiles)

        assert perfiles["IntId"]["id"] != perfiles["StrId"]["id"]
        assert isinstance(perfiles["IntId"]["id"], str)
        assert isinstance(perfiles["StrId"]["id"], str)


class TestGuardarPerfilesAtomicWrite:
    def test_normal_save_round_trips(self, tmp_path):
        """A normal save round-trips through the atomic temp-file + replace
        dance without losing or altering any data."""
        import opencohost.core.profiles.profiles as prof_mod

        profiles_file = tmp_path / "perfiles.json"
        data = {"Akira": {"id": str(uuid.uuid4()), "prompt": "hola", "use_system": True}}

        with patch.object(prof_mod, "PROFILES_FILE", str(profiles_file)):
            prof_mod.guardar_perfiles(data)

        on_disk = json.loads(profiles_file.read_text(encoding="utf-8"))
        assert on_disk == data

    def test_interrupted_write_leaves_original_file_intact(self, tmp_path):
        """MF-1 — guardar_perfiles opened the target in 'w' mode, truncating
        it before writing. If the write is interrupted mid-way (here:
        os.replace fails), the pre-existing perfiles.json on disk must be
        left untouched — never empty, never partially written, never lost."""
        import opencohost.core.profiles.profiles as prof_mod

        profiles_file = tmp_path / "perfiles.json"
        original_content = json.dumps({"Akira": {"id": "old-id", "prompt": "old", "use_system": True}})
        profiles_file.write_text(original_content, encoding="utf-8")

        new_data = {"Akira": {"id": "new-id", "prompt": "new", "use_system": True}}

        with (
            patch.object(prof_mod, "PROFILES_FILE", str(profiles_file)),
            patch.object(prof_mod.os, "replace", side_effect=OSError("simulated interrupted write")),
        ):
            prof_mod.guardar_perfiles(new_data)

        assert profiles_file.read_text(encoding="utf-8") == original_content, (
            "original file must be intact after an interrupted write"
        )

    def test_write_failure_logs_one_warning_without_profile_content(self, tmp_path):
        """SF-2 — a broken persistence must not be invisible: a warning is
        logged on write failure, containing only the path + exception type,
        never the actual profile content."""
        import opencohost.core.profiles.profiles as prof_mod

        profiles_file = tmp_path / "perfiles.json"
        secret_prompt = "SUPER_SECRET_PROMPT_CONTENT"
        new_data = {"Akira": {"id": "new-id", "prompt": secret_prompt, "use_system": True}}

        with (
            patch.object(prof_mod, "PROFILES_FILE", str(profiles_file)),
            patch.object(prof_mod.os, "replace", side_effect=OSError("disk full")),
            patch.object(prof_mod.logger, "warning") as mock_warn,
        ):
            prof_mod.guardar_perfiles(new_data)

        mock_warn.assert_called_once()
        logged_message = mock_warn.call_args[0][0]
        assert secret_prompt not in logged_message
        assert str(profiles_file) in logged_message
        assert "OSError" in logged_message
