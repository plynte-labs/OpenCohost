# Project Workflow

## Guiding Principles

1. **The Plan is the Source of Truth:** All work must be tracked in `plan.md`
2. **The Tech Stack is Deliberate:** Changes to the tech stack must be documented in `tech-stack.md` *before* implementation
3. **Test-Driven Development:** Write unit tests before implementing functionality
4. **High Code Coverage:** Aim for >80% code coverage for all modules
5. **User Experience First:** Every decision should prioritize user experience
6. **Non-Interactive & CI-Aware:** Prefer non-interactive commands. Use `CI=true` for watch-mode tools to ensure single execution.
7. **Memory First:** Before planning or coding, recover Engram context for project `voiceai` with `mem_context` first, then `mem_search` when needed.

## Task Workflow

All tasks follow a strict lifecycle:

### Standard Task Workflow

1. **Select Task:** Choose the next available task from `plan.md` in sequential order

2. **Mark In Progress:** Before beginning work, edit `plan.md` and change the task from `[ ]` to `[~]`

3. **Write Failing Tests (Red Phase):**
   - Create a new test file for the feature or bug fix.
   - Write one or more unit tests that clearly define the expected behavior and acceptance criteria for the task.
   - **CRITICAL:** Run the tests and confirm that they fail as expected. This is the "Red" phase of TDD. Do not proceed until you have failing tests.

4. **Implement to Pass Tests (Green Phase):**
   - Write the minimum amount of application code necessary to make the failing tests pass.
   - Run the test suite again and confirm that all tests now pass. This is the "Green" phase.

5. **Refactor (Optional but Recommended):**
   - With the safety of passing tests, refactor the implementation code and the test code to improve clarity, remove duplication, and enhance performance without changing the external behavior.
   - Rerun tests to ensure they still pass after refactoring.

6. **Verify Coverage:** Run coverage reports:
   ```bash
   pytest --cov=ui --cov=core --cov=smart_aggregator --cov=stream_admin --cov-report=html
   ```
   Target: >80% coverage for new code.

7. **Document Deviations:** If implementation differs from tech stack:
   - **STOP** implementation
   - Update `tech-stack.md` with new design
   - Add dated note explaining the change
   - Resume implementation

8. **Commit Code Changes:**
   - Stage all code changes related to the task.
   - Propose a clear, concise commit message e.g, `refactor(ui): Extract VoiceControlPanel from app.py`.
   - Perform the commit.

9. **Attach Task Summary with Git Notes:**
   - **Step 9.1: Get Commit Hash:** Obtain the hash of the *just-completed commit* (`git log -1 --format="%H"`).
   - **Step 9.2: Draft Note Content:** Create a detailed summary for the completed task. This should include the task name, a summary of changes, a list of all created/modified files, and the core "why" for the change.
   - **Step 9.3: Attach Note:** Use the `git notes` command to attach the summary to the commit.

10. **Get and Record Task Commit SHA:**
    - **Step 10.1: Update Plan:** Read `plan.md`, find the line for the completed task, update its status from `[~]` to `[x]`, and append the first 7 characters of the *just-completed commit's* commit hash.
    - **Step 10.2: Write Plan:** Write the updated content back to `plan.md`.

11. **Commit Plan Update:**
    - **Action:** Stage the modified `plan.md` file.
    - **Action:** Commit this change with a descriptive message (e.g., `conductor(plan): Mark task 'Extract VoiceControlPanel' as complete`).

### Phase Completion Verification and Checkpointing Protocol

**Trigger:** This protocol is executed immediately after a task is completed that also concludes a phase in `plan.md`.

1. **Announce Protocol Start:** Inform the user that the phase is complete and the verification and checkpointing protocol has begun.

2. **Ensure Test Coverage for Phase Changes:**
   - **Step 2.1: Determine Phase Scope:** Read `plan.md` to find the Git commit SHA of the *previous* phase's checkpoint.
   - **Step 2.2: List Changed Files:** Execute `git diff --name-only <previous_checkpoint_sha> HEAD` to get a precise list of all files modified during this phase.
   - **Step 2.3: Verify and Create Tests:** For each code file changed, verify a corresponding test file exists. If missing, create one following existing test conventions.

