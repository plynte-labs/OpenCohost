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
import re
import threading
import time

from opencohost.core.llm_engine import MotorVocalIA


def _bare_motor() -> MotorVocalIA:
    """A constructed-but-unstarted motor with a no-op UI callback.

    Hermetic: _generar_dialogo reaches _fetch_show through TWO callers —
    _discover_model_ctx for the context limit and _resolve_reasoning_classification
    for the capabilities probe — so both caches are seeded. That probe is bounded
    now, but a bounded network call is still a network call, and these tests wait
    on 2s Event budgets.
    """
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    return motor


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

    # Tier split (tauri_stream_chat_20260812): the stream window is the
    # turn_priority module setting (default 120s); age past it.
    from opencohost.core import turn_priority

    old = time.time() - (turn_priority.effective_stream_ttl() + 60.0)
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


# ── WU3 AC3.3 (design-fase2.md §3): _commit_history staleness hook ──────────


def test_commit_history_invalidates_cached_pregen():
    # A reply generated against PRE-commit history must never speak: any history
    # commit clears a cached pregen and bumps the invalidation epoch (strict
    # staleness — the NEW ENGINE SEAM declared in WU3's diff).
    motor = _bare_motor()
    motor._capture_memoria = lambda *a, **kw: None
    motor._prefetched_agenda = {"payload": "P", "dialogo": "STALE", "priority": 1, "source": "chat"}
    epoch0 = motor._prefetch_epoch

    motor._commit_history("otro contexto", "otra respuesta", source="direct")

    assert motor._prefetched_agenda is None, "a history commit clears the now-stale cached pregen"
    assert motor._prefetch_epoch == epoch0 + 1


def test_commit_history_invalidates_inflight_pregen_store():
    # An in-flight pregen (worker still generating) whose history commits under
    # it must DISCARD its store on the epoch check — never speak stale.
    motor = _bare_motor()
    motor._capture_memoria = lambda *a, **kw: None
    gate = threading.Event()
    stored = {"late": False}

    def _worker():
        epoch_at = motor._prefetch_epoch
        gate.wait(2.0)
        with motor._prefetch_lock:
            if motor._prefetch_epoch != epoch_at:
                return  # invalidated by the commit -> discard
            motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 1, "source": "chat"}
            stored["late"] = True
        motor._prefetch_done.set()

    th = threading.Thread(target=_worker, daemon=True)
    with motor._prefetch_lock:
        motor._pregen_inflight = {"payload": "P", "source": "chat", "priority": 1}
        motor._prefetch_thread = th
    th.start()

    # Another turn commits history between the pregen start and its store.
    motor._commit_history("ctx", "resp", source="direct")

    gate.set()
    th.join(2.0)
    assert stored["late"] is False, "an in-flight pregen must discard its store after an intervening commit"
    assert motor._prefetched_agenda is None


def test_speak_pregenerated_own_commit_never_drops_its_own_turn():
    # Ordering audit (design-fase2.md §3 WU3 AC3.3): _take_pregen_if_match pops
    # the entry at pop BEFORE _speak_pregenerated -> _commit_history runs, so the
    # turn being spoken is NOT retro-invalidated by its own commit; the commit's
    # epoch bump only kills OTHER pregens. Uses the REAL _commit_history.
    motor = _bare_motor()
    motor._capture_memoria = lambda *a, **kw: None
    spoke = []
    motor._hablar = lambda texto, source="direct": spoke.append((texto, source))

    motor._prefetched_agenda = {"payload": "P", "dialogo": "Hola.", "priority": 1, "source": "chat"}
    motor.enqueue("P", priority=1, source="chat")  # not speaking -> no interactive trigger
    epoch0 = motor._prefetch_epoch

    motor._process_priority_queue()

    assert spoke == [("Hola.", "chat")], "the turn spoke despite its own commit bumping the epoch"
    assert motor._prefetch_epoch == epoch0 + 1, "the commit bumped the epoch (would kill any OTHER pregen)"
    assert motor._prefetched_agenda is None


