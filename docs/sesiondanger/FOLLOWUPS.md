# Deferred Follow-ups — Session Danger Overnight 2026-06-25

Captured here (not acted on) so the autonomous run never blocks. Owner triages on wake.
Each entry: what, why deferred, where, suggested action.

## Carried in from the ui_thread_hardening track
- **B1 — Aggregator has no internal locking** (engram #2534). `IntentAggregator` (`_items` deque,
  `_seen_texts` dict) and `VibeThermometer` (`_buffer`) are read by worker threads while the chat
  ingestion daemon mutates them → `RuntimeError: ... changed size during iteration`. Pre-existing;
  FR2's feature activation exercises it more. Fix: add `threading.Lock` in those classes. Out of
  scope this session unless it blocks a task.

## New items found during this session

### Design decision — Bug C reverses the ui_declutter_20260614 compact default
The product workspace, `_side_config_panel`, and `_product_workspace_panel` are the SAME widget
(`app_shell.py:965-966`). Compact mode (`_compacto_active=True`, the startup default chosen by track
`ui_declutter_20260614`) hides it via `grid_remove()` at startup (`_toggle_modo_compacto`, ~:1962,
invoked at ~:972). The owner explicitly wants the product/config panel VISIBLE at launch so users
can discover settings. Implementing Bug C therefore MODIFIES the declutter track's deliberate
"compact-is-default" decision. Done per explicit owner request; documented in ADR-SD-002 for owner
confirmation. Target startup state: product workspace SHOWN, logs HIDDEN.

### Regression — FR3 worsened the music-preview multi-playback (Bug A)
FR3 (`eb8849c`, merged this session) moved `_music_play_mood` onto a per-click daemon thread via
`_dispatch_audio_play`, removing the main-thread serialization. Combined with the pre-existing
`force=True` + avoid-current random selection + 6s `old_channel.fadeout`, rapid preview clicks now
stack overlapping channels AND workers. Fixed as part of Bug A (single-flight). engram #2535.

### Residual Demeter reach (FR1 judge INFO) — out of FR1 scope
`opencohost/ui/motor_event_handlers.py:503` still does `motor_ia._last_switch_failure = None`
(UI reaching engine private state). FR1 only fixed the `_speaking`/`_lock` reach in
`_kira_agenda_emergency_stop`. Consider a future FR to encapsulate this too.

### Test hygiene (FR1 judge LOW) — `_make_shell_stub` motor=None branch
`tests/test_audio_teardown.py:104-107` builds a bare `MagicMock` motor that auto-vivifies any method
(e.g. `interrupt_speaking`), so a test through that branch could green-pass a broken/renamed engine
method. FR1's contract is guarded by REAL-engine tests so no active hole, but tighten with
`MagicMock(spec=MotorVocalIA)` to make renames raise AttributeError. (Not changed now: a shared
fixture; spec-ing it may break unrelated default-stub tests — needs its own verification pass.)

### Runtime finding (2026-06-25) — reasoning model hangs the interactive tier → SDD PROPOSAL
The heavy-model runtime validation PASSED (qwen3:1.7b hung 180s on "Hola" → watchdog → rollback to
llama3 → recovered; FR1 gate cleared). Root cause: qwen3:1.7b is a reasoning model in the `fast` tier;
the engine uncaps `num_predict` for reasoning models (`llm_engine.py:1110-1111`) and never disables
thinking → unbounded thinking on trivial prompts. Captured as a PROPOSAL (owner decision, not done):
`conductor/tracks/realtime_reasoning_model_handling_20260625/proposal.md` — decisions D1 (swap
fast/default to a non-reasoning model), D2 (disable thinking on interactive path), D3 (tier-specific
watchdog; 180s is too long for fast/light).

### B1 (restated) — aggregator locking still open (see top section).
