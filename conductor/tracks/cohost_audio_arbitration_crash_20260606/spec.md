# Cohost Audio Arbitration Crash

## Overview

During a real cohost-mode run, Kira answered a direct/operator interaction while agenda mode continued advancing from a prefetched response. The process then exited without a Python traceback immediately after starting another TTS playback. The fix scope is not to reproduce a native pygame crash; it is to close the deterministic logic hole that allows overlapping or re-entrant speech pipelines.

## Functional Requirements

- Direct/operator interactions must not start LLM/TTS work while agenda audio is already speaking.
- Agenda-prefetch playback must not start while non-agenda/direct processing or speech is active.
- If a direct interaction conflicts with a prefetched agenda response, the prefetched agenda response must be discarded instead of spoken.
- Normal agenda-to-agenda prefetch chaining may continue when the active owner is still agenda.

## Non-Functional Requirements

- Keep the fix minimal and low-risk.
- Do not change Kira persona, agenda generation policy, OBS behavior, SmartAggregator raw-chat boundaries, or TTS model behavior.
- Avoid attempting to test native pygame crashes directly; test the arbitration contract that prevents the crash path.

## Acceptance Criteria

- A red test first proves direct commands are queued while agenda speech is active.
- A red test first proves agenda-prefetch does not call `play_prefetched_agenda()` while direct processing is active.
- A stress-style test covers repeated direct interactions while agenda audio is active.
- Targeted Kira orchestration tests pass.
- Manual verification plan is available for cohost-mode runtime validation.

## Out of Scope

- Rewriting the whole audio pipeline.
- Streaming speech pipeline refactor.
- Native pygame crash harness.
- Product UI changes.