# ── WU4 F1 (WU3 follow-up): source-aware clear ──────────────────────────────


def test_clear_prefetched_agenda_only_leaves_non_agenda_draft_intact():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 1, "source": "chat"}
    epoch0 = motor._prefetch_epoch

    motor.clear_prefetched_agenda_only()

    assert motor._prefetched_agenda is not None, "an interactive occupant must survive"
    assert motor._prefetch_epoch == epoch0, "no epoch bump when nothing was cleared"


def test_clear_prefetched_agenda_only_clears_agenda_draft():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 2, "source": "kira-agenda"}
    epoch0 = motor._prefetch_epoch

    motor.clear_prefetched_agenda_only()

    assert motor._prefetched_agenda is None
    assert motor._prefetch_epoch == epoch0 + 1


# ── T4 [v5]: slot-EMPTY drop paths must kill an in-flight agenda store ─────


def test_clear_prefetched_agenda_only_slot_empty_agenda_inflight_bumps_epoch():
    """T4 [v5]: the slot is EMPTY (worker still in flight, nothing stored
    yet) for an AGENDA request — the driver's deliberate drop must still
    invalidate the incoming store so an orphaned agenda draft never lands
    after a drop it was never meant to see.
    """
    motor = _bare_motor()
    motor._prefetched_agenda = None
    motor._pregen_inflight = {"payload": "AG", "source": "kira-agenda", "priority": 2}
    epoch0 = motor._prefetch_epoch

    motor.clear_prefetched_agenda_only()

    assert motor._prefetch_epoch == epoch0 + 1, "an in-flight agenda store must be invalidated by the drop"


def test_clear_prefetched_agenda_only_slot_empty_interactive_inflight_survives():
    """T4 [v5]: an in-flight INTERACTIVE worker has nothing to do with the
    driver's agenda-only drop — it must survive untouched.
    """
    motor = _bare_motor()
    motor._prefetched_agenda = None
    motor._pregen_inflight = {"payload": "P", "source": "chat", "priority": 1}
    epoch0 = motor._prefetch_epoch

    motor.clear_prefetched_agenda_only()

    assert motor._prefetch_epoch == epoch0, "an interactive in-flight worker must not be invalidated"
    assert motor._pregen_inflight is not None


def test_clear_prefetched_agenda_only_discards_late_agenda_store_after_drop():
    """T4 [v5] end-to-end: a genuinely in-flight agenda worker whose slot is
    dropped while it's still generating must never land its store once it
    finishes — the epoch bump at drop time is the backstop (the worker's own
    store-time epoch check discards the orphaned draft).
    """
    motor = _bare_motor()
    release = threading.Event()

    def _slow_gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        release.wait(2.0)
        return "LATE_DRAFT"

    motor._generar_dialogo = _slow_gen
    motor._preview_accept_agenda_output = lambda d: True

    assert motor.pregenerate("AG", priority=2, source="kira-agenda") is True
    assert motor._pregen_inflight is not None, "marker is set synchronously before the worker starts"

    # The driver deliberately drops (yield / topic-gone) while the worker is
    # still generating.
    motor.clear_prefetched_agenda_only()

    release.set()
    motor._prefetch_thread.join(2.0)

    assert motor._pregen_inflight is None
    assert motor._prefetched_agenda is None, "the orphaned late store must never land"


# ── T3 [v5]: pop-side wait bound is 1x the watchdog timeout ────────────────


def test_pregen_wait_bound_equals_watchdog_timeout():
    motor = _bare_motor()
    motor._inference_watchdog_timeout = 42.0

    assert motor._pregen_wait_bound() == 42.0


