"""Slice 1 (profile-uuid) — Kira Memory Persistence, kira_memory_persistence_20260701.

Pins R12 (stable profile UUID migration and preservation) per spec v2.1 (#2772)
and design v2.1 §9/§12 (#2773): auto-seed on load, save-once, id-carry through
save/rename via a copy-and-update of the existing profile dict, fresh uuid4 on
new-profile creation, explicit-delete-only signal plumbing (purge wiring lands
in slice 7 — this slice only carries the (name, id) tuple through the save
callback), and launch-time dispatch of the id into
``MotorVocalIA._current_profile_id`` under ``_history_lock``.

RC-9 edge tests (owner decisions #2770, design §9): duplicate profile never
copies the source id, id-less imports get seeded, colliding ids on import are
deterministically de-duplicated (second occurrence re-seeded), and deleting a
profile never purges a different profile that later reuses the deleted
profile's old NAME (purge — landing in slice 7 — must key on id, never name).
"""

from __future__ import annotations

import json
import queue
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _call_cargar(profiles_path: str, defaults_path: str) -> dict:
    import opencohost.core.profiles as prof_mod
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
        defaults_file = tmp_path / "default_profiles.json"  # absent, irrelevant here

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
        profiles_file = tmp_path / "perfiles.json"  # absent — triggers seed path
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
# opencohost/ui/profiles_window.py — ConfiguradorPerfiles logic
# ===========================================================================

class _FakeEntry:
    def __init__(self, text=""):
        self._text = text

    def get(self):
        return self._text

    def delete(self, *_a):
        self._text = ""

    def insert(self, _idx, text):
        self._text += text


class _FakeTextbox:
    def __init__(self, text=""):
        self._text = text

    def get(self, _start, _end):
        return self._text

    def delete(self, _start, _end):
        self._text = ""

    def insert(self, _idx, text):
        self._text += text


class _FakeCheckbox:
    def __init__(self, value=False):
        self._value = bool(value)

    def get(self):
        return int(self._value)

    def select(self):
        self._value = True

    def deselect(self):
        self._value = False


def _make_configurador(perfiles, perfil_actual, entry_text, prompt_text, use_system):
    """Build a ConfiguradorPerfiles instance bypassing __init__ (no real Tk
    root needed) with only the attributes the save/new/delete logic touches.
    The list/detail rendering methods are stubbed no-ops — this slice tests
    the data-merge bug fix, not widget rendering."""
    from opencohost.ui.profiles_window import ConfiguradorPerfiles

    win = object.__new__(ConfiguradorPerfiles)
    win.perfiles = perfiles
    win.perfil_actual = perfil_actual
    win.on_save_callback = MagicMock()
    win.entry_nombre = _FakeEntry(entry_text)
    win.textbox_prompt = _FakeTextbox(prompt_text)
    win.check_system = _FakeCheckbox(use_system)
    win._actualizar_lista = lambda: None
    # Real _seleccionar_perfil's one load-bearing side effect for this slice's
    # logic is setting perfil_actual — keep that, skip the widget rendering.
    win._seleccionar_perfil = lambda nombre: setattr(win, "perfil_actual", nombre)
    return win


