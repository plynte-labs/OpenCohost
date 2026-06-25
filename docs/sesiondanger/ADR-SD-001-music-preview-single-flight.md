# ADR-SD-001 — Music Preview Single-Flight Guard

**Status**: Accepted
**Date**: 2026-06-25
**Track**: session/danger-overnight-20260625 — Bug A

---

## Context

### The bug

OpenCohost's music bed panel exposes one "Prueba rápida" button per mood
(`opencohost/ui/music_panel.py:67-71`).  Each click calls
`MusicPanel._on_play_mood(mood)` → `app_shell._music_play_mood(mood)`.

Before Fix FR3 the call ran synchronously on the Tk main thread, which at
least serialized clicks.  FR3 (commit `eb8849c`) moved every
`audio_bed.request_mood(...)` call onto a new daemon thread via
`_dispatch_audio_play` to keep disk I/O off the UI thread.  That was the
right move for latency, but it introduced Bug A: **each click spawns an
independent thread**, so N rapid clicks produce N concurrent workers.

Two compounding factors make this worse:

1. `request_mood(force=True)` bypasses the inertia gate AND the same-track
   no-op check — candidate selection actively avoids the current track and
   picks randomly, so every click selects a **different** track.
2. `_play_selected` (Phase 3) publishes to a new channel and then calls
   `old_channel.fadeout(fade_ms=6000)`.  With a 6-second crossfade, the old
   channel keeps playing while the new one starts.  Three rapid clicks
   produce three simultaneously audible channels for up to six seconds each.

The `_play_seq` generation counter guards against a *single* stale in-flight
decode being superseded — it does **not** prevent each of N fast-completing
workers from fully publishing their own channel.

### Failure evidence (RED tests)

- `TestRapidPreviewClicksSpawnOneWorker`: 8 clicks → depth trace
  `[1,2,3,4,5,6,7,8]` (8 concurrent workers).
- `TestRapidPreviewClicksOneActiveChannel`: old channel received
  `fadeout(6000)`, not `stop()`, while the new channel started playing.

---

## Decision

Add a **single-flight coalescing guard** inside `_music_play_mood` only.
The guard lives entirely in `app_shell.py` — no engine changes required.

### Mechanism (in `_music_play_mood`)

Three shared fields guard the state (lazy-init on first call so
`object.__new__` test stubs work without running `__init__`):

| Field | Type | Purpose |
|---|---|---|
| `_preview_lock` | `threading.Lock` | Protects the two fields below |
| `_preview_in_flight` | `bool` | True while a worker is executing |
| `_preview_latest_mood` | `str` | The most-recently-requested mood |

**On click (Tk main thread):**
1. Acquire `_preview_lock`.
2. Write the new mood to `_preview_latest_mood`.
3. If `_preview_in_flight` is True → release lock, call
   `_music_update_panel()`, return.  No new thread is spawned.
4. Otherwise set `_preview_in_flight = True`, release lock, spawn ONE worker.

**Worker loop:**
1. Read `_preview_latest_mood` under the lock.
2. Call `audio_bed.stop(emergency=True)` — hard-stops the current channel
   immediately (no 6-second overlap).
3. Call `audio_bed.request_mood(current_mood, force=True, boundary=True)`.
4. Re-acquire lock.  If `_preview_latest_mood` changed while we were playing,
   loop once more to serve the new mood (coalesce).  Otherwise clear
   `_preview_in_flight` and return.

**Net invariants:**
- At most ONE thread is in flight at any time (single-flight).
- The old channel is hard-stopped before the new track plays (no overlap).
- The last-clicked mood is always the mood that ends up playing (coalesce).

---

## Why this approach over alternatives

### Alternative A — debounce (e.g. 300 ms delay)

A timer-based debounce would absorb rapid clicks but would introduce
noticeable latency for a single deliberate click.  It also does not fix the
channel-overlap problem once a click does fire: the old 6-second fadeout
would still play.  Rejected.

### Alternative B — engine-level hard-stop for force=True