def test_wait_or_invalidate_pregen_falls_back_on_timeout_even_if_worker_still_alive():
    """T3 [v5]: on timeout the pop falls back to foreground regardless of
    whether the worker is still alive — the pathological >watchdog worker is
    the declared transient-overlap class (design-fase2.md §4 [v4]), not a
    case the pop keeps waiting for.
    """
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    motor._inference_watchdog_timeout = 0.05  # tiny bound for a fast test
    spoke = []
    motor._hablar = lambda texto, source="direct": spoke.append((texto, source))
    fg = []

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        fg.append(contexto)
        return "FOREGROUND"

    motor._generar_dialogo = _gen

    # A pathologically slow worker that outlives the wait bound.
    release = threading.Event()

    def _worker():
        release.wait(5.0)  # never set within this test -> still alive past the bound
        with motor._prefetch_lock:
            motor._pregen_inflight = None
        motor._prefetch_done.set()

    th = threading.Thread(target=_worker, daemon=True)
    with motor._prefetch_lock:
        motor._pregen_inflight = {"payload": "P", "source": "chat", "priority": 1}
        motor._prefetch_thread = th
    th.start()

    with motor._pq_lock:
        motor._priority_queue = [(1, time.time(), "P", "chat", None)]
    motor._process_priority_queue()

    assert fg == ["P"], "the pop must fall back to foreground on timeout even though the worker is still alive"
    assert spoke == [("FOREGROUND", "chat")]
    assert th.is_alive(), "the pathological worker is still running -- the pop must not wait for it"
    release.set()
    th.join(2.0)


# ── WU4 F3 (WU3 follow-up): non-committing foreground fallbacks bump epoch ──


def test_generar_dialogo_empty_response_invalidates_pending_pregen():
    # A foreground turn that fails with an empty LLM response never calls
    # _commit_history — without F3 the epoch-bump backstop never fires, so a
    # zombie pregen store could still land after this failed turn.
    motor = _bare_motor()
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = None
    motor._ollama_chat_with_watchdog = lambda **kw: {"message": {"content": "   "}}
    motor._prefetched_agenda = {"payload": "P", "dialogo": "STALE", "priority": 1, "source": "chat"}
    epoch0 = motor._prefetch_epoch

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    assert motor._prefetched_agenda is None, "a non-committing fallback must clear a stale pregen"
    assert motor._prefetch_epoch == epoch0 + 1


def test_generar_dialogo_empty_response_pregen_call_stays_untouched():
    # Triangulation: the SAME failure inside the pregen worker's OWN call
    # (commit_history=False) must NOT bump the epoch — it never committed
    # anything special, and bumping here would nuke an unrelated valid draft.
    motor = _bare_motor()
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = None
    motor._ollama_chat_with_watchdog = lambda **kw: {"message": {"content": "   "}}
    motor._prefetched_agenda = {"payload": "OTHER", "dialogo": "VALID", "priority": 1, "source": "chat"}
    epoch0 = motor._prefetch_epoch

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=False)

    assert dialogo == ""
    assert motor._prefetched_agenda is not None, "an unrelated valid draft must survive"
    assert motor._prefetch_epoch == epoch0


def test_generar_dialogo_guardrail_no_fallback_invalidates_pending_pregen():
    motor = _bare_motor()
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = None
    motor._ollama_chat_with_watchdog = lambda **kw: {
        "message": {"content": "Como modelo de lenguaje, no puedo responder eso."}
    }
    # No neutral fallback line configured for this source -> guardrail-no-fallback.
    motor._guardrail_fallback_line = lambda *a, **kw: ""
    motor._prefetched_agenda = {"payload": "P", "dialogo": "STALE", "priority": 1, "source": "chat"}
    epoch0 = motor._prefetch_epoch

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    assert motor._prefetched_agenda is None
    assert motor._prefetch_epoch == epoch0 + 1


def test_generar_dialogo_agenda_reject_invalidates_pending_pregen():
    motor = _bare_motor()
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = None
    motor._ollama_chat_with_watchdog = lambda **kw: {"message": {"content": "Buen contenido de agenda."}}
    motor.agenda_output_validator = lambda dialogo: False
    motor._prefetched_agenda = {"payload": "P", "dialogo": "STALE", "priority": 1, "source": "chat"}
    epoch0 = motor._prefetch_epoch

    dialogo = motor._generar_dialogo("ctx", source="kira-agenda", commit_history=True)

    assert dialogo == ""
    assert motor._prefetched_agenda is None
    assert motor._prefetch_epoch == epoch0 + 1


