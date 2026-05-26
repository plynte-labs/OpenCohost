# Implementation Plan — Editorial Cue Cards MVP

## Phase 1 — Final Discovery and Boundary Confirmation

- [x] Task: Confirm product framing
    - [x] Confirm this is an optional editorial production tool, not a search feature.
    - [x] Confirm the initial MVP is Agenda/Cohost-first with pre-stream or between-segment card preparation.
    - [x] Confirm no scraping, browser automation, hotkey ingestion, or PTT/voice activation is included.
    - [x] Confirm the first validation stream format: gaming/editorial discussion topics, with patch notes/news/general topics supported later by the same card shape.
- [x] Task: Map existing integration points
    - [x] Identify RF3 prompt assembly for compact chat context: `ui/app_shell.py::_on_smart_aggregated_context` and `ui/smart_aggregator_ui.py` RF3 prompt builder.
    - [x] Identify Agenda/Cohost block state and deterministic card link point: `AgendaTopic` / `KiraAgendaController` promotion path, with future `editorial_card_id` link before queue activation.
    - [x] Identify LLM Engine prompt/history handling so editorial context can be purged after use: `MotorVocalIA._generar_dialogo` and `_commit_history`; agenda sources already redact internal prompt history.
    - [x] Confirm SmartAggregator does not need modification; cue cards stay outside `smart_aggregator/*` and attach through Agenda/Cohost + LLM composition.
- [ ] Task: Define UX minimum
    - [ ] Decide the smallest Agenda/Cohost-compatible surface for attaching a card to a block.
    - [ ] Define maximum acceptable operator steps for pre-stream preparation and one-action block activation.
    - [ ] Define post-use rating options.
- [ ] Task: Conductor - User Manual Verification 'Phase 1 — Final Discovery and Boundary Confirmation' (Protocol in workflow.md)

## Phase 2 — Red Tests for Card Lifecycle and Store

- [x] Task: Add failing tests for card schema
    - [x] Validate required fields: `topic`, `summary`, `streamer_take`.
    - [x] Validate optional fields: `counterpoints`, `discussion_hooks`, `triggers`.
    - [x] Validate size limits for summary and final prompt block.
    - [x] Reject raw chat/raw page fields from stored cards.
- [x] Task: Add failing tests for lifecycle states
    - [x] Test `draft -> armed -> active -> used` transition.
    - [x] Test expired cards cannot become active.
    - [x] Test used cards do not auto-reactivate.
    - [x] Test only one active card is allowed.
- [x] Task: Add failing tests for SQLite/upsert behavior
    - [x] Test duplicate `topic_slug` updates the existing card.
    - [x] Test `last_used_at` and `use_count` update after use.
    - [x] Test armed card lookup is deterministic.
- [ ] Task: Conductor - User Manual Verification 'Phase 2 — Red Tests for Card Lifecycle and Store' (Protocol in workflow.md)

## Phase 3 — Implement EditorialCardStore MVP

- [x] Task: Implement card data model
    - [x] Keep model compact and serializable.
    - [x] Add validation for state, expiration, and size limits.
    - [x] Do not allow raw copied pages to become prompt context.
- [x] Task: Implement SQLite store
    - [x] Add table for editorial cards.
    - [x] Add unique `topic_slug` and upsert behavior.
    - [x] Add deterministic armed/active/used queries.
    - [x] Keep future vector index out of MVP; no vector implementation added.
- [x] Task: Implement rating records
    - [x] Store `useful | not_useful | unsure`.
    - [x] Store reason codes for usefulness/failure analysis.
    - [x] Avoid storing raw chat in rating records.
- [x] Task: Run focused store/model tests
    - [x] Use `E:\Miniconda\envs\flux_env\python.exe` for pytest.
    - [x] Fix only regressions inside the new editorial-card boundary.
- [ ] Task: Conductor - User Manual Verification 'Phase 3 — Implement EditorialCardStore MVP' (Protocol in workflow.md)

## Phase 4 — Prompt Injection and Purge Semantics

- [ ] Task: Build editorial context prompt block
    - [ ] Render only structured fields.
    - [ ] Use a bounded `<editorial_context>` block or equivalent clear delimiter.
    - [ ] Include instruction to prioritize clarity over jokes when using the card.
    - [ ] Avoid claiming source verification unless a later policy adds it.
