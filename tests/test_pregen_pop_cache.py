"""WU2 (agenda_no_dead_air fase 2, design-fase2.md §2.2-§2.5): the pop-time
pregenerated-turn cache and the belt lock, tested at the engine level.

These pin the NEW engine seams WU2 introduces:
  - `_take_pregen_if_match(payload, source)` — the pop-boundary cache lookup.
  - `_speak_pregenerated(cached)` — the worker-thread speaker (no parallel thread).
  - `_clear_prefetch_unless_matches(payload, source)` — the match-aware clear that
    keeps the very draft being routed through the queue (the replace_pending trap).
  - `_hablar_lock` — the WU2b belt lock serializing any `_hablar` caller.

Everything runs single-threaded by calling `_process_priority_queue()` directly:
`_hablar`/`_generar_dialogo` are patched so no real TTS/Ollama runs, which lets
each test assert the dispatch decision (cache hit vs generation) deterministically.
"""

import inspect
import logging
import queue
import threading
import time

from opencohost.core.llm_engine import MotorVocalIA


def _bare_motor() -> MotorVocalIA:
    """A constructed-but-unstarted motor with a no-op UI callback."""
    return MotorVocalIA(queue.Queue(), lambda event: None)


# ── §2.2 pop-time cache hit ────────────────────────────────────────────────


def test_pop_time_cache_hit_speaks_without_generation():
    motor = _bare_motor()
    hablar_calls = []
    motor._hablar = lambda texto, source="direct": hablar_calls.append((texto, source))

    def _boom(*a, **kw):
        raise AssertionError("_generar_dialogo must NOT run on a pop-time cache hit")

    motor._generar_dialogo = _boom
    commits = []
    motor._commit_history = lambda contexto, dialogo, **kw: commits.append(
        (contexto, dialogo, kw.get("source"))
    )
    records = []
    motor.agenda_output_recorder = lambda d: records.append(d)
    # F4 (judgment-day WU2): pin the UI dialogue emit — a regression dropping
    # _emit_dialogue on the cache-hit path must fail this test.
    emitted = []
    motor.dialogue_callback = lambda text, source: emitted.append((text, source))

    motor._prefetched_agenda = {
        "payload": "PROMPT_N1",
        "dialogo": "Hola, esta es la respuesta pregenerada.",
        "priority": 2,
        "source": "kira-agenda",
    }
    motor.enqueue("PROMPT_N1", priority=2, source="kira-agenda")

    motor._process_priority_queue()

    assert hablar_calls == [("Hola, esta es la respuesta pregenerada.", "kira-agenda")]
    # commit_history exactly once, at playback, with the cached dialogo.
    assert len(commits) == 1
    assert commits[0][1] == "Hola, esta es la respuesta pregenerada."
    assert commits[0][2] == "kira-agenda"
    # agenda-output recorder fired for the agenda source.
    assert records == ["Hola, esta es la respuesta pregenerada."]
    # UI dialogue line emitted (regression guard: a dropped _emit_dialogue fails here).
    assert emitted == [("Hola, esta es la respuesta pregenerada.", "kira-agenda")]
    # cache consumed.
    assert motor._prefetched_agenda is None


def test_pop_time_miss_falls_back_to_generation():
    # Triangulation of the hit test: an empty cache means the popped item takes
    # the normal generation path (the existing behavior, untouched).
    motor = _bare_motor()
    hablar_calls = []
    motor._hablar = lambda texto, source="direct": hablar_calls.append((texto, source))
    gen_calls = []

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix=None):
        gen_calls.append((contexto, source))
        return "Respuesta generada en vivo."

    motor._generar_dialogo = _gen
    assert motor._prefetched_agenda is None  # no draft armed
    motor.enqueue("hola kira", priority=1, source="chat")

    motor._process_priority_queue()

    assert gen_calls == [("hola kira", "chat")]
    # _hablar receives the RAW source ("chat"); the "kira" relabel is _emit_dialogue-only.
    assert hablar_calls == [("Respuesta generada en vivo.", "chat")]


def test_pop_time_cache_present_but_wrong_item_still_generates():
    # A cache armed for a DIFFERENT (payload, source) must not be spoken for the
    # popped item — the item generates normally and the cache is left intact.
    motor = _bare_motor()
    hablar_calls = []
    motor._hablar = lambda texto, source="direct": hablar_calls.append((texto, source))

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix=None):
        return "Generado para el chat."

    motor._generar_dialogo = _gen
    motor._prefetched_agenda = {
        "payload": "AGENDA_PROMPT",
        "dialogo": "Respuesta de agenda.",
        "priority": 2,
        "source": "kira-agenda",
    }
    motor.enqueue("hola kira", priority=1, source="chat")

    motor._process_priority_queue()

    assert hablar_calls == [("Generado para el chat.", "chat")]
    assert motor._prefetched_agenda is not None, "a non-matching cache survives the pop"