# ── WU4 F4 (WU3 follow-up): TTL-clear TOCTOU vs a fresh in-flight replacement ─


def test_clear_prefetch_if_matches_skips_when_fresh_replacement_inflight():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "P", "dialogo": "STALE", "priority": 2, "source": "kira-agenda"}
    motor._pregen_inflight = {"payload": "P", "source": "kira-agenda", "priority": 2}
    epoch0 = motor._prefetch_epoch

    motor._clear_prefetch_if_matches("P", "kira-agenda")

    assert motor._prefetched_agenda is not None, "a fresh in-flight replacement must not be clobbered"
    assert motor._prefetch_epoch == epoch0


def test_clear_prefetch_if_matches_clears_when_no_inflight_replacement():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "P", "dialogo": "STALE", "priority": 2, "source": "kira-agenda"}
    epoch0 = motor._prefetch_epoch

    motor._clear_prefetch_if_matches("P", "kira-agenda")

    assert motor._prefetched_agenda is None
    assert motor._prefetch_epoch == epoch0 + 1


# ── WU4 F5 (WU3 follow-up): thread.start() raise must not stick the marker ──


def test_pregenerate_thread_start_raise_clears_inflight_marker(monkeypatch):
    motor = _bare_motor()
    motor._generar_dialogo = lambda *a, **kw: "should never run"

    def _boom(self):
        raise RuntimeError("can't allocate thread")

    monkeypatch.setattr(threading.Thread, "start", _boom)

    ok = motor.pregenerate("P", priority=1, source="chat")

    assert ok is False
    assert motor._pregen_inflight is None, "a failed spawn must not stick the in-flight marker"


def test_pregenerate_admitted_again_after_a_prior_start_failure(monkeypatch):
    motor = _bare_motor()

    def _boom(self):
        raise RuntimeError("can't allocate thread")

    monkeypatch.setattr(threading.Thread, "start", _boom)
    assert motor.pregenerate("P", priority=1, source="chat") is False
    assert motor._pregen_inflight is None

    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(1))

    ok = motor.pregenerate("P2", priority=1, source="chat")

    assert ok is True, "a prior start failure must not stick the slot refused forever"
    assert started == [1]


# ── WU4 4a: boundary telemetry — "used" (pop-time cache hit) ────────────────


def test_boundary_telemetry_used_reports_gap_ms_since_last_speaking_end(caplog):
    motor = _bare_motor()
    motor._hablar = lambda texto, source="direct": None
    motor._commit_history = lambda contexto, dialogo, **kw: None
    motor._last_speaking_end_monotonic = time.monotonic() - 0.5
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 2, "source": "kira-agenda"}
    motor.enqueue("P", priority=2, source="kira-agenda")

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._process_priority_queue()

    lines = [r.getMessage() for r in caplog.records if "Pregen boundary:" in r.getMessage()]
    assert len(lines) == 1
    assert "draft=used" in lines[0]
    assert "source=kira-agenda" in lines[0]
    m = re.search(r"gap_ms=(-?\d+)", lines[0])
    assert m is not None
    assert int(m.group(1)) >= 400, "gap_ms should reflect the ~500ms elapsed"


def test_boundary_telemetry_used_gap_ms_minus_one_when_unknown(caplog):
    motor = _bare_motor()
    motor._hablar = lambda texto, source="direct": None
    motor._commit_history = lambda contexto, dialogo, **kw: None
    assert motor._last_speaking_end_monotonic is None
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 2, "source": "kira-agenda"}
    motor.enqueue("P", priority=2, source="kira-agenda")

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._process_priority_queue()

    lines = [r.getMessage() for r in caplog.records if "Pregen boundary:" in r.getMessage()]
    assert len(lines) == 1
    assert "gap_ms=-1" in lines[0]


# ── F8 optional (runtime_findings_batch_20260807): [TURN_LATENCY] for a
# pregen pop-time cache hit -- this path never ran _ejecutar_inferencia, so
# it never hit that method's own emit and the metric had a blind spot on
# every hit (6 of ~15 real-session answers). ───────────────────────────────