- [ ] Task: Inject one active card into one request
    - [ ] Keep Kira's base system prompt unchanged.
    - [ ] Add card as user-level ephemeral context.
    - [ ] Do not append editorial block to long-term history.
- [ ] Task: Mark used and purge after generation
    - [ ] On successful use, mark card `used` and increment `use_count`.
    - [ ] On failed/cancelled generation, define whether card returns to `armed` or stays `active`.
    - [ ] Verify no-card path has identical behavior to current baseline.
- [ ] Task: Add prompt safety tests
    - [ ] Test no active card means no editorial block.
    - [ ] Test used card is absent from next request.
    - [ ] Test oversized card is rejected or compressed before injection.
    - [ ] Test internal labels/JSON are not expected in Kira's public output where output guards exist.
- [ ] Task: Conductor - User Manual Verification 'Phase 4 — Prompt Injection and Purge Semantics' (Protocol in workflow.md)

## Phase 5 — Agenda/Cohost Activation and Operator UX

- [ ] Task: Add pre-stream/between-segment card preparation flow
    - [ ] Prefer manual fields: `topic`, `summary`, `streamer_take`.
    - [ ] Optional fields may be left blank.
    - [ ] Enforce size limits before arming.
- [ ] Task: Link cards to Agenda/Cohost blocks
    - [ ] Add a deterministic association between an armed card and an Agenda/Cohost block or equivalent UI state.
    - [ ] Show which card is linked before activation.
    - [ ] Handle missing, expired, or already-used linked cards clearly.
- [ ] Task: Add deterministic Agenda/Cohost activation flow
    - [ ] Activate through block/UI selection only.
    - [ ] Do not implement PTT/voice command routing in MVP.
    - [ ] Do not implement passive keyword-trigger activation in MVP.
    - [ ] Ensure one active card is consumed by one Agenda/Cohost response and then marked used.
- [ ] Task: Add post-use rating flow
    - [ ] Allow quick `useful`, `not useful`, or `unsure` rating.
    - [ ] Allow optional reason code.
    - [ ] Do not interrupt the live flow if rating is skipped.
- [ ] Task: Add UX tests or controller tests
    - [ ] Test create -> arm -> link to Agenda block -> activate block -> use -> rate flow.
    - [ ] Test duplicate topic update warning/upsert.
    - [ ] Test missing/expired/used linked card does not inject context.
    - [ ] Test skipped rating does not break state.
- [ ] Task: Conductor - User Manual Verification 'Phase 5 — Agenda/Cohost Activation and Operator UX' (Protocol in workflow.md)

## Phase 6 — Validation, Kill Criteria, and Documentation

- [ ] Task: Run automated regression
    - [ ] Run new editorial card tests.
    - [ ] Run touched LLM prompt/history tests.
    - [ ] Run relevant SmartAggregator tests to prove no behavior drift.
- [ ] Task: Manual validation session
    - [ ] Prepare 5-10 cards before a test stream/session.
    - [ ] Link cards to Agenda/Cohost blocks.
    - [ ] Use cards with deterministic UI/block activation only.
    - [ ] Rate each use as useful/not useful/unsure.
    - [ ] Record whether Kira stayed natural or became a note reader.
- [ ] Task: Evaluate discard/redesign criteria
    - [ ] Stop or redesign if most cards are not useful.
    - [ ] Stop or redesign if operator friction is too high.
    - [ ] Stop or redesign if Kira leaks metadata or becomes rigid/repetitive.
    - [ ] Stop or redesign if the feature only feels valuable with autonomous cloud/browser search.
    - [ ] Stop or redesign if the feature only feels usable with PTT/voice activation in the first version.
- [ ] Task: Document results and next track options
    - [ ] Record whether optional extractor, FTS5 trigger search, or vector DB is justified.
    - [ ] Record whether hotkey/clipboard draft or PTT activation is justified after deterministic validation.
    - [ ] Record whether a news-specific policy/card type is worth a separate track.
    - [ ] Save Engram summary with decisions, gotchas, and verification commands.
- [ ] Task: Conductor - User Manual Verification 'Phase 6 — Validation, Kill Criteria, and Documentation' (Protocol in workflow.md)
