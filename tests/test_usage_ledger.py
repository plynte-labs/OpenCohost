"""T1: the usage ledger store (usage-activity-ledger-20261109)."""
import os
import sqlite3
from datetime import date

import pytest

from opencohost.config import settings
from opencohost.core.usage import ledger


@pytest.fixture(autouse=True)
def _reset():
    ledger._reset_for_tests()
    yield
    ledger._reset_for_tests()


def _rows(sql):
    con = sqlite3.connect(settings.USAGE_LEDGER_DB)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_record_call_upserts_and_increments():
    for _ in range(2):
        ledger.record_call(model="m1", is_local=True, kind="turn", source="direct",
                           profile_id="kira", prompt_tokens=100, completion_tokens=10,
                           compute_ms=500, cache_est_reused=7)
    rows = _rows("select calls,prompt_tokens,completion_tokens,compute_ms,cache_est_reused,failures,provider from usage_daily")
    assert rows == [(2, 200, 20, 1000, 14, 0, "local")]


def test_failed_call_counts_failure():
    ledger.record_call(model="m", is_local=False, kind="turn", source="chat", failed=True)
    assert _rows("select calls,failures,provider from usage_daily") == [(1, 1, "cloud")]


def test_unknown_kind_and_source_are_normalised():
    ledger.record_call(model="m", is_local=True, kind="weird", source="kira-agenda-stop")
    ledger.record_call(model="m", is_local=True, kind="turn", source="kira-agenda")
    ledger.record_call(model="m", is_local=True, kind="turn", source="zzz")
    rows = _rows("select kind,source,calls from usage_daily order by kind,source")
    assert ("turn", "agenda", 2) in rows  # unknown kind folds into turn
    assert ("turn", "other", 1) in rows
    assert all(r[0] in ledger.KINDS for r in rows)


def test_guardrail_retry_source_is_other():
    ledger.record_call(model="m", is_local=True, kind="guardrail_retry", source="guardrail_retry")
    assert _rows("select kind,source from usage_daily") == [("guardrail_retry", "other")]


def test_negative_and_non_int_values_clamped():
    ledger.record_call(model="m", is_local=True, kind="turn", source="direct",
                       prompt_tokens=-5, completion_tokens="x", compute_ms=None)
    assert _rows("select prompt_tokens,completion_tokens,compute_ms from usage_daily") == [(0, 0, 0)]


def test_web_search_records_calls_only():
    ledger.record_web_search(profile_id="kira")
    ledger.record_web_search(profile_id="kira")
    assert _rows("select kind,calls,prompt_tokens,completion_tokens from usage_daily") == [("web_search", 2, 0, 0)]


def test_record_decision_stores_enums_only():
    ledger.record_decision("vision", "on", "laya", "ok")
    ledger.record_decision("vision", "on", "laya", "ok")
    assert _rows("select decision_id,mode,acted_by,status,count from decision_daily") == [
        ("vision", "on", "laya", "ok", 2)]


def test_first_recorded_set_once():
    ledger.record_call(model="m", is_local=True, kind="turn", source="direct")
    first = _rows("select value from meta where key='first_recorded'")
    ledger.record_call(model="m", is_local=True, kind="turn", source="direct")
    assert first == _rows("select value from meta where key='first_recorded'")
    assert first[0][0] == date.today().isoformat()


def test_disabled_writes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "USAGE_LEDGER_ENABLED", False)
    ledger.record_call(model="m", is_local=True, kind="turn", source="direct")
    ledger.record_decision("vision", "on", "laya", "ok")
    ledger.record_web_search()
    assert not os.path.exists(settings.USAGE_LEDGER_DB)


def test_acl_applied_once_on_creation(monkeypatch):
    calls = []
    import opencohost.stream_admin.oauth_store as oauth_store
    monkeypatch.setattr(oauth_store, "restrict_file_permissions", lambda p: calls.append(str(p)) or True)
    for _ in range(3):
        ledger.record_call(model="m", is_local=True, kind="turn", source="direct")
    assert len(calls) == 1 and calls[0] == settings.USAGE_LEDGER_DB


