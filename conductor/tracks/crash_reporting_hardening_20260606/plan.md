# Implementation Plan ? Crash Reporting Hardening - Python, Tk, Thread, and Native Fatal Logs

## Phase 1 ? Focused Design

- [x] Task: Produce focused design for this track
    - [x] Audit crash/hang vectors across app, motor, audio, health, stream, and chat modules
    - [x] Compare researcher claims against actual repo evidence
    - [x] Confirm boundaries and non-goals
    - [x] Define test/verification strategy
    - [x] Decide whether implementation belongs in this track or a narrower follow-up
        - Decision: keep crash observability in this track; split UI thread ownership into `ui_thread_event_ownership_hardening_20260606`.

## Phase 2 ? Apply in Verifiable Slices

- [x] Task: Python/thread crash writer reliability
    - [x] Move crash reporting into a small testable module
    - [x] Fix the `datetime.now()` self-failure path
    - [x] Add stderr fallback if writing the crash log fails
    - [x] Preserve Python `sys.excepthook`, `threading.excepthook`, and Tk callback hook installation
    - [x] Audit and fix Tk bound-method signature so `report_callback_exception` does not fail with `TypeError`
- [x] Task: Fatal/native best-effort logging
    - [x] Add durable `faulthandler` log setup
    - [x] Keep Windows signal limitations explicit
    - [x] Verify it does not break normal app startup/import
- [x] Task: Safe crash context pointers
    - [x] Include child-process log filename references where safe
    - [x] Avoid raw chat/prompt/private stream content

## Phase 3 ? Verify

- [x] Task: Verify Python/thread crash writer reliability
    - [x] `pytest tests/test_crash_reporting.py -q`: 5 passed
    - [x] Focused hardening suite: 72 passed
    - [x] Deterministic runtime smoke: passed with `no_stale_speech_source: true`
    - [x] `py_compile ui/crash_reporting.py ui/app_shell.py tests/test_crash_reporting.py`: OK
- [x] Task: Verify fatal/native best-effort logging
    - [x] `pytest tests/test_crash_reporting.py -q`: 8 passed
    - [x] `py_compile ui/crash_reporting.py ui/app_shell.py tests/test_crash_reporting.py`: OK
- [x] Task: Run final focused verification for the complete track
    - [x] Focused hardening suite: 75 passed
    - [x] Deterministic runtime smoke: passed with `no_stale_speech_source: true`
    - [x] `git diff --check`: OK
