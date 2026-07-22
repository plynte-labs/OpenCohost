"""WU3 (agenda_no_dead_air fase 2, design-fase2.md §2.3 + §3 WU3): interactive
pregeneration — a queued typed/PTT reply generates in the BACKGROUND while the
current TTS plays, so at the turn boundary the worker pops a cache hit and speaks
near-instantly instead of after a silent foreground generation.

These pin the NEW engine seams WU3 introduces on top of WU2's pop-time cache:
  - `pregenerate(payload, priority, source)` — generalized fill (source gate
    removed; agenda keeps its preview guardrail, interactive skips it because its
    transform/veto steps already ran inside `_generar_dialogo`).
  - `_llm_generating` — the narrow "Ollama busy right now" flag (GPU-free rule),
    distinct from `_processing` (which brackets the whole turn incl. TTS).
  - interactive trigger inside `enqueue()` (+ head-tracking / slot eviction).
  - `_commit_history` staleness hook (AC3.3) lives in test_pregen_pop_cache.py.
  - pop-side wait-or-fallback (AC3.2).

Convention (mirrors test_pregen_pop_cache.py): a constructed-but-unstarted motor,
`_hablar`/`_generar_dialogo` patched so no real TTS/Ollama runs, and the worker
loop driven synchronously via `_process_priority_queue()`. Background pregen work
runs on the real daemon thread `pregenerate` spawns; `_wait_until` polls for it.
"""

import queue
import threading
import time

from opencohost.core.llm_engine import MotorVocalIA


def _bare_motor() -> MotorVocalIA:
    return MotorVocalIA(queue.Queue(), lambda event: None)


def _wait_until(pred, timeout: float = 2.0, interval: float = 0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _seed_queue(motor, items):
    with motor._pq_lock:
        motor._priority_queue = list(items)


# ── AC3.1 typed message pregenerates during speech, pops from cache ─────────


def test_ac31_typed_message_pregenerates_during_speech_then_pops_from_cache():
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    gen_calls = []
    gen_done = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        gen_calls.append((contexto, source, commit_history))
        gen_done.set()
        return "Respuesta interactiva."

    motor._generar_dialogo = _gen
    spoke = []
    motor._hablar = lambda texto, source="direct": spoke.append((texto, source))

    # TTS is playing and Ollama is idle -> the GPU-free predicate holds.
    motor._speaking = True

    motor.enqueue("hola kira", priority=1, source="chat")

    # The interactive trigger spawned a background pregen; it generates once.
    assert gen_done.wait(2.0), "the interactive trigger never started a pregen"
    assert _wait_until(lambda: motor._prefetched_agenda is not None), "pregen never stored"
    assert gen_calls[0][2] is False, "pregen must use commit_history=False"

    # Speech ends; the worker pops the queued item -> cache hit, NO 2nd gen.
    motor._speaking = False
    motor._process_priority_queue()

    assert spoke == [("Respuesta interactiva.", "chat")]
    assert len(gen_calls) == 1, "a foreground generation ran at the boundary (pregen missed)"
    assert motor._prefetched_agenda is None, "cache consumed at pop"


# ── F1 [v4]: history_text carried end-to-end through the pregen path ────────


def test_f1_pregen_commits_honest_history_text_not_payload():
    """A PTT/direct reply spoken from the pregen cache must commit the HONEST
    history_text (what the host actually said), never the raw prompt template.
    Regression guard for the memoria_quality drop: the head snapshot used to drop
    tuple index 4, the slot dict never stored it, and _speak_pregenerated
    committed without it — so the raw payload template leaked into historial +
    memoria capture. Carry it enqueue -> pregenerate slot -> _commit_history.
    """
    motor = _bare_motor()
    committed = []
    motor._commit_history = lambda contexto, dialogo, **kw: committed.append(
        (contexto, dialogo, kw.get("source"), kw.get("history_text"))
    )
    gen_done = threading.Event()

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        gen_done.set()
        return "Respuesta PTT."

    motor._generar_dialogo = _gen
    motor._hablar = lambda texto, source="direct": None
    motor._speaking = True

    motor.enqueue(
        "RAW_PROMPT_TEMPLATE",
        priority=0,
        source="ptt",
        history_text="lo que el host realmente dijo",
    )

    assert gen_done.wait(2.0), "the interactive trigger never started a pregen"
    assert _wait_until(lambda: motor._prefetched_agenda is not None), "pregen never stored"
    # The honest text survives into the slot dict.
    assert motor._prefetched_agenda.get("history_text") == "lo que el host realmente dijo"

    motor._speaking = False
    motor._process_priority_queue()

    assert len(committed) == 1
    _ctx, _dialogo, source, hist = committed[0]
    assert source == "ptt"
    assert hist == "lo que el host realmente dijo", (
        "the pregen path must commit the honest history_text, not the payload template"
    )
    assert hist != "RAW_PROMPT_TEMPLATE"


# ── AC3.2 / F3 [v4]: pop-side wait-or-fallback ─────────────────────────────


def test_f3_same_item_inflight_waits_and_takes_landed_draft():
    """F3 [v4]: a matching in-flight pregen -> the pop ALWAYS waits on
    _prefetch_done (bounded by the watchdog), never starts a foreground
    generation racing behind the doomed/healthy pregen on the single Ollama
    runner. Healthy case: the draft lands during the wait -> cache hit, zero
    foreground generations.
    (The old estimate fields, removed by F3, are set so this is RED pre-fix: the
    old bound skipped the wait and foregrounded — `_boom` — for a >2s estimate.)
    """
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    spoke = []
    motor._hablar = lambda texto, source="direct": spoke.append((texto, source))

    def _boom(*a, **kw):
        raise AssertionError("no foreground generation — the pop must wait for the in-flight pregen")

    motor._generar_dialogo = _boom

    # A genuinely slow worker in flight for THIS exact item; it stores a valid draft.
    def _worker():
        try:
            time.sleep(0.2)
            with motor._prefetch_lock:
                motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 1, "source": "chat"}
        finally:
            with motor._prefetch_lock:
                motor._pregen_inflight = None
            motor._prefetch_done.set()

    th = threading.Thread(target=_worker, daemon=True)
    with motor._prefetch_lock:
        motor._pregen_inflight = {"payload": "P", "source": "chat", "priority": 1}
        motor._prefetch_thread = th
        motor._pregen_started_at = time.time()      # old mechanism (removed by F3)
        motor._pregen_last_gen_duration = 30.0      # >2s estimate: old code would SKIP the wait
    th.start()

    _seed_queue(motor, [(1, time.time(), "P", "chat", None)])
    motor._process_priority_queue()
    th.join(2.0)

    assert spoke == [("D", "chat")], "the pop must wait and take the landed pregen draft"
    assert motor._prefetched_agenda is None