def test_pregen_hit_emits_turn_latency_marked_path_pregen(caplog):
    motor = _bare_motor()
    motor._hablar = lambda texto, source="direct": None
    motor._commit_history = lambda contexto, dialogo, **kw: None
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 0, "source": "direct"}
    submitted_at = time.monotonic() - 0.3
    motor.enqueue("P", priority=0, source="direct", submitted_at=submitted_at)

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._process_priority_queue()

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[TURN_LATENCY]")]
    assert len(lines) == 1, lines
    assert "source=direct" in lines[0]
    assert "path=pregen" in lines[0]
    m = re.search(r"request_to_tts_total_ms=(\d+)", lines[0])
    assert m is not None and int(m.group(1)) >= 250


def test_pregen_hit_emits_no_turn_latency_when_submitted_at_unknown(caplog):
    """An internally-generated item (no submit-time stamp -- e.g. agenda's
    own turn) must not fabricate a latency figure it never measured, mirrors
    the foreground path's own `submitted_at is None` handling."""
    motor = _bare_motor()
    motor._hablar = lambda texto, source="direct": None
    motor._commit_history = lambda contexto, dialogo, **kw: None
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 2, "source": "kira-agenda"}
    motor.enqueue("P", priority=2, source="kira-agenda")

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._process_priority_queue()

    assert not any(r.getMessage().startswith("[TURN_LATENCY]") for r in caplog.records)


# ── WU4 4a: boundary telemetry — "evicted" (slot eviction at spawn) ─────────


def test_boundary_telemetry_evicted_logs_the_evicted_occupants_source(caplog):
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "AG", "dialogo": "AGD", "priority": 2, "source": "kira-agenda"}
    motor._generar_dialogo = lambda *a, **kw: "CHAT"

    caplog.set_level(logging.INFO, logger="OpenCohost")
    ok = motor.pregenerate("chatP", priority=1, source="chat")

    assert ok is True
    lines = [r.getMessage() for r in caplog.records if "Pregen boundary:" in r.getMessage()]
    assert len(lines) == 1
    assert "draft=evicted" in lines[0]
    assert "source=kira-agenda" in lines[0], "the evicted occupant's source, not the new request's"


# ── T1(a) [v5]: gen_ms / speech_ms fields on the boundary line ─────────────


def test_boundary_telemetry_used_reports_gen_ms_and_speech_ms(caplog):
    motor = _bare_motor()
    motor._hablar = lambda texto, source="direct": None
    motor._commit_history = lambda contexto, dialogo, **kw: None
    motor._last_speaking_end_monotonic = time.monotonic() - 0.5
    motor._last_speech_duration_ms = 1234
    motor._prefetched_agenda = {
        "payload": "P", "dialogo": "D", "priority": 2, "source": "kira-agenda", "gen_ms": 777,
    }
    motor.enqueue("P", priority=2, source="kira-agenda")

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._process_priority_queue()

    lines = [r.getMessage() for r in caplog.records if "Pregen boundary:" in r.getMessage()]
    assert len(lines) == 1
    assert "gen_ms=777" in lines[0], "gen_ms must be the DRAFT's own recorded generation duration"
    assert "speech_ms=1234" in lines[0], "speech_ms must be the PREVIOUS turn's speech duration"


def test_boundary_telemetry_gen_ms_and_speech_ms_default_to_minus_one(caplog):
    motor = _bare_motor()
    motor._hablar = lambda texto, source="direct": None
    motor._commit_history = lambda contexto, dialogo, **kw: None
    assert motor._last_speech_duration_ms is None
    # No "gen_ms" key -- a draft stored before this field existed, or an
    # eviction/rejection with nothing measurable.
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 2, "source": "kira-agenda"}
    motor.enqueue("P", priority=2, source="kira-agenda")

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._process_priority_queue()

    lines = [r.getMessage() for r in caplog.records if "Pregen boundary:" in r.getMessage()]
    assert len(lines) == 1
    assert "gen_ms=-1" in lines[0]
    assert "speech_ms=-1" in lines[0]


