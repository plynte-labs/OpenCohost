# OpenCohost Work Methodology

OpenCohost work follows controlled validation: small changes, real evidence, and
clear separation between current behavior and future plans.

## Core Rule

Do not expand the product blindly. Validate existing behavior, document reality,
and only then decide whether to build, refactor, package, or polish.

## Work Modes

| Mode | Use when | Requirements |
|---|---|---|
| Direct tiny fix | The behavior is understood and the change is narrow. | Preserve behavior, verify with a focused test or inspection, keep the diff small. |
| Conductor track | The change is ambiguous, multi-file, architectural, risky, or release-facing. | Create or update a track before implementation. Separate exploration, design, apply, and review. |
| Documentation track | The output is public docs, onboarding, architecture, testing, methodology, or contribution rules. | Start from source/test/config evidence. Label current behavior, known limitations, decisions, and future plans. |
| Runtime validation | The behavior depends on real audio, UI event loops, local services, OBS, OAuth, or stream state. | Do not pretend unit tests prove the full runtime. Use manual or opt-in smoke validation when appropriate. |

## Evidence Model

Important claims should be backed by one or more of:

- source files,
- tests,
- configuration files,
- committed changes,
- accepted Conductor track documents,
- manual validation notes.

Recent memory or agent handoff notes can guide investigation, but they should
not be the only proof for public documentation.

## Claim Labels

Use these labels when writing public docs:

| Label | Meaning |
|---|---|
| Current behavior | Verified in source, tests, or committed runtime behavior. |
| Current configuration | Verified in configuration or setup files. |
| Known limitation | A verified gap, risk, or missing validation. |
| Design decision | A documented decision with rationale. |
| Future plan | Not implemented yet; belongs in a track, roadmap, or deferred section. |

## Change Workflow

1. Check the current worktree before editing.
2. Read the relevant handoff, rules, and module docs.
3. Verify the current behavior before changing it.
4. Keep the change scoped to one concern.
5. Add or update focused tests when practical.
6. For runtime-dependent behavior, record what automated tests cannot prove.
7. Review the diff for unrelated changes, private artifacts, and unsupported claims.
8. Commit only when explicitly requested by the maintainer.

## Feature Preservation

Before modifying, extracting, or removing an existing feature:

- verify how it works today,
- identify whether it is a feature gate, privacy boundary, validation rule, or fallback path,
- avoid mixing refactors with behavior changes,
- ask for a design track when the behavior is unclear or risky.

## Runtime Boundaries

Some behavior cannot be fully proven by unit tests alone:

- real audio device behavior,
- pygame mixer/native behavior,
- Tk mainloop and worker-thread interactions,
- Qwen subprocess lifecycle,
- OBS websocket behavior,
- YouTube/OAuth behavior,
- real stream/chat service behavior.

For these areas, automated tests should be paired with manual validation or
opt-in runtime smoke checks.

## Privacy and Safety Boundaries

- Do not expose raw chat to LLM prompts, diagnostics, logs, or persistence.
- Keep diagnostics aggregated as counts, reasons, or summaries.
- Keep generated runtime data, tokens, local memory stores, logs, and model caches out of public commits.
- Treat OAuth credentials and stream service tokens as local/private data.

## Documentation Workflow

Public docs should be produced in this order:

1. documentation index,
2. methodology,
3. testing guide,
4. architecture map,
5. module docs,
6. contributing/security/license/notices,
7. README public entry point,
8. final documentation audit.

README comes late because it should summarize validated documentation instead of
inventing product claims early.

## Future Plans

Future work should live in:

- Conductor tracks for active or pending design/implementation work,
- a curated roadmap only after public wording is approved,
- clearly labeled `Deferred Work` sections when needed to explain a current boundary.

Never put future plans inside a `Current State` section.
