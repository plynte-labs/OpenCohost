# Crash Reporting Hardening - Deep Exploration

## Context

This exploration expands `crash_reporting_hardening_20260606` after reviewing the
researcher notes against the actual VoiceAI/OpenCohost codebase.

The researcher is directionally right: one global `try/except` is not enough for
Python/Tk/thread/native-ish runtime failures. However, several generic warnings
need to be classified against real code so the design does not overfit to
irrelevant risks.

## Current Crash Reporting State

- `ui/app_shell.py` installs a global crash handler at import time.
- It hooks:
  - `sys.excepthook`
  - `threading.excepthook`
  - `tkinter.Tk.report_callback_exception`
- It writes to `VOICEAI_CRASH_LOG` or `logs/crash.log`.
- It currently calls `datetime.now()` without importing `datetime`, so the crash
  writer itself can fail when first used.
- `main.py` registers an `atexit` cleanup handler for Qwen/health monitor and
  Ollama model release, but it is cleanup-only, not crash evidence capture.

## Researcher Claims - Repo-Grounded Verdict

### Correct / Relevant

- Python, Tk, and thread exceptions need separate handling.
- Native/fatal exits may bypass Python exception hooks.
- Daemon threads can end abruptly and skip cleanup during process shutdown.
- Tkinter thread-safety is a serious concern.
- stdout/stderr persistence matters for startup and child-process failures.

### Partially Relevant

- `asyncio` task exceptions: relevant as a general principle, but current code
  mostly uses `asyncio.run(...)` / `run_until_complete(...)`, not untracked
  `create_task(...)` patterns. This is not the main current blind spot.
- `subprocess.PIPE` deadlocks: possible in direct `OllamaStartupManager` use, but
  the AppShell startup path passes log files for stdout/stderr. Qwen subprocess
  also redirects stdout/stderr to files. Treat as a design guardrail, not a
  confirmed active bug.
- Sentry: useful for distributed production apps, but OpenCohost is local-first
  and privacy-sensitive. A local-first crash bundle should come before any
  external telemetry decision.

### False Positive / Out of Scope for This Track

- Mutation testing is not necessary for the first crash-reporting slice. It can
  be valuable later, but the immediate gap is missing durable evidence.
- Memory profiler / OOM debugging is useful for Qwen/Torch investigation but
  should not be the first implementation target for crash logging.

## Crash / Hang Vectors Found in Code

### 1. Crash Handler Self-Failure

**Evidence**

- `ui/app_shell.py` uses `datetime.now()` in `_write_crash()` without importing
  `datetime`.

**Risk**

- The handler intended to capture the crash can raise its own `NameError`,
  resulting in no `crash.log`.

**Severity**

- High. It directly explains why a Python/Tk/thread exception may leave no crash
  artifact.

### 2. Tkinter Updates from Background Threads

**Evidence**

- `MotorVocalIA` is a daemon thread and calls `self.ui_callback(...)`.
- In AppShell, `self.motor_ia = MotorVocalIA(self.log_queue, self._on_motor_event)`.
- `_on_motor_event()` invokes handlers directly, without scheduling the handler
  itself on the Tk main thread.
- Some handler internals use `_safe_after(...)`, but not all:
  - `_actualizar_pipeline(...)` directly calls `status_bar.update_pipeline_state`
    and `voice_panel.update_tts_label`.
  - `_log_accion(...)` directly writes through `AdvancedModePanel.log_action`.
  - `_start_speaking_alt_timer()` and `_tick_speaking_alt()` call `self.after`
    from the handler path.
  - `_hilo_grabacion()` uses direct `self.after(...)` from a recording thread.

**Risk**

- Tk can raise runtime errors such as "main thread is not in main loop", or worse,
  hit native/Tcl instability that Python hooks do not reliably capture.

**Severity**

- High for runtime stability. This is not only crash-reporting; it points to a
  separate UI-thread ownership hardening concern.

### 3. Pygame / Audio Native-ish Surface

**Evidence**

- `core/llm_engine.py` initializes and uses `pygame.mixer`.
- TTS playback uses `pygame.mixer.music.load/play/get_busy/unload`.
- `core/audio_bed.py` uses pygame mixer channels and sounds.
- Audio recording uses `sounddevice` and `soundfile`.

**Risk**

- Python exceptions are mostly caught around playback, but native audio backend
  failures can terminate or destabilize the process before Python can log them.

**Severity**

- Medium/High. It matches the original "pygame/audio real" concern and is best
  addressed through `faulthandler`, stderr persistence, and opt-in runtime smoke.

### 4. Qwen/Torch Separate Process

**Evidence**

- `core/health_monitor.py` starts `server_qwen.py` as a subprocess.
- Qwen stdout/stderr are already redirected to dedicated files.
- `server_qwen.py` imports torch/qwen_tts, loads model, and exposes Flask routes.