def test_pregen_store_records_real_gen_ms_for_the_stored_draft():
    """T1(a) [v5]: gen_ms is tracked at STORE time — a real, measured
    duration, not a placeholder.
    """
    motor = _bare_motor()
    motor._preview_accept_agenda_output = lambda d: True

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        time.sleep(0.05)
        return "DRAFT"

    motor._generar_dialogo = _gen

    assert motor.pregenerate("AG", priority=2, source="kira-agenda") is True
    motor._prefetch_thread.join(2.0)

    assert motor._prefetched_agenda is not None
    gen_ms = motor._prefetched_agenda["gen_ms"]
    assert gen_ms >= 40, "gen_ms must reflect the real measured generation duration"


# ── T1(d) [v5]: "none" — the previously-invisible plain foreground fallback ─


def test_boundary_telemetry_none_on_interactive_plain_foreground_fallback(caplog):
    """T1(d) [v5]: the highest-dead-air boundary -- a full foreground
    generation for an interactive item with NOT EVEN an in-flight pregen to
    wait for -- used to be entirely invisible.
    """
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    motor._hablar = lambda texto, source="direct": None
    motor._generar_dialogo = lambda *a, **kw: "REPLY"
    motor.enqueue("hola", priority=1, source="chat")  # not speaking -> no interactive trigger

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._process_priority_queue()

    lines = [r.getMessage() for r in caplog.records if "Pregen boundary:" in r.getMessage()]
    assert len(lines) == 1
    assert "draft=none" in lines[0]
    assert "source=chat" in lines[0]
    assert "gen_ms=-1" in lines[0]


def test_boundary_telemetry_none_never_double_reports_for_agenda_plain_foreground(caplog):
    """Agenda's own 'none' boundary is owned by the driver (reported before
    the item is even enqueued) -- the worker must not double-report it.
    """
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    motor._hablar = lambda texto, source="direct": None
    motor._generar_dialogo = lambda *a, **kw: "REPLY"
    motor.enqueue("agenda prompt", priority=2, source="kira-agenda")

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._process_priority_queue()

    lines = [r.getMessage() for r in caplog.records if "Pregen boundary:" in r.getMessage()]
    assert lines == [], "agenda's 'none' boundary belongs to the driver, not the worker"


# ── WU4 4c seam: speech_remaining_estimate() ─────────────────────────────────


def test_speech_remaining_estimate_none_when_not_speaking():
    motor = _bare_motor()
    assert motor._speaking is False
    assert motor.speech_remaining_estimate() is None


def test_speech_remaining_estimate_none_before_first_fragment_played():
    motor = _bare_motor()
    motor._speaking = True
    motor._speech_progress = {"total": 5, "played": 0, "start": time.time()}
    assert motor.speech_remaining_estimate() is None


def test_speech_remaining_estimate_ignores_synthesis_warmup_via_first_play():
    """T2(b) [v5]: a slow-to-synthesize first fragment must not inflate the
    per-fragment mean. `start` (wall clock) simulates a long synthesis
    warm-up before playback ever began; `first_play` (monotonic, set when
    fragment 1's playback actually started) is the honest baseline.
    """
    motor = _bare_motor()
    motor._speaking = True
    motor._speech_progress = {
        "total": 5,
        "played": 1,
        # 10s "synthesis warm-up" before the first fragment ever started
        # playing -- the OLD (start-based) baseline would inflate on this.
        "start": time.time() - 10.0,
        # The REAL playback duration of fragment 1: ~0.2s.
        "first_play": time.monotonic() - 0.2,
    }

    estimate = motor.speech_remaining_estimate()

    assert estimate is not None
    # Honest mean ~0.2s/fragment * 4 remaining ~= 0.8s -- nowhere near the
    # ~40s the inflated start-based baseline would have produced.
    assert estimate < 2.0