# ── §2.2 _take_pregen_if_match primitive ───────────────────────────────────


def test_take_pregen_if_match_pops_on_match_keeps_on_mismatch():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 2, "source": "kira-agenda"}

    # wrong source -> None, cache intact
    assert motor._take_pregen_if_match("P", "ptt") is None
    assert motor._prefetched_agenda is not None
    # wrong payload -> None, cache intact
    assert motor._take_pregen_if_match("OTHER", "kira-agenda") is None
    assert motor._prefetched_agenda is not None
    # exact match -> returns the draft and pops it
    got = motor._take_pregen_if_match("P", "kira-agenda")
    assert got is not None and got["dialogo"] == "D"
    assert motor._prefetched_agenda is None
    # empty cache -> None
    assert motor._take_pregen_if_match("P", "kira-agenda") is None


# ── §2.2 replace_pending trap + §2.5 supersede ─────────────────────────────


def test_replace_pending_keeps_matching_draft_clears_on_mismatch():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "DRAFT", "dialogo": "D", "priority": 2, "source": "kira-agenda"}
    epoch0 = motor._prefetch_epoch

    # SAME (payload, source): this IS the draft being routed to the queue by the
    # consume path -> replace_pending must NOT nuke it (the trap).
    motor.replace_pending("DRAFT", priority=2, source="kira-agenda")
    assert motor._prefetched_agenda is not None, "the routed draft must survive its own enqueue"
    assert motor._prefetch_epoch == epoch0, "no epoch bump on a self-matching enqueue"

    # DIFFERENT payload: a genuine supersede (AC2.5) -> clear + epoch bump.
    motor.replace_pending("NUEVO", priority=2, source="kira-agenda")
    assert motor._prefetched_agenda is None, "a superseding new turn clears the stale draft"
    assert motor._prefetch_epoch == epoch0 + 1, "supersede bumps the invalidation epoch"


def test_clear_prefetch_unless_matches_primitive():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 2, "source": "kira-agenda"}

    motor._clear_prefetch_unless_matches("P", "kira-agenda")
    assert motor._prefetched_agenda is not None, "exact match is kept"

    motor._clear_prefetch_unless_matches("P", "kira-agenda-stop")
    assert motor._prefetched_agenda is None, "source mismatch clears"


# ── §2.4 consume-at-event: accumulation must not flush in the gap (AC2.4) ───


def test_accumulation_not_flushed_before_consume_enqueued_draft_pops():
    motor = _bare_motor()
    # Snapshot the accumulation-buffer size at each _hablar so we can prove the
    # draft speaks BEFORE the buffer is flushed.
    snapshots = []
    motor._hablar = lambda texto, source="direct": snapshots.append(
        (texto, len(motor._accumulation_buffer))
    )
    motor._generar_dialogo = (
        lambda contexto, source="direct", commit_history=True, history_text=None, log_prefix=None: "GEN:"
        + contexto
    )

    # An agenda draft was pregenerated and (consume-at-event) already enqueued
    # for the worker's next pop.
    motor._prefetched_agenda = {
        "payload": "PROMPT",
        "dialogo": "Respuesta agenda pregenerada.",
        "priority": 2,
        "source": "kira-agenda",
    }
    motor.enqueue("PROMPT", priority=2, source="kira-agenda")
    # Chat piled up in the accumulation buffer during the prior speech.
    motor.enqueue_accumulation("hola kira", source="chat")

    motor._process_priority_queue()

    # The draft pops FIRST (queue non-empty), so the accumulation buffer is NOT
    # flushed in the gap: at the moment the draft speaks, 1 item still pending.
    assert snapshots[0] == ("Respuesta agenda pregenerada.", 1), snapshots
    # The accumulation flushes only AFTER, on the next empty-queue cycle.
    assert len(snapshots) == 2
    assert snapshots[1][1] == 0, "accumulation drained only after the draft popped"
    assert snapshots[1][0].startswith("GEN:")