**Risk**

- Torch/model crashes may kill only the Qwen subprocess, not the UI process.
- Existing logs help, but app-level crash reporting should include pointers to
  `server_qwen_stdout.log` and `server_qwen_stderr.log`.

**Severity**

- Medium. Important for diagnosis, but not the same as app crash.

### 5. Daemon Threads and Lost Cleanup

**Evidence**

- `MotorVocalIA`, `HealthMonitor`, chat sources, OBS retry, model startup worker,
  stream admin workers, audio/recording workers, and UIState dispatcher use
  daemon/background threads.

**Risk**

- During shutdown, daemon threads can terminate without final logs. During normal
  runtime, unhandled thread exceptions should hit `threading.excepthook`, but
  only if not swallowed.

**Severity**

- Medium. Crash logging can improve evidence; full lifecycle cleanup is broader
  than this track.

### 6. Swallowed Exceptions / Observability Blind Spots

**Evidence**

- Several modules intentionally catch callback exceptions and `pass`, especially
  SmartAggregator/chat callbacks and UI logging helpers.

**Risk**

- This usually prevents crashes, but it also hides root causes. A crash-reporting
  design should not convert all swallowed exceptions into crashes, but it should
  identify critical callbacks where exceptions should be logged.

**Severity**

- Medium. More observability issue than fatal crash issue.

### 7. Subprocess Hang / Deadlock Surface

**Evidence**

- `core/ollama_startup.py` defaults stdout/stderr to `subprocess.PIPE` unless log
  paths are provided.
- AppShell does provide stdout/stderr log paths in the visible path.
- `core/health_monitor.py` redirects Qwen subprocess stdout/stderr to log files.

**Risk**

- Direct or future use of `OllamaStartupManager` without log paths could deadlock
  if the child writes enough output to PIPE.

**Severity**

- Low/Medium for current app path; worth documenting as a design invariant.

### 8. Mainloop / Shutdown Gaps

**Evidence**

- `main.py` does not wrap `mainloop()` in a top-level `try/finally`.
- `on_closing()` performs many cleanup steps and then `destroy()`.
- `atexit` cleanup exists but does not record crash context.

**Risk**

- If the app exits through a path that bypasses `on_closing()` or if native code
  terminates the process, cleanup/crash evidence may be incomplete.

**Severity**

- Medium.

## Recommended Design Direction

### Phase 1 - Local Crash Evidence First

Implement local-first crash capture before considering external telemetry:

- Fix crash handler self-failure (`datetime` import or safe timestamp source).
- Make `_write_crash()` defensive: if file write fails, still attempt stderr.
- Add `faulthandler.enable(...)` to a durable fatal log file.
- Consider `faulthandler.register(...)` only where supported; keep Windows limits
  explicit.
- Redirect or tee process-level stderr to a durable runtime log where feasible.
- Include environment/runtime metadata with no private raw chat:
  - Python version
  - platform
  - thread name
  - active feature flags/profile names only if safe
  - relevant log file paths

### Phase 2 - Thread/Tk Evidence

- Keep `threading.excepthook`, but verify it writes through the fixed crash
  writer.
- Add tests for:
  - main-thread unhandled exception
  - background thread exception
  - Tk `report_callback_exception`
  - crash writer failure fallback
- Do not rely on real Tk windows in unit tests unless isolated carefully.

### Phase 3 - Runtime Boundaries

- Record Qwen/Ollama child log paths in crash bundles.
- Treat native audio/Torch crashes as "best effort" with faulthandler + stderr,
  not as fully catchable Python exceptions.
- Pair with the pending runtime smoke harness only when operator opts in.

## What Experts Would Say

Experts would push back on a single magical crash handler. The reliable design
is layered:

1. Python exceptions: `sys.excepthook`, `threading.excepthook`, Tk callback hook,
   and focused tests.
2. Native-ish crashes: `faulthandler` and durable stderr/fatal logs.
3. Child processes: separate stdout/stderr logs and parent pointers to those logs.
4. Hangs: watchdog/runtime smoke, not just crash logs.
5. Privacy: local-first crash bundles and redaction before external telemetry.

The big architectural warning is that crash reporting will not make unsafe
cross-thread Tk access safe. It only gives evidence. UI-thread ownership should
be a separate hardening concern if this audit is confirmed as a runtime risk.

## Recommended Next Track Decisions

- Continue `crash_reporting_hardening_20260606` as an observability track.
- Do not expand it into fixing every crash vector.
- Consider a separate follow-up track for UI-thread event ownership if we decide
  to harden `_on_motor_event()` and related UI updates.
- Keep `runtime_smoke_harness_20260606` semi-real mode pending until explicit
  operator approval.