def test_speech_remaining_estimate_extrapolates_from_played_fragments():
    motor = _bare_motor()
    motor._speaking = True
    # 1 fragment played in ~1.0s, 4 remain -> ~4.0s remaining.
    motor._speech_progress = {"total": 5, "played": 1, "start": time.time() - 1.0}

    estimate = motor.speech_remaining_estimate()

    assert estimate is not None
    assert 3.0 <= estimate <= 5.0


def test_hablar_impl_drives_speech_progress_from_the_real_consumer_loop(tmp_path, monkeypatch):
    """WU4 4c seam: speech_remaining_estimate() must reflect the REAL
    _hablar_impl consumer loop's own progress counters (chunks_played /
    len(oraciones) / start_tts), not a separate timer. Drives the real
    heavy-TTS path with a fake requests.post + no-op pygame mixer — same
    harness convention as test_speech_serialization_race.py's _fast_hablar_motor.
    """
    from unittest.mock import MagicMock

    from opencohost.core import llm_engine

    # T7 [v5]: a load-call counter -- the SECOND real chunk's load() call
    # happens right after fragment 1 has fully finished playing (chunks_played
    # already bumped to 1) and while _speaking is still True. That is the
    # seam that proves `played` is advanced by the REAL consumer loop (not a
    # separate timer) and that speech_remaining_estimate() is real-derived.
    seen = {"mid": None, "after_first_played": None}

    class _FakeMusic:
        def __init__(self):
            self.load_calls = 0

        def load(self, path):
            self.load_calls += 1
            if self.load_calls == 2:
                seen["after_first_played"] = {
                    "played": motor._speech_progress["played"] if motor._speech_progress else None,
                    "speaking": motor._speaking,
                    "estimate": motor.speech_remaining_estimate(),
                }

        def play(self):
            pass

        def get_busy(self):
            return False

        def unload(self):
            pass

    motor = _bare_motor()
    motor.motor_tts = "pesado"
    ref = tmp_path / "voice.wav"
    ref.write_bytes(b"ref")
    motor.voz_referencia = str(ref)
    motor.pygame = MagicMock()
    motor.pygame.mixer.music = _FakeMusic()

    def fake_post(url, json=None, timeout=None):
        return MagicMock(status_code=200, content=b"wav")

    monkeypatch.setattr(llm_engine.requests, "post", fake_post)

    # Sample mid-flight progress via the first-chunk log line as a hook: patch
    # _log to snapshot progress the moment playback of chunk 1 begins.
    original_log = motor._log

    def _spy_log(msg, level="info"):
        if "Primer fragmento listo" in msg:
            seen["mid"] = {
                "speaking": motor._speaking,
                "progress": dict(motor._speech_progress) if motor._speech_progress else None,
            }
        return original_log(msg, level=level)

    motor._log = _spy_log

    # Step 2 (speech-router design §11 B1): the gap_ms/speaking_end bookkeeping
    # asserted at the end of this test left `_hablar_impl` for the boundary
    # owner (`_hablar` on the legacy path, the router when armed), so the drive
    # is the wrapper. The progress counters below are still the REAL
    # `_hablar_impl` consumer loop's.
    motor._hablar("Frase uno. Frase dos. Frase tres.", source="direct")

    assert seen["mid"] is not None, "speech progress must be tracked mid-playback"
    assert seen["mid"]["speaking"] is True
    assert seen["mid"]["progress"] is not None
    assert seen["mid"]["progress"]["total"] == 3
    # T7 [v5]: `played` must be advanced by the REAL consumer loop (not a
    # separate timer), and speech_remaining_estimate() must return a
    # real-derived value while still speaking.
    assert seen["after_first_played"] is not None, "must observe state after the real consumer loop advances played"
    assert seen["after_first_played"]["played"] == 1, "played must be advanced by the REAL consumer loop"
    assert seen["after_first_played"]["speaking"] is True
    assert seen["after_first_played"]["estimate"] is not None
    assert seen["after_first_played"]["estimate"] >= 0
    # After the pipeline completes, progress + speaking reset and the
    # speaking-end monotonic clock (WU4 4a gap_ms seam) is recorded.
    assert motor._speaking is False
    assert motor._speech_progress is None
    assert motor._last_speaking_end_monotonic is not None
