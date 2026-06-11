# Agent Instructions — RF3 Smart Aggregator

## Environment

Activate your project Python environment before running any command:

```bash
# Run all scripts and tests through the activated environment
python smart_aggregator/test_local.py
python -m pytest tests/
```

**Prohibited:**
- `pip install` / `pip uninstall` / `conda install`
- Modifying, updating, or removing packages from the environment
- Using `python` without activating the correct environment first
- Creating `requirements.txt` or modifying existing dependencies without prior approval

If you need a new dependency, ask first — do not install on your own.

---

## Goal

Implement the `smart_aggregator/` module in full according to `docs/RF3_Smart_Aggregator_Spec.md`.

---

## System Context

The project has **two applications** running in parallel:

1. **`OpenCohost`** (this project) — UI in `ui/app_shell.py`, AI engine, TTS, LLM
2. **`LiveAudio`** — Separate app that handles:
   - Microphone audio capture
   - Transcription via **Whisper**
   - Voice detection via **Silero VAD**
   - Sends transcriptions via WebSocket to OpenCohost

The **Smart Aggregator** (RF3) consumes chat from **YouTube Live**, NOT audio from LiveAudio. It does not need to integrate with Silero or Whisper. It only receives already-formatted chat messages and processes them through filters, vibe thermometer, and activity trigger.

---

## Step 0 — Initial Setup

```bash
# 1. Create and switch to feature branch
git checkout -b feature/rf3-smart-aggregator

# 2. Create directory structure
mkdir -p smart_aggregator data/smart_aggregator
touch smart_aggregator/__init__.py
```

---

## Step 1 — Implement in Order

Follow the implementation order from the spec (section "Suggested Implementation Order"):

1. **`smart_aggregator/session_history.py`** — Hybrid SQLite + JSONL persistence
2. **`smart_aggregator/message_filter.py`** — RF3.1 quality filters
3. **`smart_aggregator/chat_source.py`** — YouTube source using `pytchat`
4. **`smart_aggregator/vibe_thermometer.py`** — RF3.2 sentiment analysis
5. **`smart_aggregator/activity_trigger.py`** — RF3.3 activity trigger
6. **`smart_aggregator/aggregator.py`** — Main orchestrator
7. **`smart_aggregator/config.yaml`** — Unified configuration
8. **`smart_aggregator/test_local.py`** — Headless tests with mock data

---

## Step 2 — Implementation Rules

### Architecture
- Each class in its own `.py` file
- All classes accept `config: dict` in `__init__` (loaded from YAML)
- Callback interface for communicating with core (see spec)
- **DO NOT create new LLM instances** — accept `llm_interface: callable` as parameter

### Files to CREATE (only these)
```
smart_aggregator/
├── __init__.py
├── session_history.py
├── message_filter.py
├── chat_source.py
├── vibe_thermometer.py
├── activity_trigger.py
├── aggregator.py
├── config.yaml
└── test_local.py
```

### Files NOT to modify
- `ui/app_shell.py` (or legacy `ui/app.py`) — do not touch
- `core/llm_engine.py` — do not touch
- Any other existing file — do not touch

### Dependencies
- Only use: `pytchat` or `chatdownload`, `sqlite3` (stdlib), `pyyaml`, `requests`
- **DO NOT use:** `transformers`, `torch`, `tensorflow`, `nltk`, `textblob`, `vaderSentiment`

---

## Step 3 — Tests

Run `test_local.py` after implementing each class:

```bash
python smart_aggregator/test_local.py
```

Tests must cover scenarios TC3.1 to TC3.7 from the spec.

---

## Step 4 — Update Documentation

After completing each class, update:

1. **`docs/RF3_Smart_Aggregator_Spec.md`** — Mark the class as implemented in the corresponding section
2. **`docs/changes.md`** — Mark RF3.1-RF3.5 as complete in the table as they finish

---

## Step 5 — Final Verification

Before reporting completion, verify:

```bash
# 1. Functional import
python -c "from smart_aggregator import Aggregator; print('Import OK')"

# 2. Tests pass
python smart_aggregator/test_local.py

# 3. No existing files modified
git status
```

---

## Commit Message Format

```
feat(smart-aggregator): implement <feature>

- <RF3.X> <short description>
- <RF3.X> <short description>

Refs: docs/RF3_Smart_Aggregator_Spec.md
```

Example:
```
feat(smart-aggregator): implement session_history and message_filter

- RF3.1 MessageFilter with configurable thresholds
- RF3.4 SessionHistory with SQLite + JSONL persistence

Refs: docs/RF3_Smart_Aggregator_Spec.md
```

---

## Communication Contract with Core (ui/app_shell.py)

The aggregator is instantiated in `ui/app_shell.py` as follows (reference code — do NOT modify):

```python
from smart_aggregator import Aggregator

self.smart_agg = Aggregator(config_path="config/smart_aggregator.yaml")
self.smart_agg.on_filtered_message = self._on_kira_input   # filtered message → Kira pipeline
self.smart_agg.on_vibe_update = self._update_vibe_display  # vibe temperature update
self.smart_agg.on_activity_trigger = self._handle_activity_trigger  # chat spike
```

**The module only defines the callbacks. The core decides what to do with them.**

---

## Resources

- Full spec: `docs/RF3_Smart_Aggregator_Spec.md`
- Test cases: same section in spec (TC3.1 — TC3.7)
- Sample configuration under each class in the spec
