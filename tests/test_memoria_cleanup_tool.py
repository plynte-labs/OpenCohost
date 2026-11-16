"""T9 (memory-grounding-and-recall-budget-20261016): owner-run poisoned-row cleanup.

Runs only against a temp DB (never the live file). Dry run is the default and
read-only; apply backs up first and sets inactive=1 for the listed ids only;
revert sets inactive=0 again. Nothing is ever deleted.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from opencohost.core.memory.memoria_store import MemoriaStore

TOOL = Path(__file__).resolve().parents[1] / "scripts" / "memoria_cleanup.py"
POISONED = ["mem_af6598d79d6b4f0a8211cc9c82d54cd9", "mem_2e44c69428dd4eee8e0567e2b8081287"]


def _tool():
    spec = importlib.util.spec_from_file_location("memoria_cleanup", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db(tmp_path):
    path = tmp_path / "memorias.db"
    MemoriaStore(path)
    rows = [
        (POISONED[0], "p", "draft", "2026-10-17T02:38:32Z", "contexto: tesla → Kira: invented"),
        (POISONED[1], "p", "draft", "2026-10-17T02:39:14Z", "contexto: tesla otra vez → Kira: invented"),
        ("web1", "p", "draft", "2026-10-17T02:41:00Z", "contexto: busca algo → Kira: respuesta web"),
        ("web2", "p", "draft", "2026-10-17T02:58:59Z", "contexto: otra busqueda"),
        ("early", "p", "draft", "2026-10-17T02:10:00Z", "contexto: antes de la ventana"),
        ("kept", "p", "promoted", "2026-10-17T02:45:00Z", "hecho real promovido"),
    ]
    with sqlite3.connect(str(path)) as conn:
        conn.executemany(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, status, "
            "pinned, private, inactive, created_at, updated_at, signature) "
            "VALUES (?, ?, ?, 1, 't', ?, ?, 0, 0, 0, ?, ?, '')",
            [(i, p, f"{p}|{i}", c, s, ts, ts) for i, p, s, ts, c in rows],
        )
    return path


def _inactive(path):
    with sqlite3.connect(str(path)) as conn:
        return {r[0] for r in conn.execute("SELECT id FROM memorias WHERE inactive = 1")}


def test_dry_run_is_read_only_and_lists_ids(tmp_path, capsys):
    path = _db(tmp_path)
    before = path.read_bytes()

    code = _tool().main(["--db", str(path)])

    out = capsys.readouterr().out
    assert code == 0
    for mid in POISONED:
        assert mid in out
    assert path.read_bytes() == before
    assert _inactive(path) == set()


def test_review_list_covers_the_window_and_hides_content_by_default(tmp_path, capsys):
    path = _db(tmp_path)

    _tool().main(["--db", str(path)])

    out = capsys.readouterr().out
    assert "web1" in out and "web2" in out
    assert "early" not in out and "kept" not in out
    assert "respuesta web" not in out  # content is opt-in


def test_review_list_content_is_opt_in(tmp_path, capsys):
    path = _db(tmp_path)
    _tool().main(["--db", str(path), "--show-content"])
    assert "respuesta web" in capsys.readouterr().out


def test_apply_backs_up_then_deactivates_only_listed_ids(tmp_path):
    path = _db(tmp_path)
    backups = tmp_path / "bk"

    code = _tool().main(["--db", str(path), "--apply", "--backup-dir", str(backups)])

    assert code == 0
    assert _inactive(path) == set(POISONED)
    (backup,) = list(backups.glob("memorias-*.db"))
    with sqlite3.connect(str(backup)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memorias WHERE inactive = 1").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memorias").fetchone()[0] == 6
    with sqlite3.connect(str(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memorias").fetchone()[0] == 6  # nothing deleted


def test_explicit_ids_extend_the_apply_list(tmp_path):
    path = _db(tmp_path)
    _tool().main(["--db", str(path), "--apply", "--ids", "web1", "--backup-dir", str(tmp_path / "bk")])
    assert _inactive(path) == set(POISONED) | {"web1"}


def test_revert_restores_active_state(tmp_path):
    path = _db(tmp_path)
    tool = _tool()
    tool.main(["--db", str(path), "--apply", "--backup-dir", str(tmp_path / "bk")])
    code = tool.main(["--db", str(path), "--revert", "--backup-dir", str(tmp_path / "bk")])
    assert code == 0
    assert _inactive(path) == set()


def test_unknown_id_is_reported_and_nothing_changes(tmp_path, capsys):
    path = _db(tmp_path)
    code = _tool().main(
        ["--db", str(path), "--apply", "--ids", "nope", "--backup-dir", str(tmp_path / "bk")]
    )
    assert code != 0
    assert "nope" in capsys.readouterr().out
    assert _inactive(path) == set()


def test_missing_db_is_an_error_not_a_new_file(tmp_path):
    missing = tmp_path / "absent.db"
    assert _tool().main(["--db", str(missing)]) != 0
    assert not missing.exists()


# ---- judge round 1: apply saves prior state, revert restores exactly it -----

def _state(path, mid):
    with sqlite3.connect(str(path)) as conn:
        return conn.execute(
            "SELECT inactive, judged_at FROM memorias WHERE id = ?", (mid,)).fetchone()


def _set_state(path, mid, inactive, judged_at):
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE memorias SET inactive = ?, judged_at = ? WHERE id = ?",
                     (inactive, judged_at, mid))


def test_apply_clears_judged_at_like_a_mute(tmp_path):
    path = _db(tmp_path)
    _set_state(path, POISONED[0], 0, "2026-10-17T03:00:00Z")

    _tool().main(["--db", str(path), "--apply", "--backup-dir", str(tmp_path / "bk")])

    assert _state(path, POISONED[0]) == (1, "")


def test_revert_restores_exactly_the_saved_prior_values(tmp_path):
    path = _db(tmp_path)
    _set_state(path, POISONED[0], 0, "2026-10-17T03:00:00Z")   # judged, visible
    _set_state(path, POISONED[1], 1, "judge-hid")               # already hidden by the judge
    tool, bk = _tool(), str(tmp_path / "bk")

    tool.main(["--db", str(path), "--apply", "--backup-dir", bk])
    assert tool.main(["--db", str(path), "--revert", "--backup-dir", bk]) == 0

    assert _state(path, POISONED[0]) == (0, "2026-10-17T03:00:00Z")
    assert _state(path, POISONED[1]) == (1, "judge-hid")  # NOT forced to inactive=0


def test_second_apply_keeps_the_original_prior_state(tmp_path):
    path = _db(tmp_path)
    _set_state(path, POISONED[0], 0, "stamp")
    tool, bk = _tool(), str(tmp_path / "bk")

    tool.main(["--db", str(path), "--apply", "--backup-dir", bk])
    tool.main(["--db", str(path), "--apply", "--backup-dir", bk])
    tool.main(["--db", str(path), "--revert", "--backup-dir", bk])

    assert _state(path, POISONED[0]) == (0, "stamp")


def test_revert_without_saved_state_changes_nothing(tmp_path, capsys):
    path = _db(tmp_path)
    _set_state(path, POISONED[0], 1, "x")

    code = _tool().main(["--db", str(path), "--revert", "--backup-dir", str(tmp_path / "bk")])

    assert code != 0
    assert _state(path, POISONED[0]) == (1, "x")
    assert "no saved prior state" in capsys.readouterr().out


def test_dry_run_writes_no_manifest(tmp_path):
    path = _db(tmp_path)
    _tool().main(["--db", str(path), "--backup-dir", str(tmp_path / "bk")])
    assert not (tmp_path / "bk").exists()