Adding `channel.stop()` instead of `channel.fadeout(fade_ms)` inside
`_play_selected` when `force=True` would fix the channel overlap globally.
However, `force=True` is also used by the agenda auto-start path
(`_kira_agenda_enable`) and potentially future callers that may legitimately
want a crossfade.  Changing the engine contract for all `force=True` callers
is a wider change with broader regression risk.  Rejected in favour of
confining the hard-stop to the preview path at the call site.

### Alternative C — engine `preview=True` parameter

A new keyword `preview=True` in `request_mood` / `_play_selected` that
switches from crossfade to hard-stop could be added cleanly.  However the
concurrency serialization (single-flight) would still need to live in the
shell.  Adding a new engine parameter adds engine complexity for a problem
that is fundamentally about caller behaviour.  Deferred — if future callers
need hard-replace semantics, consider adding `preview=True` at that point.

### Chosen — shell-level single-flight + hard-stop at call site

- Scope is minimal: the guard is 20 lines added to one method in `app_shell.py`.
- The engine contract is unchanged; `on_boundary` / agenda paths keep their
  6-second crossfade policy.
- The lock is on the Tk main thread for the "in-flight" check and on the
  worker thread only briefly for the mood-read and done-check — no contention
  risk.
- Fully reversible: removing the guard reverts to the original 9-line method.

---

## Consequences

**Positive:**
- Rapid preview clicks produce exactly ONE audible track at a time.
- No unbounded thread spawning from the preview path.
- Last-click always wins regardless of how many intermediate clicks arrive.
- The automatic on_boundary crossfade (6-second policy fade) is unaffected.
- `app_shell.py` stays under the 2700-line cap (2699 after fix).

**Negative / constraints:**
- Preview requests coalesce: very fast click sequences may skip intermediate
  moods.  This is intentional (last-click wins) and expected UX for a preview
  button.
- If `audio_bed.stop(emergency=True)` fails (e.g. pygame not initialized),
  the worker logs a warning and clears `_preview_in_flight`; the next click
  will retry.  No silent lock-up.
- **Preview hard-replaces ANY active bed instantly (owner flag):** The worker
  calls `audio_bed.stop(emergency=True)` unconditionally before each
  `request_mood`.  This means clicking a preview button while agenda music or
  a boundary-triggered track is playing will cut it off immediately with no
  crossfade — instead of the 6-second policy fade.  For a manual "test this
  mood" button this is acceptable (the user's intent is to hear the preview
  now), but it is a sharper interruption than the pre-fix behavior.  If a
  future owner requirement calls for preserving the crossfade on the preview
  path, consider the `preview=True` engine parameter described in Alternative C
  above.

---

## Tests that lock this decision

All in `tests/test_music_preview_singleflight.py`:

| Test | What it verifies | Was RED before fix |
|---|---|---|
| `test_rapid_preview_clicks_spawn_one_worker` | Max concurrency depth == 1 across N rapid clicks | Yes (depth [1..8]) |
| `test_rapid_preview_clicks_one_active_channel` | Old channel receives `stop()`, not `fadeout(6000)`, before new one plays | Yes (fadeout(6000)) |
| `test_last_clicked_mood_wins` | Final active mood == last clicked mood | Passed already (coincidental) |
| `test_boundary_transition_keeps_crossfade` | `on_boundary` still calls `fadeout(policy.fade_ms)` on old channel | Passed (regression guard) |
| `test_dispatch_audio_play_still_spawns_thread` | `_dispatch_audio_play` (agenda path) still runs on a worker thread | Passed (regression guard) |

---

## Files changed

| File | Change |
|---|---|
| `opencohost/ui/app_shell.py` | Replaced 9-line `_music_play_mood` with 28-line single-flight version; net line count 2679 → 2699 |
| `tests/test_music_preview_singleflight.py` | New: 5 tests for Bug A (RED → GREEN evidence) |
| `docs/sesiondanger/ADR-SD-001-music-preview-single-flight.md` | This document |
