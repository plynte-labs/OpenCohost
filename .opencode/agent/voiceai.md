You are the VoiceAI general agent. Know the project, its conventions, and work with stress-first TDD.

## Project context
- Python 3.x, Pygame audio, CustomTkinter GUI (ctk), Ollama LLM
- Local Flask server (`server_qwen.py`) for zero-shot voice synthesis: Qwen3-TTS (offline, VRAM-heavy) + Edge-TTS (lightweight fallback)
- PyTest, stress-first TDD
- Structure: `core/` (engine + telemetry), `ui/` (CustomTkinter), `tests/` (pytest), `tools/` (scripts)
- Conventions: snake_case, type hints, dataclasses, Spanish logging

## Tkinter concurrency — CRITICAL
- Tkinter does NOT tolerate concurrency. Any async UI update from chat threads (Twitch/YouTube) MUST:
  1. `with self._chat_lock:` before mutating `_seen_chat_ids`
  2. `self.after_cancel(prev_id)` before `self.after()` to avoid race conditions
  3. If `after_cancel` raises RuntimeError, log debug only and reschedule — do not re-raise
- Real bugs from violating this: silent chat thread death (Bug H), disappearing avatar (Bug E)

## Project rules
- NEVER break legacy `_hablar()` method
- Use Strangler Pattern for new features
- Do NOT touch `data/`, `models/`, `.env`
- Feature gates: never remove without confirming purpose
- Do NOT git commit/push

## Stress-first TDD (mandatory)
1. Red test capturing exact behavior
2. Minimal change to pass
3. Focused test → green
4. Try to break your own test (timeouts, invalid state, partial failures, retries, rollback)
5. Valuable case → new red test and repeat
6. Stop when new cases are over-engineering
7. Focused regression → work unit ready

## SmartAggregator
- Never expose raw chat to LLM, UI diagnostics, logs, or persistence
- Cohost/Agenda: agenda topics first, chat is secondary compact context
- LiveVoice/PTT: separate pipelines, do not touch when tuning SmartAggregator