def test_f3_doomed_same_item_waits_until_worker_finishes_then_foregrounds_once():
    """F3 [v4]: a DOOMED matching pregen (epoch bumped mid-flight by an
    intervening commit) -> the pop WAITS until the worker frees the GPU, then
    foregrounds EXACTLY ONCE. Never two same-item generations, never a foreground
    queued behind a still-running Ollama call.
    RED pre-fix: the old >2s-estimate path foregrounded IMMEDIATELY (elapsed ~0)
    instead of waiting for the in-flight worker to finish.
    """
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    spoke = []
    motor._hablar = lambda texto, source="direct": spoke.append((texto, source))
    fg = []

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        fg.append(contexto)
        return "FOREGROUND"

    motor._generar_dialogo = _gen

    release = threading.Event()
    stored = {"late": False}
    epoch_at = motor._prefetch_epoch  # captured BEFORE the doom bump (deterministic)

    def _worker():
        try:
            release.wait(2.0)
            with motor._prefetch_lock:
                if motor._prefetch_epoch != epoch_at:
                    return  # doomed -> discard (never a second speak for one item)
                motor._prefetched_agenda = {"payload": "P", "dialogo": "LATE", "priority": 1, "source": "chat"}
                stored["late"] = True
        finally:
            with motor._prefetch_lock:
                motor._pregen_inflight = None
            motor._prefetch_done.set()

    th = threading.Thread(target=_worker, daemon=True)
    with motor._prefetch_lock:
        motor._pregen_inflight = {"payload": "P", "source": "chat", "priority": 1}
        motor._prefetch_thread = th
        motor._pregen_started_at = time.time()      # old mechanism (removed by F3)
        motor._pregen_last_gen_duration = 30.0      # >2s estimate: old code SKIPPED the wait
    th.start()

    # Doom the in-flight pregen (an intervening turn committed history mid-flight).
    with motor._prefetch_lock:
        motor._prefetch_epoch += 1

    # Free the worker slightly later, from another thread, so the pop must WAIT.
    threading.Thread(target=lambda: (time.sleep(0.2), release.set()), daemon=True).start()

    _seed_queue(motor, [(1, time.time(), "P", "chat", None)])
    t0 = time.time()
    motor._process_priority_queue()
    elapsed = time.time() - t0
    th.join(2.0)

    assert elapsed >= 0.15, "F3: the pop must WAIT for the in-flight pregen, not foreground immediately"
    assert fg == ["P"], "foreground must run exactly once after the worker frees the GPU"
    assert spoke == [("FOREGROUND", "chat")]
    assert stored["late"] is False, "the doomed pregen must discard its store (no double generation)"
    assert motor._prefetched_agenda is None


