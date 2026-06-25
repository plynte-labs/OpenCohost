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
- (appended as discovered)