class TestGuardarPerfilActualPreservesId:
    def test_profile_edit_without_rename_preserves_id(self):
        """Direct v1-bug regression: editing prompt/use_system without
        renaming used to rebuild the value dict from scratch, dropping 'id'
        (and any unknown key). Must now preserve it."""
        pid = str(uuid.uuid4())
        perfiles = {"Akira": {"id": pid, "prompt": "old", "use_system": False}}
        win = _make_configurador(perfiles, "Akira", "Akira", "nuevo prompt", True)

        win._guardar_perfil_actual()

        assert perfiles["Akira"]["id"] == pid
        assert perfiles["Akira"]["prompt"] == "nuevo prompt"
        assert perfiles["Akira"]["use_system"] is True

    def test_edit_rename_preserves_profile_id(self):
        """1.3 — renaming a profile while editing must carry the id to the
        new name key."""
        pid = str(uuid.uuid4())
        perfiles = {"Akira": {"id": pid, "prompt": "old", "use_system": False}}
        win = _make_configurador(perfiles, "Akira", "Akira Renombrada", "old", False)

        win._guardar_perfil_actual()

        assert "Akira" not in perfiles
        assert perfiles["Akira Renombrada"]["id"] == pid

    def test_guardar_preserves_unknown_keys(self):
        """The fix must be general — ANY unknown key on the value dict
        survives a save, not just 'id'."""
        perfiles = {
            "Akira": {"prompt": "old", "use_system": False, "future_field": "keep-me"}
        }
        win = _make_configurador(perfiles, "Akira", "Akira", "new", True)

        win._guardar_perfil_actual()

        assert perfiles["Akira"]["future_field"] == "keep-me"

    def test_rename_to_existing_name_blocks_and_does_not_lose_id(self):
        pid = str(uuid.uuid4())
        perfiles = {
            "Akira": {"id": pid, "prompt": "old", "use_system": False},
            "Otro": {"id": str(uuid.uuid4()), "prompt": "otro", "use_system": False},
        }
        win = _make_configurador(perfiles, "Akira", "Otro", "old", False)

        with patch("opencohost.ui.profiles_window.mb") as mock_mb:
            win._guardar_perfil_actual()

        mock_mb.showerror.assert_called_once()
        # Nothing was mutated — Akira's id is intact and untouched.
        assert perfiles["Akira"]["id"] == pid


class TestNuevoPerfilSeedsId:
    def test_new_profile_seeds_fresh_uuid4(self):
        """1.4 — a brand new profile gets a fresh uuid4 id at creation time."""
        perfiles = {}
        win = _make_configurador(perfiles, None, "", "", False)

        win._nuevo_perfil()

        assert len(perfiles) == 1
        nombre = next(iter(perfiles))
        assert _is_uuid4(perfiles[nombre]["id"])

    def test_new_profile_id_distinct_across_creations(self):
        perfiles = {}
        win = _make_configurador(perfiles, None, "", "", False)

        win._nuevo_perfil()
        win.perfil_actual = next(iter(perfiles))
        win._nuevo_perfil()

        ids = [datos["id"] for datos in perfiles.values()]
        assert len(set(ids)) == len(ids), "each new profile must get a distinct id"


class TestEliminarPerfilSignal:
    def test_delete_profile_never_purges_profile_with_reused_old_name(self):
        """1.8 / RC-9 — deleting profile A records (name, id) for A. A LATER
        profile that reuses A's old NAME gets a DIFFERENT id (via
        _nuevo_perfil), so any future purge keyed on id (slice 7) can never
        touch the new profile B just because it shares A's old name.
        This slice only asserts the signal carries the correct id — no purge
        is wired yet."""
        deleted_id = str(uuid.uuid4())
        perfiles = {
            "Reciclado": {"id": deleted_id, "prompt": "vieja", "use_system": False},
            "Otro": {"id": str(uuid.uuid4()), "prompt": "otra", "use_system": False},
        }
        win = _make_configurador(perfiles, "Reciclado", "", "", False)

        with patch("opencohost.ui.profiles_window.mb") as mock_mb:
            mock_mb.askyesno.return_value = True
            win._eliminar_perfil()

        win.on_save_callback.assert_called_once()
        _call_args, call_kwargs = win.on_save_callback.call_args
        assert call_kwargs.get("deleted") == ("Reciclado", deleted_id)

        # A brand new profile is created (fresh uuid4) and renamed to reuse
        # the just-freed "Reciclado" name.
        win._nuevo_perfil()  # sets win.perfil_actual to "Nuevo Perfil 1"
        win.entry_nombre = _FakeEntry("Reciclado")
        win.textbox_prompt = _FakeTextbox("nueva")
        win.check_system = _FakeCheckbox(False)
        win._guardar_perfil_actual()

        assert win.perfiles["Reciclado"]["id"] != deleted_id

    def test_eliminar_perfil_declines_when_only_one_profile(self):
        perfiles = {"Unico": {"id": str(uuid.uuid4()), "prompt": "x", "use_system": False}}
        win = _make_configurador(perfiles, "Unico", "", "", False)

        with patch("opencohost.ui.profiles_window.mb") as mock_mb:
            win._eliminar_perfil()

        mock_mb.askyesno.assert_not_called()
        win.on_save_callback.assert_not_called()
        assert "Unico" in perfiles

    def test_eliminar_perfil_cancelled_by_user_does_not_delete(self):
        pid = str(uuid.uuid4())
        perfiles = {
            "A": {"id": pid, "prompt": "a", "use_system": False},
            "B": {"id": str(uuid.uuid4()), "prompt": "b", "use_system": False},
        }
        win = _make_configurador(perfiles, "A", "", "", False)

        with patch("opencohost.ui.profiles_window.mb") as mock_mb:
            mock_mb.askyesno.return_value = False
            win._eliminar_perfil()

        assert "A" in perfiles
        win.on_save_callback.assert_not_called()


