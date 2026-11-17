"""T2 (owner-reflection-memory-20261015, ADR-065): owner-only structure, opaque provenance,
counters-only logs. All text is synthetic."""
from __future__ import annotations

import json
import logging
import re

import pytest

from opencohost.core.context.owner_reflection import (
    OwnerExchange,
    ReflectionRing,
    build_distillation_prompt,
    owner_exchange_from_turn,
)
from tests.owner_reflection_support import (
    all_rows,
    fact_json,
    feed,
    install_stream,
    make_motor,
    owner_reflection_lines,
    run_job,
    spy_capture,
)

VIEWER_SENTINEL = "ZQXVIEWERSENTINEL"
OWNER_SENTINEL = "OWNERSENTINEL"


def test_viewer_sentinel_absent_from_built_prompt():
    ring = ReflectionRing(session_token="abcdef01")
    for i in range(4):
        ring.offer("direct", False, f"{OWNER_SENTINEL}{i} my dog is called Rex", f"kira reply {i}")
        ring.offer("chat", False, f"{VIEWER_SENTINEL}{i} user text", f"{VIEWER_SENTINEL}{i} kira text")
    batch, _evicted = ring.pending_batch()
    prompt = build_distillation_prompt(batch)
    assert VIEWER_SENTINEL not in prompt
    for i in range(4):
        assert f"{OWNER_SENTINEL}{i}" in prompt


def test_kira_side_text_never_enters_the_prompt():
    ring = ReflectionRing(session_token="abcdef01")
    ring.offer("direct", False, "my dog is called Rex", "KIRASIDESENTINEL reply")
    batch, _ = ring.pending_batch()
    prompt = build_distillation_prompt(batch)
    assert "my dog is called Rex" in prompt
    assert "KIRASIDESENTINEL" not in prompt


@pytest.mark.parametrize(
    "source,expected",
    [("direct", True), ("ptt", True), ("chat", False), ("owner-bundle", False),
     ("kira-agenda-intro", False), ("agenda", False), ("mystery", False), ("", False)],
)
def test_only_direct_and_ptt_construct_exchanges(source, expected):
    ex = owner_exchange_from_turn(source, False, 1, "my dog is called Rex", "ok")
    assert (ex is not None) is expected
    if expected:
        assert isinstance(ex, OwnerExchange) and ex.seq == 1


@pytest.mark.parametrize("private", [True, None, "yes", 1])
def test_private_turn_never_constructs_exchange(private):
    assert owner_exchange_from_turn("direct", private, 1, "my dog is called Rex", "ok") is None


def test_agenda_sentinel_never_constructs_exchange():
    assert owner_exchange_from_turn("direct", False, 1, "[agenda segura: prompt interno omitido]", "ok") is None


def test_exchange_text_is_capped_and_ring_rejects_non_exchanges():
    ex = owner_exchange_from_turn("direct", False, 1, "x" * 1000, "y" * 1000)
    assert ex is not None and len(ex.user_text) <= 300 and len(ex.kira_text) <= 300
    with pytest.raises(TypeError):
        ReflectionRing().add("a raw string")  # the ring accepts OwnerExchange only


def test_commit_hook_feeds_ring_only_from_owner_gate(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    motor._commit_history(f"{VIEWER_SENTINEL} viewer asks about the garden tomatoes", "kira replies", source="chat")
    motor._commit_history("owner says the tomatoes love compost heaps", "kira agrees", source="direct")
    motor._memorias_private = True
    motor._commit_history("private owner statement about tomatoes", "kira agrees", source="direct")
    motor._memorias_private = False
    motor._commit_history("spoken owner statement about compost", "kira agrees", source="ptt")
    texts = [e.user_text for e in motor._owner_reflection_ring().snapshot()]
    assert texts == ["owner says the tomatoes love compost heaps", "spoken owner statement about compost"]


def test_provenance_is_opaque_owner_turn_ids(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    install_stream(
        monkeypatch, motor,
        fact_json(("What is the owner's dog called?", "The owner's dog is called Rex.", (2, 3))),
    )
    calls = spy_capture(monkeypatch, motor)
    run_job(motor)
    assert len(calls) == 1
    prov = calls[0][1].get("provenance")
    assert prov, "a distilled draft must carry provenance"
    ids = json.loads(prov)
    assert ids and all(re.fullmatch(r"[0-9a-f]{8}:\d+", i) for i in ids)
    stored = [r["provenance"] for r in all_rows(tmp_path / "memorias.db") if r["provenance"]]
    assert stored == [prov]


def test_logs_are_counters_only(monkeypatch, tmp_path, caplog):
    motor = make_motor(monkeypatch, tmp_path)
    ring = motor._owner_reflection_ring()
    for i in range(1, 9):
        ring.offer("direct", False, f"{OWNER_SENTINEL}{i} the owner has a dog called Rex", f"KIRALINE{i} ok")
    ring.offer("chat", False, f"{VIEWER_SENTINEL} viewer text", f"{VIEWER_SENTINEL} kira text")
    install_stream(
        monkeypatch, motor,
        fact_json(("QSENTINEL what is the dog called?", "ASENTINEL the dog is called Rex.", (1,))),
    )
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    run_job(motor)
    text = caplog.text
    for secret in (VIEWER_SENTINEL, OWNER_SENTINEL, "KIRALINE", "QSENTINEL", "ASENTINEL", "Rex"):
        assert secret not in text
    lines = owner_reflection_lines(caplog)
    assert len(lines) == 1
    assert re.fullmatch(
        r"\[OWNER_REFLECTION\] outcome=(ok|skipped|cancelled|failed) reason=[a-z_]+ "
        r"considered=\d+ eligible=\d+ distilled=\d+ repeated=\d+ schema_rejected=\d+ "
        r"ungrounded=\d+ unkeyed=\d+ written=\d+ elapsed_ms=\d+",
        lines[0],
    )