# ── AC3.4 GPU-free rule ────────────────────────────────────────────────────


def test_ac34_no_pregen_starts_while_llm_generating():
    motor = _bare_motor()
    gen_calls = []
    motor._generar_dialogo = (
        lambda *a, **kw: gen_calls.append(a) or "x"
    )
    motor._speaking = True
    motor._llm_generating = True  # Ollama is busy right now

    motor.enqueue("hola", priority=1, source="chat")

    assert motor._prefetch_thread is None, "GPU-free rule violated: a pregen was spawned mid-generation"
    assert motor._prefetched_agenda is None
    assert gen_calls == []


def test_llm_generating_flag_brackets_the_ollama_call():
    # The flag is set True around the ACTUAL Ollama call inside _generar_dialogo
    # (foreground AND pregen pass here) and cleared in finally. Drive with an
    # Event-gated fake Ollama call and observe the flag mid-flight.
    motor = _bare_motor()
    motor._discover_model_ctx = lambda model: None
    motor._model_ctx_limit = {motor.current_model: 8192}

    seen = {"during": None}
    entered = threading.Event()
    release = threading.Event()

    def _fake_ollama(**kwargs):
        seen["during"] = motor.llm_generating
        entered.set()
        release.wait(2.0)
        return {"message": {"content": "hola desde el modelo."}}

    motor._ollama_chat_with_watchdog = lambda **kw: _fake_ollama(**kw)

    assert motor.llm_generating is False
    t = threading.Thread(
        target=lambda: motor._generar_dialogo("hola", source="chat", commit_history=False),
        daemon=True,
    )
    t.start()
    assert entered.wait(2.0), "the fake Ollama call never ran"
    assert seen["during"] is True, "_llm_generating must be True during the Ollama call"
    release.set()
    t.join(2.0)
    assert motor.llm_generating is False, "_llm_generating must be cleared in finally"


# ── AC3.5 head-tracking + slot eviction ────────────────────────────────────


def test_ac35_ptt_behind_chat_pregen_evicts_and_retriggers_for_ptt():
    motor = _bare_motor()
    motor._speaking = True
    # A chat pregen already occupies the slot (priority 1).
    motor._prefetched_agenda = {"payload": "chatP", "dialogo": "chatD", "priority": 1, "source": "chat"}
    epoch0 = motor._prefetch_epoch

    gen = []

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        gen.append((contexto, source))
        return "PTT_REPLY"

    motor._generar_dialogo = _gen

    # PTT (priority 0) lands in front -> the head is now the PTT item.
    motor.enqueue("pttP", priority=0, source="ptt")

    assert _wait_until(
        lambda: motor._prefetched_agenda is not None and motor._prefetched_agenda.get("source") == "ptt"
    ), "the PTT head never displaced the stale chat pregen"
    assert motor._prefetched_agenda["payload"] == "pttP"
    assert motor._prefetch_epoch >= epoch0 + 1, "evicting the chat pregen must bump the epoch"
    assert gen and gen[0][1] == "ptt"


def test_ac35_agenda_request_never_evicts_interactive_occupant():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "chatP", "dialogo": "chatD", "priority": 1, "source": "chat"}
    epoch0 = motor._prefetch_epoch

    ok = motor.pregenerate("agendaP", priority=2, source="kira-agenda")

    assert ok is False, "agenda (2) must not evict an interactive (1) occupant"
    assert motor._prefetched_agenda["source"] == "chat", "the interactive occupant survives"
    assert motor._prefetch_epoch == epoch0, "a refused request must not bump the epoch"


# ── slot eviction: cached AND in-flight agenda occupants ───────────────────