# ===========================================================================
# opencohost/ui/profile_panel.py — delete-signal plumbing (backward compat)
# ===========================================================================

class TestProfilePanelDeleteSignalPlumbing:
    def _make_panel(self):
        from opencohost.ui.state import UIState
        from opencohost.ui.protocols import CallbackDispatcher
        from opencohost.ui.profile_panel import ProfilePanel

        ui_state = UIState()
        dispatcher = CallbackDispatcher(source="ProfilePanel")
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=MagicMock(),
        )
        return panel, ui_state

    def test_on_perfiles_guardados_accepts_deleted_kwarg_without_raising(self):
        """The real on_save_callback wired into ConfiguradorPerfiles is
        ProfilePanel._on_perfiles_guardados. It must accept the new
        deleted=(name, id) kwarg emitted by _eliminar_perfil without
        raising — otherwise deleting a profile in the real app would crash."""
        panel, ui_state = self._make_panel()
        try:
            with patch("opencohost.ui.profile_panel.guardar_perfiles"):
                panel._on_perfiles_guardados(
                    {"Otro": {"id": "x", "prompt": "p", "use_system": False}},
                    deleted=("Reciclado", "old-id-123"),
                )
        finally:
            panel.cleanup()
            ui_state.shutdown(timeout=2.0)


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
        holding _history_lock (slice 1 introduces only the field + the
        lock-guarded write; the full snapshot/swap/clear critical section
        lands in slice 4)."""
        motor = _make_motor()

        # NOTE: `with obj:` resolves __enter__/__exit__ on the TYPE, not the
        # instance — so the counter must live on the class-level methods,
        # never as an instance-attribute monkeypatch (that would silently
        # never fire and produce a false negative here).
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


# ===========================================================================
# Launch coverage (1.9) — cargar_perfiles -> ProfilePanel -> dispatch -> motor
# ===========================================================================

class TestLaunchCoverage:
    def test_app_shell_launch_seeds_ids_for_all_legacy_profiles(self, tmp_path):
        """Mirrors the real startup chain (app_shell.py:135 cargar_perfiles ->
        :785-786 ProfilePanel.set_profiles -> :228 after(500,
        _aplicar_perfil_actual) -> profile_panel.apply_current_profile() ->
        on_set_profile dispatch -> motor._dispatch_command('set_profile', ..))
        without needing a real Tk root: legacy (id-less) profiles on disk get
        seeded ids on load, and the launch-time profile application carries
        that id all the way into MotorVocalIA._current_profile_id."""
        profiles_file = tmp_path / "perfiles.json"
        profiles_file.write_text(
            json.dumps({
                "Akira": {"prompt": "hola", "use_system": True},
                "Show": {"prompt": "energia", "use_system": True},
            }),
            encoding="utf-8",
        )
        defaults_file = tmp_path / "default_profiles.json"

        perfiles = _call_cargar(str(profiles_file), str(defaults_file))
        assert all(_is_uuid4(datos["id"]) for datos in perfiles.values())

        from opencohost.ui.state import UIState
        from opencohost.ui.protocols import CallbackDispatcher
        from opencohost.ui.profile_panel import ProfilePanel

        mock_ctk = MagicMock()

        class _MockOptionMenu:
            def __init__(self, master=None, **kwargs):
                self.values = kwargs.get("values", [])
                self._value = self.values[0] if self.values else ""

            def set(self, value):
                self._value = value

            def get(self):
                return self._value

            def configure(self, **kwargs):
                if "values" in kwargs:
                    self.values = kwargs["values"]

            def pack(self, **kwargs):
                pass

        mock_ctk.CTkLabel = MagicMock(return_value=MagicMock(pack=lambda **k: None))
        mock_ctk.CTkButton = MagicMock(return_value=MagicMock(pack=lambda **k: None))
        mock_ctk.CTkOptionMenu = _MockOptionMenu
        mock_ctk.CTkFont = MagicMock()

        ui_state = UIState()
        dispatcher = CallbackDispatcher(source="ProfilePanel")
        motor = _make_motor()
        dispatcher.subscribe("on_set_profile", lambda p: motor._dispatch_command("set_profile", p))

        try:
            panel = ProfilePanel(
                parent_frame=MagicMock(),
                ui_state=ui_state,
                dispatcher=dispatcher,
                on_log=MagicMock(),
            )
            panel.set_profiles(perfiles)
            with patch("opencohost.ui.profile_panel.ctk", mock_ctk):
                panel.build()

            # Startup dispatch (app_shell.py:228 -> _aplicar_perfil_actual).
            panel.apply_current_profile()

            expected_id = perfiles[panel.get_default_profile_name()]["id"]
            assert motor._current_profile_id == expected_id
        finally:
            panel.cleanup()
            ui_state.shutdown(timeout=2.0)


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
        from opencohost.core.profiles import _ensure_stable_ids

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
        import opencohost.core.profiles as prof_mod

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
        import opencohost.core.profiles as prof_mod

        profiles_file = tmp_path / "perfiles.json"
        original_content = json.dumps({"Akira": {"id": "old-id", "prompt": "old", "use_system": True}})
        profiles_file.write_text(original_content, encoding="utf-8")

        new_data = {"Akira": {"id": "new-id", "prompt": "new", "use_system": True}}

        with (
            patch.object(prof_mod, "PROFILES_FILE", str(profiles_file)),
            patch.object(prof_mod.os, "replace", side_effect=OSError("simulated interrupted write")),
        ):
            prof_mod.guardar_perfiles(new_data)  # must not raise (fail-open)

        assert profiles_file.read_text(encoding="utf-8") == original_content, (
            "original file must be intact after an interrupted write"
        )

    def test_write_failure_logs_one_warning_without_profile_content(self, tmp_path):
        """SF-2 — a broken persistence must not be invisible: a warning is
        logged on write failure, containing only the path + exception type,
        never the actual profile content (never expose raw chat/profile
        content in logs, per CLAUDE.md safety rules)."""
        import opencohost.core.profiles as prof_mod

        profiles_file = tmp_path / "perfiles.json"
        secret_prompt = "SUPER_SECRET_PROMPT_CONTENT"
        new_data = {"Akira": {"id": "new-id", "prompt": secret_prompt, "use_system": True}}

        with (
            patch.object(prof_mod, "PROFILES_FILE", str(profiles_file)),
            patch.object(prof_mod.os, "replace", side_effect=OSError("disk full")),
            patch.object(prof_mod, "logger") as mock_logger,
        ):
            prof_mod.guardar_perfiles(new_data)

        mock_logger.warning.assert_called_once()
        logged_message = mock_logger.warning.call_args[0][0]
        assert secret_prompt not in logged_message
        assert str(profiles_file) in logged_message
        assert "OSError" in logged_message