def test_consume_via_speaking_end_event_beats_accumulation_flush():
    """F4 / AC2.4: drive the consume through the REAL speaking_end event path
    (ui_callback -> consume-enqueue, mirroring engine_host._route_motor_event),
    proving the enqueue LANDS before the post-turn pop. A pre-loaded
    accumulation buffer must NOT flush in the boundary gap — the consume-enqueued
    draft pops first. This structurally re-verifies F1's iterative drain keeps
    the AC2.4 guarantee (the enqueue happens inside _hablar's tail, before the
    per-item finally, exactly as consume-at-event does on the worker thread).
    """
    motor = _bare_motor()
    motor._commit_history = lambda contexto, dialogo, **kw: None
    snapshots = []  # (spoken_text, accumulation_len_at_speak_time)
    consumed = {"armed": False}

    def _route(event):
        # engine_host._route_motor_event analog: on an agenda speaking_end,
        # consume the ready draft SYNCHRONOUSLY on THIS worker thread inside
        # _hablar's tail — enqueue it BEFORE the per-item finally.
        if event == "speaking_end" and not consumed["armed"]:
            consumed["armed"] = True
            motor._prefetched_agenda = {
                "payload": "DRAFT_PROMPT",
                "dialogo": "Respuesta agenda pregenerada.",
                "priority": 2,
                "source": "kira-agenda",
            }
            motor.enqueue("DRAFT_PROMPT", priority=2, source="kira-agenda")

    motor.ui_callback = _route

    def _hablar(texto, source="direct"):
        snapshots.append((texto, len(motor._accumulation_buffer)))
        # The real _hablar_impl tail emits speaking_end; here it drives consume.
        motor.ui_callback("speaking_end")

    motor._hablar = _hablar
    motor._generar_dialogo = (
        lambda contexto, source="direct", commit_history=True, history_text=None, log_prefix=None: "GEN:"
        + contexto
    )

    # Turn 1 is a plain agenda generation (no cache yet); its speaking_end drives
    # the consume-enqueue of the pregenerated NEXT turn.
    motor.enqueue("FIRST", priority=2, source="kira-agenda")
    # Chat piled up in the accumulation buffer during the prior speech.
    motor.enqueue_accumulation("hola kira", source="chat")

    motor._process_priority_queue()

    # Turn 1 spoke with the chat still buffered...
    assert snapshots[0] == ("GEN:FIRST", 1), snapshots
    # ...the consume-enqueued draft popped NEXT (queue non-empty), so the
    # accumulation did NOT flush in the gap — chat STILL buffered.
    assert snapshots[1] == ("Respuesta agenda pregenerada.", 1), snapshots
    # Accumulation flushes only AFTER, on the empty-queue cycle.
    assert len(snapshots) == 3
    assert snapshots[2][1] == 0, "accumulation drained only after the draft popped"
    assert snapshots[2][0].startswith("GEN:")


# ── §2.5 belt lock (WU2b) ──────────────────────────────────────────────────


def test_hablar_belt_lock_serializes_concurrent_callers(caplog):
    # Two threads inside _hablar (the CTK legacy speaker + worker scenario) must
    # be serialized by the belt lock, and the second caller logs contention.
    motor = _bare_motor()
    occ = {"cur": 0, "max": 0}
    occ_lock = threading.Lock()
    inside = threading.Event()
    release = threading.Event()

    def _impl(texto, source="direct"):
        with occ_lock:
            occ["cur"] += 1
            occ["max"] = max(occ["max"], occ["cur"])
        inside.set()
        release.wait(2.0)
        with occ_lock:
            occ["cur"] -= 1

    motor._hablar_impl = _impl

    caplog.set_level(logging.INFO, logger="OpenCohost")
    t1 = threading.Thread(target=motor._hablar, args=("uno.",), kwargs={"source": "kira-agenda"})
    t1.start()
    assert inside.wait(2.0), "first caller never entered _hablar"

    t2 = threading.Thread(target=motor._hablar, args=("dos.",), kwargs={"source": "kira-agenda"})
    t2.start()
    # Deterministic: t1 holds _hablar_lock until we release, so t2's non-blocking
    # acquire fails and logs contention. Wait for that log before releasing.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if any("hablar contention" in r.getMessage() for r in caplog.records):
            break
        threading.Event().wait(0.01)
    release.set()
    t1.join(2.0)
    t2.join(2.0)

    assert occ["max"] == 1, "belt lock must serialize _hablar (max occupancy 1)"
    assert any("hablar contention" in r.getMessage() for r in caplog.records), (
        "the serialized caller must log contention"
    )


# ── F1 (judgment-day WU2): iterative drain, flat stack ─────────────────────


