# Implementation Plan — Startup and Model Lifecycle Polish

## Phase 1 — Discovery and Lifecycle Boundaries

- [x] Task: Map current startup/shutdown paths for Ollama, OBS, and Qwen/TTS
    - [x] Identify where VoiceAI starts subprocesses versus where it only connects to external services.
    - [x] Identify current close/shutdown hooks in UI/app lifecycle.
    - [x] Identify temp file paths safe for janitor cleanup.
    - [x] Document forbidden touch points: SmartAggregator, prompts, LiveVoice/PTT, analytics.
- [x] Task: Define process ownership model
    - [x] Specify how code marks "started by VoiceAI" versus "external process detected".
    - [x] Specify what shutdown is allowed for owned resources.
    - [x] Specify what shutdown is forbidden for external resources.
- [ ] Task: Conductor - User Manual Verification 'Phase 1 — Discovery and Lifecycle Boundaries' (Protocol in workflow.md)

## Phase 2 — Tests for Startup State and Ownership

- [x] Task: Add failing tests for startup state transitions
    - [x] Test slow startup remains `starting`/`waiting`, not immediate critical failure.
    - [x] Test exhausted retries become `degraded` or `failed` with actionable reason.
    - [x] Test recovered service transitions to `ready`.
- [x] Task: Add failing tests for process ownership safety
    - [x] Owned Qwen/Ollama-like process receives shutdown attempt.
    - [x] External process is never killed by VoiceAI shutdown.
    - [x] Shutdown timeout returns control without blocking indefinitely.
- [x] Task: Add failing tests for janitor safety
    - [x] Old VoiceAI temp files can be cleaned.
    - [x] User files/assets outside controlled temp paths are ignored.
    - [x] Running cleanup twice is safe.
- [ ] Task: Conductor - User Manual Verification 'Phase 2 — Tests for Startup State and Ownership' (Protocol in workflow.md)

## Phase 3 — Implement Startup State Polish

- [x] Task: Implement explicit startup/degraded/ready/failure states
    - [x] Add or refine state representation in the smallest responsible module.
    - [x] Keep existing health semantics compatible with previous tests.
    - [x] Avoid changing fallback policy or generation behavior.
- [x] Task: Implement retry/backoff messaging for Ollama/OBS startup
    - [x] Log transitory startup as informational/debug, not critical error spam.
    - [x] Escalate only after configured attempts/timeouts.
    - [x] Preserve non-blocking operator warnings.
- [x] Task: Verify existing health/resilience tests
    - [x] Run focused tests around health monitor and AppShell OBS resilience.
    - [x] Fix only regressions caused by this phase.
- [ ] Task: Conductor - User Manual Verification 'Phase 3 — Implement Startup State Polish' (Protocol in workflow.md)

## Phase 4 — Implement Shutdown and Memory Release Lifecycle

- [x] Task: Implement owned-resource shutdown registry or equivalent minimal mechanism
    - [x] Track resources started by VoiceAI.
    - [x] Track external resources as protected/non-owned.
    - [x] Ensure shutdown operations are idempotent.
- [x] Task: Implement model/process release on normal close
    - [x] Attempt graceful shutdown/release for owned Qwen/TTS resources.
    - [x] Attempt safe model unload or equivalent for owned Ollama lifecycle only when applicable.
    - [x] Use short timeouts and never block UI indefinitely.
    - [x] Log success, timeout, or skipped external ownership clearly.
- [x] Task: Implement next-start janitor for crash recovery
    - [x] Clean only VoiceAI-controlled temp artifacts.
    - [x] Detect/report likely orphan leftovers without destructive action unless ownership is safe.
    - [x] Keep cleanup idempotent.
- [ ] Task: Conductor - User Manual Verification 'Phase 4 — Implement Shutdown and Memory Release Lifecycle' (Protocol in workflow.md)

## Phase 5 — Regression and Operator-Facing Validation

- [x] Task: Run focused automated regression
    - [x] Run tests for health monitor, AppShell OBS resilience, temp cleanup, LLM timeout/lifecycle-adjacent behavior.
    - [x] Use `E:\Miniconda\envs\flux_env\python.exe` for pytest.
    - [x] Confirm no websocket/runtime warnings are reintroduced.
- [ ] Task: Manual startup validation
    - [ ] Start VoiceAI with Ollama not ready and confirm non-critical startup state.
    - [x] Start VoiceAI with OBS unavailable and confirm degraded/non-blocking behavior.
        - Verified via `logs/voiceai_20260524_232748.log`: OBS unavailable no longer blocks the app, but the log exposed repeated `OBS WebSocket connection refused` ERROR spam.
        - Follow-up fix applied: OBS retry loop now calls `OBSClient.connect(log_failures=not logged_once)` so only the first operator-facing notice is emitted; refused retries are no longer ERROR spam.
    - [ ] Start VoiceAI after services become available and confirm transition to ready.
- [ ] Task: Manual shutdown validation
    - [ ] Confirm owned Qwen/TTS process releases memory/process on close.
    - [ ] Confirm external Ollama process is not killed if it was not started by VoiceAI.
    - [x] Confirm close does not hang UI.
        - Verified via `logs/voiceai_20260524_232748.log`: app closed and returned after normal shutdown.
    - [x] Confirm owned Ollama model releases on close.
        - Verified via `logs/voiceai_20260524_232748.log`: `Modelo Ollama qwen3:1.7b liberado`.
- [x] Task: Save Engram summary and update track plan with results
    - [x] Record decisions, gotchas, and final verification commands.
    - [x] Record any intentionally deferred follow-up.
- [ ] Task: Conductor - User Manual Verification 'Phase 5 — Regression and Operator-Facing Validation' (Protocol in workflow.md)

## Final Review Notes — 2026-05-25

- Automated QA verdict: PASS WITH WARNINGS.
- Focused regression after QA fix: `E:\Miniconda\envs\flux_env\python.exe -m pytest tests/test_obs_client.py tests/test_app_shell_obs_resilience.py tests/test_health_monitor.py tests/test_temp_file_cleanup.py tests/test_llm_engine_timeouts.py tests/test_llm_engine_tiers.py -q` → `134 passed`.
- User log review: `logs/voiceai_20260524_232748.log` confirmed owned Ollama model release on close and exposed remaining OBS retry ERROR spam.
- Final fix: `OBSClient.connect(log_failures=False)` supports silent retry-loop attempts; `AppShell` forwards `not logged_once` so the operator gets one notice instead of repeated ERROR logs.
- Learned: AppShell's `logged_once` gate was insufficient because `OBSClient.connect()` logged internally on every refused connection; lifecycle retry policy must be enforced at the lowest logging layer that sees the failure.
- Remaining manual validation before marking full track complete:
    - Ollama unavailable/slow startup path.
    - OBS becomes available after VoiceAI startup and transitions to connected/ready.
    - Real owned Qwen/TTS shutdown.
    - External Ollama process protection.
