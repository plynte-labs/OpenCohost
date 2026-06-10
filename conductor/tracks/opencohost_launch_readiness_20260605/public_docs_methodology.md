# OpenCohost Public Documentation Methodology

This document defines the public documentation direction for OpenCohost. The
goal is not to copy another project mechanically. The goal is to give humans and
coding agents enough context to work safely in a local-first cohost runtime.

## Decision

OpenCohost public documentation must prioritize orientation, architecture, work
methodology, testing, contribution rules, security/trust boundaries, licensing,
and third-party notices before broad packaging or feature expansion.

All public documentation artifacts must be written in English.

## Documentation Set

| Document | Purpose | Reader |
|---|---|---|
| `README.md` | Product entry point, quick start, honest current status, and links by goal. | New users and contributors |
| `AGENTS.md` | Root rules for coding agents and contributors working with agent assistance. | Agents and maintainers |
| `docs/INDEX.md` or `docs/agents/index.md` | Navigation index for humans and coding agents. | Anyone entering the repo |
| `docs/ARCHITECTURE.md` | System map, ownership boundaries, file/module context, and decision rationale. | Contributors and reviewers |
| `docs/METHODOLOGY.md` | How work is planned, implemented, reviewed, validated, and committed. | Contributors |
| `docs/TESTING.md` | Test catalog, commands, manual validation gates, and runtime smoke boundaries. | Contributors and reviewers |
| `CONTRIBUTING.md` | PR expectations, acceptable changes, proof requirements, and review norms. | External contributors |
| `SECURITY.md` or `docs/TRUST_MODEL.md` | Local-first trust model, privacy boundaries, token handling, and vulnerability reports. | Users and researchers |
| `LICENSE` | Project license. | Everyone |
| `THIRD_PARTY_NOTICES.md` | Dependencies, models, assets, licenses, and attribution requirements. | Users and maintainers |

## Agent/Contributor Index Requirements

The index should answer one question fast: "Where do I go before touching this
area?"

It should include:

- Start here: README, architecture, methodology, testing.
- Runtime debugging path: crash reporting, logs, MotorVocalIA, TTS, UI events.
- UI work path: app shell, panels, Tk mainloop ownership, preview expectations.
- Audio/TTS path: light/heavy TTS, Qwen subprocess, pygame/audio limitations.
- Stream/Admin path: OBS, YouTube OAuth, Stream Admin config, token privacy.
- SmartAggregator path: raw chat policy, filter boundaries, agenda vs direct interaction.
- Release path: repo safety, migration policy, public docs, manual validation gates.

## Architecture Documentation Requirements

Architecture documentation must explain what exists and why it exists. It should
not only list files.

Required sections:

1. Product boundary: OpenCohost as the product, Kira as the preserved cohost/persona.
2. Runtime map: UI shell, MotorVocalIA, TTS, LLM, OBS, Stream Admin, SmartAggregator.
3. Ownership boundaries: UI thread ownership, audio ownership, raw chat privacy, config ownership.
4. Important decisions:
   - Local-first direction.
   - No blind feature expansion during launch readiness.
   - Kira remains.
   - Direct user interaction must not be spoken over by agenda prefetch.
   - Worker/UI callbacks must be routed through the Tk main loop.
   - Crash reporting needs layered evidence: Python hooks, Tk hooks, thread hooks, fatal logs.
5. Deferred tracks:
   - `runtime_smoke_harness_20260606`
   - Packaging/installer work
   - Broad Product UI work
   - Qwen lifecycle hardening unless runtime validation proves it is needed

## Work Methodology Requirements

OpenCohost work should follow controlled validation:

- Prefer small, reviewable changes.
- Do not add broad features when unresolved runtime validation remains.
- Use Conductor tracks for ambiguous, risky, multi-file, or architectural work.
- Keep tiny fixes direct only when behavior and validation are clear.
- Preserve existing behavior before refactoring.
- Require real behavior proof for user-visible runtime, audio, UI, OBS, or stream changes.
- Keep unit tests, focal tests, and manual runtime validation clearly separated.
- Never publish runtime-private artifacts, local tokens, generated data, or local memory stores.

## Testing Documentation Requirements

`docs/TESTING.md` should document the current test suite deeply enough that a
new contributor can choose the right proof without guessing.

Required sections:

- Test command index.
- Test catalog by subsystem.
- What each test group proves.
- What each test group does not prove.
- Manual validation gates.
- Opt-in runtime/smoke validation policy.
- Known local environment assumptions.
- How to add tests safely.

Important boundary:

Unit tests are necessary but not enough for OpenCohost. They cannot fully prove
real audio device behavior, pygame mixer behavior, Tk mainloop/thread
interaction, Qwen subprocess lifecycle, OBS websocket behavior, or real stream
service interactions.

## OpenClaw-Inspired Pattern, Adapted

OpenClaw provides a useful public-repo pattern: README as product router,
AGENTS as hard working rules, CONTRIBUTING as PR policy, SECURITY as trust
model, and architecture/docs links by goal.

OpenCohost should adapt that pattern to its own reality:

- Local-first live cohost runtime.
- Python/Tk/audio constraints.
- Stream integrations and OAuth token privacy.
- Kira persona continuity.
- Stronger manual/runtime validation boundaries.

The documentation should be honest about current prototype status. It should not
pretend packaging, installer, or first-run readiness are complete until they are
validated.
