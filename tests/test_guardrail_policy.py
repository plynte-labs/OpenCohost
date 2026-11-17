"""Owner guardrail toggles, T1 (guardrail-toggles): registry, store, enforcement.

``guardrails.json`` is redirected to a per-test tmp path by the autouse
``_isolate_guardrail_policy`` fixture in tests/conftest.py.
"""

import json
import os
import threading

import pytest

from opencohost.config import guardrail_policy as gp
from opencohost.config.validation import NON_NEGOTIABLE_IDS, output_guard

R9_TEXT = "As an AI language model, I cannot help with that."
R10_TEXT = "Your audience seems to love it."
R11_TEXT = "This stream is so boring and dead, nobody is here."


def test_registry_covers_every_non_negotiable_id():
    # no_code_output is listed (locked) so the owner UI shows it, but it is not a
    # preset-level non-negotiable: the registry is a superset.
    assert set(gp.REGISTRY) == set(NON_NEGOTIABLE_IDS) | {
        "no_code_output", "no_memory_denial", "no_false_capability",
        "no_isolation_pressure",
    }
    for spec in gp.REGISTRY.values():
        assert spec["tier"] in {"locked", "toggleable"}
        assert spec["category"] in {"safety", "persona", "style"}


def test_only_r9_r10_r11_are_toggleable():
    assert gp.TOGGLEABLE_IDS == frozenset(
        {"no_ai_self_identification", "no_meta_commentary", "no_negative_engagement"}
    )


def test_default_policy_is_everything_enabled():
    assert gp.get_disabled() == frozenset()
    assert all(gp.is_rule_enabled(r) for r in NON_NEGOTIABLE_IDS)


def test_locked_ids_cannot_be_disabled():
    errors = gp.set_disabled(["no_hate_speech"])
    assert errors == [{"id": "no_hate_speech", "code": "locked"}]
    assert gp.is_rule_enabled("no_hate_speech")


def test_unknown_ids_rejected():
    assert gp.set_disabled(["bogus"]) == [{"id": "bogus", "code": "unknown"}]


def test_non_list_rejected():
    assert gp.set_disabled("no_meta_commentary")[0]["code"] == "invalid"


def test_disabled_set_is_immutable_frozenset():
    assert gp.set_disabled(["no_meta_commentary", "no_meta_commentary"]) == []
    assert isinstance(gp.get_disabled(), frozenset)
    assert gp.get_disabled() == frozenset({"no_meta_commentary"})


def test_baseline_rules_block():
    assert output_guard(R9_TEXT, "chat")[0] is False
    assert output_guard(R9_TEXT, "direct")[0] is False
    assert "[no_meta_commentary]" in output_guard(R10_TEXT, "chat")[1]
    assert "[no_negative_engagement]" in output_guard(R11_TEXT, "chat")[1]


@pytest.mark.parametrize(
    "rule,text",
    [
        ("no_ai_self_identification", R9_TEXT),
        ("no_meta_commentary", R10_TEXT),
        ("no_negative_engagement", R11_TEXT),
    ],
)
def test_disabling_rule_lets_matching_reply_through(rule, text):
    assert output_guard(text, "chat")[0] is False
    assert gp.set_disabled([rule]) == []
    assert output_guard(text, "chat")[0] is True


def test_disabling_r9_still_blocks_other_rules():
    gp.set_disabled(["no_ai_self_identification"])
    assert output_guard("go to http://evil.example.com now", "chat")[0] is False
    assert output_guard("call me at 555-123-4567", "chat")[0] is False
    assert output_guard("I promise you will win", "chat")[0] is False
    # other toggleable rules stay enforced
    assert output_guard(R10_TEXT, "chat")[0] is False


def test_streaming_per_sentence_guard_honours_policy():
    from opencohost.core.context.generation_evaluator import output_guard_with_tts_check

    assert output_guard_with_tts_check(R9_TEXT, source="chat")[0] is False
    gp.set_disabled(["no_ai_self_identification"])
    assert output_guard_with_tts_check(R9_TEXT, source="chat")[0] is True


def test_orchestrator_stream_guard_uses_policy_aware_guard():
    """handle_stream_sentence and apply_stream_guard_verdict both route through
    the module-level ``_output_guard_with_tts_check``, which wraps output_guard."""
    from opencohost.core.engine import generation_orchestrator as go

    assert go._output_guard_with_tts_check(R9_TEXT, source="chat")[0] is False
    gp.set_disabled(["no_ai_self_identification"])
    assert go._output_guard_with_tts_check(R9_TEXT, source="chat")[0] is True


def test_load_missing_file_fails_closed():
    assert gp.load_guardrails() == 0
    assert gp.get_disabled() == frozenset()


@pytest.mark.parametrize("payload", ["{not json", "[]", '"x"', "null", '{"disabled": "x"}'])
def test_load_corrupt_or_non_dict_fails_closed(payload):
    with open(gp.GUARDRAILS_FILE, "w", encoding="utf-8") as f:
        f.write(payload)
    gp.set_disabled(["no_meta_commentary"])  # prior in-memory state must be dropped
    assert gp.load_guardrails() == 0
    assert gp.get_disabled() == frozenset()


def test_load_ignores_unknown_and_locked_ids():
    with open(gp.GUARDRAILS_FILE, "w", encoding="utf-8") as f:
        json.dump({"disabled": ["no_hate_speech", "bogus", "no_meta_commentary", 5]}, f)
    assert gp.load_guardrails() == 1
    assert gp.get_disabled() == frozenset({"no_meta_commentary"})
    assert gp.is_rule_enabled("no_hate_speech")