def test_eviction_interactive_evicts_cached_agenda_occupant():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "AG", "dialogo": "AGD", "priority": 2, "source": "kira-agenda"}
    epoch0 = motor._prefetch_epoch
    gen = []

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        gen.append(source)
        return "CHAT"

    motor._generar_dialogo = _gen

    ok = motor.pregenerate("chatP", priority=1, source="chat")

    assert ok is True, "chat (1) must evict a cached agenda (2) occupant"
    assert motor._prefetch_epoch == epoch0 + 1
    assert _wait_until(
        lambda: motor._prefetched_agenda is not None and motor._prefetched_agenda.get("source") == "chat"
    )
    assert motor._prefetched_agenda["payload"] == "chatP"


def test_f2_pregenerate_refuses_while_an_inflight_worker_is_alive():
    """F2 [v4]: an in-flight worker's Ollama call is UNCANCELLABLE — evicting it
    would leave a zombie running while a replacement spawns (two concurrent
    generations), and the zombie's finally would poison the replacement's shared
    _llm_generating / _prefetch_done bookkeeping. So while a worker thread is
    genuinely alive mid-generation, any new pregenerate request is REFUSED (no
    second worker, no epoch bump); the old worker's store lands normally.
    (Replaces the old in-flight-eviction test that masked this with instant fakes.)
    """
    motor = _bare_motor()
    motor._preview_accept_agenda_output = lambda d: True
    release = threading.Event()
    ollama_entered = threading.Event()

    # A REAL slow worker: a genuine _generar_dialogo blocked mid-"generation".
    def _slow_gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        ollama_entered.set()
        release.wait(2.0)
        return "AGENDA_DRAFT"

    motor._generar_dialogo = _slow_gen

    assert motor.pregenerate("AG", priority=2, source="kira-agenda") is True
    assert ollama_entered.wait(2.0), "the agenda worker never started generating"
    epoch0 = motor._prefetch_epoch

    # A higher-priority chat request arrives while the worker is genuinely alive.
    ok = motor.pregenerate("chatP", priority=1, source="chat")

    assert ok is False, "F2: a new request must be REFUSED while an in-flight worker is alive"
    assert motor._prefetch_epoch == epoch0, "a refused request must not bump the epoch"

    # The original in-flight worker's store lands normally once it finishes.
    release.set()
    assert _wait_until(
        lambda: motor._prefetched_agenda is not None and motor._prefetched_agenda.get("source") == "kira-agenda"
    ), "the in-flight worker's own store must land normally after refusal"
    assert motor._prefetched_agenda["payload"] == "AG"
    assert _wait_until(lambda: motor._pregen_inflight is None), (
        "the worker clears its in-flight marker in finally (F4)"
    )


# ── F5 [v4]: legacy consumers are source-aware ─────────────────────────────


def test_f5_legacy_consumers_ignore_interactive_occupant():
    """F5 [v4]: wait_prefetched_agenda / play_prefetched_agenda are the CTK legacy
    agenda consume path — they must ignore an interactive (chat/ptt) occupant, or
    CTK could speak a stale chat reply as an agenda turn. wait reports False and
    play pops nothing, leaving the interactive draft for its own worker-path pop.
    """
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 1, "source": "chat"}
    motor._prefetch_done.set()

    assert motor.wait_prefetched_agenda(timeout=0.0) is False, "wait must report False for an interactive occupant"
    assert motor.play_prefetched_agenda() is False, "play must not pop an interactive occupant"
    assert motor._prefetched_agenda is not None, "the interactive draft survives for its own pop"
    assert motor._prefetched_agenda["source"] == "chat"


def test_equal_priority_request_is_refused_on_busy_slot():
    motor = _bare_motor()
    motor._prefetched_agenda = {"payload": "chatA", "dialogo": "D", "priority": 1, "source": "chat"}
    epoch0 = motor._prefetch_epoch

    ok = motor.pregenerate("chatB", priority=1, source="chat")

    assert ok is False, "an equal-priority request must not evict the occupant (today's behavior)"
    assert motor._prefetched_agenda["payload"] == "chatA"
    assert motor._prefetch_epoch == epoch0


# ── trigger exclusions ─────────────────────────────────────────────────────


def test_trigger_accumulated_source_never_triggers():
    motor = _bare_motor()
    motor._speaking = True
    # F1 [v4]: the head snapshot is now (priority, payload, source, history_text).
    motor._maybe_trigger_interactive_pregen((1, "acc", "accumulated", None))
    assert motor._prefetch_thread is None, "source='accumulated' must never trigger a pregen"
    assert motor._prefetched_agenda is None