def test_iterative_drain_keeps_flat_stack_over_consecutive_cache_hits():
    """Consecutive ready-draft boundaries must NOT grow the engine thread's
    stack. WU2's consume-at-event enqueues the next agenda turn INSIDE this
    turn's _hablar tail (before the per-item finally), so a recursive
    _complete_processing_cycle -> _process_priority_queue tail-call nests ~4
    frames per turn and eventually RecursionError the worker (permanent
    silence). The drain must be iterative: pop-boundary stack depth stays flat
    across N chained cache-hit turns, and every turn still speaks.
    """
    motor = _bare_motor()
    N = 50
    depths: list = []
    spoke: list = []

    motor._commit_history = lambda contexto, dialogo, **kw: None

    def _hook():
        # Depth captured at the per-item pop boundary (fires once per popped item).
        depths.append(len(inspect.stack(0)))

    motor._test_pop_boundary_hook = _hook

    def _hablar(texto, source="direct"):
        # Mirror engine_host's consume-at-event (on_agenda_speaking_end): the
        # speaking_end handler runs on THIS worker thread inside _hablar's tail
        # and enqueues the NEXT agenda turn + arms its pregen draft BEFORE the
        # per-item finally runs _complete_processing_cycle.
        spoke.append(texto)
        n = len(spoke)
        if n < N:
            motor._prefetched_agenda = {
                "payload": f"PROMPT_{n}",
                "dialogo": f"draft {n}",
                "priority": 2,
                "source": "kira-agenda",
            }
            motor.enqueue(f"PROMPT_{n}", priority=2, source="kira-agenda")

    motor._hablar = _hablar

    # Prime turn 0.
    motor._prefetched_agenda = {
        "payload": "PROMPT_0",
        "dialogo": "draft 0",
        "priority": 2,
        "source": "kira-agenda",
    }
    motor.enqueue("PROMPT_0", priority=2, source="kira-agenda")

    motor._process_priority_queue()

    # Every chained turn actually spoke — no lost items.
    assert spoke == [f"draft {i}" for i in range(N)], spoke
    assert len(depths) == N
    # Flat stack: the drain loop must not add frames per turn. A tiny tolerance
    # absorbs interpreter noise; recursion would add ~4 frames/turn (~180 total).
    growth = depths[-1] - depths[4]
    assert growth <= 2, f"stack grew {growth} frames over {N} turns: {depths}"


# ── F2 (judgment-day WU2): TTL sweep must not strand adopted agenda drafts ──


def test_ttl_sweep_exempts_kira_agenda_but_still_expires_chat():
    """A consume-enqueued kira-agenda draft stuck behind long interactive turns
    (>30s) must NOT be silently expired by the TTL sweep — that strands the
    adopted turn (never speaks) and the orphaned cache blocks every new
    prefetch. kira-agenda* is replace_pending-deduped (never stacks), so
    exempting it from TTL cannot grow the queue. A same-age chat item still
    expires (triangulation).
    """
    motor = _bare_motor()
    spoke: list = []
    motor._hablar = lambda texto, source="direct": spoke.append((texto, source))
    motor._commit_history = lambda contexto, dialogo, **kw: None

    old = time.time() - (motor._pq_ttl_seconds + 60.0)
    # Raw queue items (priority, ts, payload, source, history_text), both older
    # than the TTL. kira-agenda is priority 2 (>0), so pre-fix it also expires.
    with motor._pq_lock:
        motor._priority_queue = [
            (1, old, "chat viejo", "chat", None),
            (2, old, "AGENDA_PROMPT", "kira-agenda", None),
        ]
    motor._prefetched_agenda = {
        "payload": "AGENDA_PROMPT",
        "dialogo": "respuesta de agenda",
        "priority": 2,
        "source": "kira-agenda",
    }

    motor._process_priority_queue()

    # The stale chat item expired; the stale kira-agenda draft survived, popped,
    # and spoke from cache.
    assert spoke == [("respuesta de agenda", "kira-agenda")], spoke
    with motor._pq_lock:
        assert motor._priority_queue == [], "queue fully drained"


def test_hablar_single_caller_never_contends(caplog):
    # Non-reentrant lock, released in finally: two SEQUENTIAL calls each acquire
    # freely (no self-deadlock), and no contention is ever logged.
    motor = _bare_motor()
    motor._hablar_impl = lambda texto, source="direct": None

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._hablar("hola.", source="kira-agenda")
    motor._hablar("otra.", source="kira-agenda")

    assert not any("hablar contention" in r.getMessage() for r in caplog.records)