3. **Execute Automated Tests with Proactive Debugging:**
   - Announce the exact shell command: `pytest tests/ -v --cov=ui --cov-report=term-missing`
   - Execute the command.
   - If tests fail, attempt to fix a maximum of two times. If still failing, stop and report.

4. **Propose a Detailed, Actionable Manual Verification Plan:**
   - Generate step-by-step verification plan based on `product.md`, `product-guidelines.md`, and `plan.md`.

5. **Await Explicit User Feedback:**
   - Ask for confirmation. PAUSE and await response.

6. **Create Checkpoint Commit:**
   - Stage all changes. Commit with message `conductor(checkpoint): Checkpoint end of Phase X`.

7. **Attach Auditable Verification Report using Git Notes**

8. **Get and Record Phase Checkpoint SHA**

9. **Commit Plan Update**

10. **Announce Completion**

### Quality Gates

Before marking any task complete, verify:

- [ ] All tests pass
- [ ] Code coverage meets requirements (>80%)
- [ ] Code follows PEP 8
- [ ] All public functions have docstrings
- [ ] Type hints present on public functions
- [ ] No linting errors (ruff/flake8)
- [ ] Documentation updated if needed
- [ ] No security vulnerabilities introduced
- [ ] Thread safety verified for shared state
- [ ] Resilience patterns tested (circuit breaker, retry, fallback)
- [ ] Idempotency verified for state-changing operations

## Development Commands

### Setup
```powershell
# Client environment (flux_env)
E:\Miniconda\envs\flux_env\python.exe -m pip install -r requirements.txt

# TTS server environment (xtts_env)
E:\Miniconda\envs\xtts_env\python.exe -m pip install -r requirements.txt
```

### Daily Development
```powershell
# Run tests
pytest tests/ -v

# Run tests with coverage
pytest tests/ -v --cov=ui --cov=core --cov=smart_aggregator --cov=stream_admin --cov-report=term-missing

# Lint
ruff check .

# Format
ruff format .
```

### Before Committing
```powershell
ruff check . && ruff format . && pytest tests/ -v --cov=ui --cov-report=term-missing
```

## Testing Requirements

### Unit Testing
- Every module must have corresponding tests.
- Use pytest fixtures for setup/teardown.
- Mock external dependencies (Ollama, TTS server, YouTube API).
- Test both success and failure cases.
- Test thread safety for shared state.

### Integration Testing
- Test complete user flows (voice input -> LLM -> TTS -> playback)
- Verify WebSocket reconnection
- Verify OAuth token refresh
- Test Smart Aggregator pipeline end-to-end

### Resilience Testing
- Test circuit breaker behavior for Ollama, TTS server, YouTube API
- Test graceful degradation when subsystems fail
- Test idempotency of state-changing operations
- Test retry logic with backoff
- Test timeout handling

## Code Review Process

### Self-Review Checklist

1. **Functionality**
   - Feature works as specified
   - Edge cases handled
   - Error messages are user-friendly

2. **Code Quality**
   - Follows PEP 8
   - DRY principle applied
   - Clear variable/function names
   - Appropriate comments and docstrings

3. **Testing**
   - Unit tests comprehensive
   - Integration tests pass
   - Coverage adequate (>80%)
   - Resilience tests included

4. **Security**
   - No hardcoded secrets
   - Input validation present
   - Thread safety verified

5. **Performance**
   - No blocking calls in hot paths
   - Audio status updates lightweight
   - No heavy animations during inference/TTS

## Commit Guidelines

### Message Format
```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

### Types
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Formatting
- `refactor`: Code change that neither fixes a bug nor adds a feature
- `test`: Adding missing tests
- `chore`: Maintenance tasks

## Definition of Done

A task is complete when:

1. All code implemented to specification
2. Unit tests written and passing
3. Code coverage meets project requirements
4. Documentation complete (if applicable)
5. Code passes all configured linting and static analysis checks
6. Implementation notes added to `plan.md`
7. Changes committed with proper message
8. Git note with task summary attached to the commit
9. Engram memory saved for decisions, discoveries, and bugfixes

## Continuous Improvement

- Review workflow weekly
- Update based on pain points
- Document lessons learned
- Keep things simple and maintainable
