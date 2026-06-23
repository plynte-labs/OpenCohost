---
name: Bug report
about: Something is not working as expected in OpenCohost
title: "[Bug] "
labels: bug
---

## Environment

| Field | Value |
|---|---|
| **OS** | <!-- e.g. Windows 11 22H2, Ubuntu 24.04 --> |
| **Python version** | <!-- `python --version` --> |
| **Ollama version** | <!-- `ollama --version` --> |
| **Ollama model in use** | <!-- e.g. llama3, qwen3:4b --> |
| **OpenCohost version / commit** | <!-- `git rev-parse --short HEAD` or release tag --> |
| **TTS mode** | <!-- Edge-TTS (default) or Piper (local-tts extra) --> |

---

## What happened

<!-- A clear description of the bug. What did you observe? -->

---

## What you expected to happen

<!-- What should have happened instead? -->

---

## Steps to reproduce

1. 
2. 
3. 

<!-- If the issue is intermittent, note how often it occurs and under what conditions. -->

---

## Relevant logs

Log files are written to:

- **Windows:** `%APPDATA%\OpenCohost\logs\`
  (e.g. `C:\Users\<you>\AppData\Roaming\OpenCohost\logs\`)
- **Linux / macOS:** `~/.local/share/OpenCohost/logs/`

Files to check:

| File | Contains |
|---|---|
| `opencohost_<YYYYMMDD_HHMMSS>.log` | Main runtime log — start here |
| `acciones.jsonl` | Action/event log with structured entries |
| `crash.log` | Crash summary (if the app exited unexpectedly) |
| `fatal.log` | Low-level fault handler output (hard crashes only) |

For verbose output, set `OPENCOHOST_DEBUG=1` before launching:

```powershell
# Windows PowerShell
$env:OPENCOHOST_DEBUG=1; python -m opencohost
```

```bash
# Linux / macOS
OPENCOHOST_DEBUG=1 python -m opencohost
```

<details>
<summary>Paste log excerpt here (redact any personal info)</summary>

```
<!-- paste log lines here -->
```

</details>

---

## Screenshots

<!-- If the bug has a visible UI symptom, attach a screenshot here. -->
<!-- Drag and drop images directly into this text box. -->

---

## Additional context

<!-- Any other detail that might help: hardware specs (especially VRAM if using a local model),
     OBS version if avatar / scene switching is involved, stream platform (YouTube / Twitch),
     or anything unusual about your setup. -->
