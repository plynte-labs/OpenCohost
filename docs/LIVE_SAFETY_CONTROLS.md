# Live Safety Controls

Controls added after a live test against a massive YouTube stream where SmartAggregator returned empty vibe responses and GPU usage spiked.

## SmartAggregator

- Default activity threshold is now `10 msg/s` in `config/smart_aggregator.yaml`.
- Above the live-safety threshold, SmartAggregator enters high-traffic mode:
  - samples accepted chat into compact intent context instead of processing every spike;
  - skips vibe LLM calls while traffic remains above threshold;
  - keeps raw chat persistence disabled; only compact context snapshots may be stored.
- Repeated empty/unparseable vibe responses trigger a cooldown/backoff before another vibe LLM call.
- Logs are state-transition style only, for example `Live-safe high traffic ON` or `Vibe en cooldown`, not per-message noise.

## Kira Co-host modes

Co-host Agenda has a `Modo vivo` selector:

- `live_safe`: default for real live streams; caps agenda output around 1100 chars (~25-40s).
- `monologue`: preserves longer Kira monologues but keeps them interruptible; cap 3000 chars.
- `test`: allows long test blocks; cap 6000 chars (~60-90s).

PTT/chat priority and agenda prefetch source-gating remain active: human input can pause cached agenda continuation and prevent the next long autonomous block from taking over.