def test_trigger_head_matching_cache_does_not_retrigger():
    motor = _bare_motor()
    motor._speaking = True
    motor._prefetched_agenda = {"payload": "P", "dialogo": "D", "priority": 1, "source": "chat"}
    calls = []
    original = motor.pregenerate
    motor.pregenerate = lambda *a, **kw: calls.append(a) or original(*a, **kw)

    motor.enqueue("P", priority=1, source="chat")  # head already matches the live cache

    assert calls == [], "a head matching the live cache must not re-trigger a pregen"
    assert motor._prefetched_agenda["dialogo"] == "D"


def test_trigger_does_not_fire_while_not_speaking():
    motor = _bare_motor()
    motor._speaking = False  # idle: the worker will take this turn itself
    motor._generar_dialogo = lambda *a, **kw: "x"
    motor.enqueue("hola", priority=1, source="chat")
    assert motor._prefetch_thread is None, "no pregen while idle (only during ongoing speech)"


# ── interactive parity: _speak_pregenerated mirrors _ejecutar_inferencia ────


def test_interactive_parity_emit_relabels_chat_source_to_kira():
    # Foreground _ejecutar_inferencia emits a non-agenda reply with source
    # "kira" (not the raw "chat"). A pregenerated chat reply spoken at pop must
    # match that relabel — else the UI dialogue line is mislabeled.
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    emitted = []
    motor.dialogue_callback = lambda text, source: emitted.append((text, source))
    spoke = []
    motor._hablar = lambda texto, source="direct": spoke.append((texto, source))

    motor._prefetched_agenda = {"payload": "P", "dialogo": "Respuesta.", "priority": 1, "source": "chat"}
    _seed_queue(motor, [(1, time.time(), "P", "chat", None)])

    motor._process_priority_queue()

    assert spoke == [("Respuesta.", "chat")], "_hablar receives the RAW source (foreground parity)"
    assert emitted == [("Respuesta.", "kira")], "emit must relabel non-agenda to 'kira' (foreground parity)"


def test_interactive_parity_agenda_emit_keeps_agenda_source():
    # Triangulation: agenda sources are NOT relabeled — they emit as-is.
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    motor.agenda_output_recorder = lambda d: None
    emitted = []
    motor.dialogue_callback = lambda text, source: emitted.append((text, source))
    motor._hablar = lambda texto, source="direct": None

    motor._prefetched_agenda = {"payload": "AG", "dialogo": "Agenda.", "priority": 2, "source": "kira-agenda"}
    _seed_queue(motor, [(2, time.time(), "AG", "kira-agenda", None)])

    motor._process_priority_queue()

    assert emitted == [("Agenda.", "kira-agenda")], "agenda source is not relabeled"


def test_interactive_parity_chat_spoken_clock_advances():
    # Foreground fires on_chat_turn_spoken after speaking a chat turn; a chat
    # turn spoken from cache must advance the same spoken clock.
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    motor._hablar = lambda texto, source="direct": None
    ticks = []
    motor.on_chat_turn_spoken = lambda: ticks.append(1)

    motor._prefetched_agenda = {"payload": "P", "dialogo": "R", "priority": 1, "source": "chat"}
    _seed_queue(motor, [(1, time.time(), "P", "chat", None)])

    motor._process_priority_queue()

    assert ticks == [1], "a chat turn spoken from cache must advance the spoken clock (parity)"


# ── prefetch_agenda stays a thin, behavior-preserving alias ─────────────────


def test_prefetch_agenda_alias_rejects_non_agenda_source():
    motor = _bare_motor()
    assert motor.prefetch_agenda("hola", source="chat") is False
    assert motor.prefetch_agenda("", source="kira-agenda") is False
    assert motor._prefetch_thread is None


def test_prefetch_agenda_alias_still_pregenerates_agenda():
    motor = _bare_motor()
    motor._preview_accept_agenda_output = lambda d: True

    def _gen(contexto, source="direct", commit_history=True, history_text=None, log_prefix="LLM"):
        return "Texto de agenda."

    motor._generar_dialogo = _gen

    assert motor.prefetch_agenda("AGENDA_PROMPT", priority=2, source="kira-agenda") is True
    assert _wait_until(lambda: motor._prefetched_agenda is not None)
    assert motor._prefetched_agenda["source"] == "kira-agenda"
    assert motor._prefetched_agenda["dialogo"] == "Texto de agenda."
