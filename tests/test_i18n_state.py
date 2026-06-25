"""Tests for i18n-core locale state + CLI (next-boot model).

T0c of the english_compatibility_i18n track. The CLI writes the desired locale;
it takes effect on the NEXT startup. No runtime switch (owner decision).
"""
from __future__ import annotations

from pathlib import Path

from opencohost.i18n import state
from opencohost.i18n.cli import main as cli_main


# --- state -----------------------------------------------------------------

def test_get_locale_defaults_to_es_when_no_file(tmp_path):
    assert state.get_locale(tmp_path / "locale.json") == "es"


def test_set_then_get_round_trip(tmp_path):
    f = tmp_path / "config" / "locale.json"
    state.set_locale("en", f)
    assert state.get_locale(f) == "en"


def test_set_locale_records_timestamp(tmp_path):
    import json

    f = tmp_path / "locale.json"
    state.set_locale("en", f)
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["locale"] == "en"
    assert data.get("set_at")


def test_corrupt_file_falls_back_to_default(tmp_path):
    f = tmp_path / "locale.json"
    f.write_text("{not valid json", encoding="utf-8")
    assert state.get_locale(f) == "es"


def test_atomic_write_leaves_no_tmp(tmp_path):
    f = tmp_path / "locale.json"
    state.set_locale("en", f)
    assert not (tmp_path / "locale.json.tmp").exists()


# --- CLI -------------------------------------------------------------------

def test_cli_set_locale_writes_and_returns_zero(tmp_path, capsys):
    f = tmp_path / "locale.json"
    rc = cli_main(["--set-locale", "en"], locale_file=f, codes=["es", "en"])
    assert rc == 0
    assert state.get_locale(f) == "en"
    assert "next start" in capsys.readouterr().out


def test_cli_unknown_locale_rejected_and_not_written(tmp_path, capsys):
    f = tmp_path / "locale.json"
    rc = cli_main(["--set-locale", "xx"], locale_file=f, codes=["es", "en"])
    assert rc == 2
    assert not f.exists()  # nothing persisted on rejection
    assert "unknown locale" in capsys.readouterr().out


def test_cli_show_locale(tmp_path, capsys):
    f = tmp_path / "locale.json"
    state.set_locale("en", f)
    rc = cli_main(["--show-locale"], locale_file=f, codes=["es", "en"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "en"


def test_cli_list_codes(tmp_path, capsys):
    rc = cli_main(["--list"], locale_file=tmp_path / "locale.json", codes=["es", "en"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "es" in out and "en" in out