def test_save_roundtrip_atomic_and_logs_ids_only(caplog):
    caplog.set_level("DEBUG")
    assert gp.save_guardrails(["no_meta_commentary"]) == []
    with open(gp.GUARDRAILS_FILE, encoding="utf-8") as f:
        assert json.load(f) == {"disabled": ["no_meta_commentary"]}
    assert "[GUARDRAILS] policy disabled=" in caplog.text
    gp.reset_guardrails()
    gp.load_guardrails()
    assert gp.get_disabled() == frozenset({"no_meta_commentary"})


def test_save_failure_returns_none_keeps_state_and_no_tmp_left(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(gp.os, "replace", boom)
    assert gp.save_guardrails(["no_meta_commentary"]) is None
    assert gp.get_disabled() == frozenset()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_save_validation_errors_do_not_touch_disk():
    assert gp.save_guardrails(["no_doxxing"]) == [{"id": "no_doxxing", "code": "locked"}]
    assert not os.path.exists(gp.GUARDRAILS_FILE)


def test_concurrent_swap_never_exposes_partial_state():
    gp.set_disabled([])
    stop = threading.Event()
    seen_bad = []
    full = frozenset(gp.TOGGLEABLE_IDS)

    def reader():
        while not stop.is_set():
            d = gp.get_disabled()
            if d not in (frozenset(), full):
                seen_bad.append(d)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(300):
            gp.set_disabled(sorted(full))
            gp.set_disabled([])
    finally:
        stop.set()
        t.join()
    assert not seen_bad


def test_no_reply_text_in_logs(caplog):
    caplog.set_level("DEBUG")
    gp.set_disabled(["no_ai_self_identification"])
    gp.save_guardrails(["no_meta_commentary"])
    output_guard("go to http://secret-leak.example.com", "chat")
    guardrail_lines = [r.getMessage() for r in caplog.records if "[GUARDRAILS]" in r.getMessage()]
    assert guardrail_lines
    assert "secret-leak" not in "".join(guardrail_lines)


def test_list_rules_shape_covers_all_ids():
    gp.set_disabled(["no_meta_commentary"])
    by_id = {r["id"]: r for r in gp.list_rules()}
    assert set(by_id) == set(NON_NEGOTIABLE_IDS) | {
        "no_code_output", "no_memory_denial", "no_false_capability",
        "no_isolation_pressure",
    }
    assert set(by_id["no_meta_commentary"]) == {"id", "tier", "category", "enabled"}
    assert by_id["no_meta_commentary"]["enabled"] is False
    assert by_id["no_hate_speech"] == {
        "id": "no_hate_speech",
        "tier": "locked",
        "category": "safety",
        "enabled": True,
    }


# ── no_code_output (spoken-output-guard-and-measurement-20261109 T1) ─────────
# Same governance as no_human_claim / no_machine_lecture: enforced by
# output_guard, never owner-toggleable, and deliberately NOT a registry row
# (the registry mirrors the creator-config non-negotiables and the owner UI).


def test_no_code_output_can_never_be_disabled():
    assert "no_code_output" not in gp.TOGGLEABLE_IDS
    errors = gp.set_disabled(["no_code_output"])
    assert errors and errors[0]["id"] == "no_code_output"
    assert gp.is_rule_enabled("no_code_output")
    # even with every toggleable rule off, the rule still blocks
    gp.set_disabled(sorted(gp.TOGGLEABLE_IDS))
    allowed, reason = output_guard("Hola [Tu nombre], bienvenido.", "direct")
    assert not allowed and "[no_code_output]" in reason


def test_no_code_output_is_described_for_the_block_log():
    from opencohost.config.validation import _NN_DESCRIPTIONS

    assert "no_code_output" in _NN_DESCRIPTIONS


def test_no_code_output_is_listed_as_locked_safety_rule():
    by_id = {r["id"]: r for r in gp.list_rules()}
    assert by_id["no_code_output"] == {
        "id": "no_code_output",
        "tier": "locked",
        "category": "safety",
        "enabled": True,
    }
    assert "no_code_output" not in gp.TOGGLEABLE_IDS
    assert "no_code_output" not in NON_NEGOTIABLE_IDS


def test_no_code_output_cannot_be_disabled_by_any_path(tmp_path, monkeypatch):
    assert gp.set_disabled(["no_code_output"]) == [{"id": "no_code_output", "code": "locked"}]
    assert gp.save_guardrails(["no_code_output"]) == [{"id": "no_code_output", "code": "locked"}]
    # A hand-edited policy file naming it is ignored on load.
    policy = tmp_path / "guardrails.json"
    policy.write_text('{"disabled": ["no_code_output", "no_meta_commentary"]}', encoding="utf-8")
    monkeypatch.setattr(gp, "GUARDRAILS_FILE", str(policy))
    gp.load_guardrails()
    assert gp.get_disabled() == frozenset({"no_meta_commentary"})
    assert gp.is_rule_enabled("no_code_output")


def test_no_code_output_enforcement_ignores_the_registry(monkeypatch):
    block = "Here you go:\n```python\nprint(1)\n```"
    monkeypatch.setattr(gp, "is_rule_enabled", lambda rule_id: False)
    monkeypatch.setattr(gp, "REGISTRY", {})
    assert "no_code_output" in output_guard(block, "direct")[1]