def test_write_errors_never_raise_and_warn_once(monkeypatch, caplog, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "USAGE_LEDGER_DB", str(blocker / "sub" / "y.db"))
    with caplog.at_level("WARNING", logger="OpenCohost"):
        for _ in range(3):
            ledger.record_call(model="secret-model", is_local=True, kind="turn", source="direct")
    warns = [r for r in caplog.records if "USAGE_LEDGER" in r.getMessage()]
    assert len(warns) == 1
    assert "secret-model" not in warns[0].getMessage()


def test_summary_shape_and_aggregation():
    ledger.record_call(model="a", is_local=True, kind="turn", source="direct", profile_id="kira",
                       prompt_tokens=10, completion_tokens=2, compute_ms=100, cache_est_reused=4)
    ledger.record_call(model="b", is_local=False, kind="vision", source="", profile_id="kira",
                       prompt_tokens=5, completion_tokens=1, compute_ms=50)
    ledger.record_web_search(profile_id="kira")
    ledger.record_decision("vision", "on", "laya", "ok")
    ledger.record_decision("vision", "on", "rule", "ok")
    ledger.record_decision("vision", "shadow", "rule", "low_confidence")
    today = date.today().isoformat()
    s = ledger.summary()
    assert set(s) == {"from", "to", "enabled", "first_recorded", "days", "by_model", "by_kind",
                      "by_source", "by_profile", "cache", "decisions"}
    assert s["enabled"] is True and s["first_recorded"] == today and s["to"] == today
    assert s["days"] == [{"date": today, "calls": 3, "prompt_tokens": 15,
                          "completion_tokens": 3, "compute_ms": 150}]
    models = {(m["model"], m["provider"]): m for m in s["by_model"]}
    assert set(models) == {("a", "local"), ("b", "cloud")}  # web_search excluded
    assert models[("a", "local")]["calls"] == 1
    kinds = {k["kind"]: k for k in s["by_kind"]}
    assert kinds["web_search"]["calls"] == 1 and kinds["web_search"]["prompt_tokens"] == 0
    assert [x["source"] for x in s["by_source"]] == ["direct"]
    assert s["by_profile"][0]["profile_id"] == "kira" and s["by_profile"][0]["calls"] == 3
    assert s["cache"]["estimated_reused_tokens"] == 4
    assert s["cache"]["real_available"] is False
    assert s["cache"]["real_prompt_tokens"] is None and s["cache"]["real_reused_tokens"] is None
    d = s["decisions"][0]
    assert d == {"decision_id": "vision", "asked": 3, "laya_acted": 1, "rule_acted": 2,
                 "by_status": {"ok": 2, "low_confidence": 1}}


def test_summary_date_range_filters():
    ledger.record_call(model="a", is_local=True, kind="turn", source="direct")
    con = sqlite3.connect(settings.USAGE_LEDGER_DB)
    con.execute("update usage_daily set date='2020-01-01'")
    con.commit()
    con.close()
    s = ledger.summary(date_from="2021-01-01", date_to="2021-02-01")
    assert s["days"] == [] and s["by_model"] == []
    assert ledger.summary(date_from="2020-01-01", date_to="2020-01-01")["days"][0]["calls"] == 1


def test_summary_empty_when_no_db():
    s = ledger.summary()
    assert s["days"] == [] and s["first_recorded"] is None and s["enabled"] is True
    assert not os.path.exists(settings.USAGE_LEDGER_DB)


def test_export_rows_and_purge():
    ledger.record_call(model="a", is_local=True, kind="turn", source="direct", prompt_tokens=3)
    ledger.record_decision("vision", "on", "laya", "ok")
    rows = ledger.export_rows()
    assert any(r["table"] == "usage_daily" and r["model"] == "a" for r in rows)
    assert ledger.purge() is True
    assert ledger.export_rows() == []
    assert ledger.summary()["first_recorded"] is None
