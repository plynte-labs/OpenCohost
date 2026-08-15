# Support

OpenCohost is an open-beta project built and maintained by one developer. There is no commercial support, guaranteed response time, or SLA. Help arrives on a best-effort basis, from me and from whoever else in the community has hit the same thing.

---

## Before asking for help

1. Check the [Quick Start guide](docs/QUICKSTART.md) — most setup issues are covered there.
2. Search [existing Issues](https://github.com/plynte-labs/opencohost/issues?q=is%3Aissue) — including closed ones — to see if your question has already been answered.
3. If the app fails to start, check the log files first (see [Logs](#reading-the-logs) below).

---

## Where to get help

| Type of problem | Where to go |
|---|---|
| Bug or unexpected behavior | [Open an issue](https://github.com/plynte-labs/opencohost/issues/new?template=bug_report.md) using the **Bug Report** template |
| Setup / configuration questions | [Open an issue](https://github.com/plynte-labs/opencohost/issues/new) and put `[Question]` in the title |
| Feature ideas | [Open an issue](https://github.com/plynte-labs/opencohost/issues/new?template=feature_request.md) using the **Feature Request** template |
| Security vulnerability | [Open a private Security Advisory](https://github.com/plynte-labs/opencohost/security/advisories/new) (do NOT open a public issue) |

GitHub Discussions is not enabled on this repository — Issues is the single
public channel for everything except security reports.

### Opening a bug report

Use the **Bug Report** issue template. Include:

- OpenCohost version or commit (`git rev-parse --short HEAD`, or the release tag)
- Python version (`python --version`)
- OS and version (Windows 10/11, Ubuntu 22.04, etc.)
- Ollama version (`ollama --version`)
- Steps to reproduce
- What you expected vs. what actually happened
- Relevant log output (see [Logs](#reading-the-logs) below)

The more detail you provide, the faster someone can help.

---

## Common setup problems

Most first-run failures fall into one of these categories. Check [docs/QUICKSTART.md](docs/QUICKSTART.md) for step-by-step instructions.

### Ollama is not running

OpenCohost requires [Ollama](https://ollama.com/) to be installed and running before launch.

```powershell
# Start Ollama (leave this terminal open)
ollama serve

# Pull a model (in a second terminal)
ollama pull llama3
```

If Ollama is unreachable at `http://127.0.0.1:11434`, the LLM features will be disabled at startup. The status indicator in the app will show the connection state.

### No voice output

The default voice uses **Microsoft Edge-TTS**, a free cloud synthesis service. This requires an active internet connection. Kira's outgoing spoken text is sent to Microsoft's servers for synthesis; nothing else is included in that request — no viewer chat, no prompts, no LLM context, no conversation memory.

Those stay on your machine under the shipped defaults. The one exception is the optional cloud LLM provider, which is off by default: if you enable it, the full prompt — including the filtered viewer-chat context — goes to the endpoint you chose. See [PRIVACY.md](docs/PRIVACY.md#optional-cloud-llm-providers-opt-in).

If you want fully offline voice, install the `local-tts` extra:

```powershell
pip install -e ".[local-tts,integrations,api]"
```

Then select a Piper TTS voice in Settings. See [docs/QUICKSTART.md](docs/QUICKSTART.md) for model download instructions.

### Dependency installation errors

Use the recommended install command with the correct extras for your setup:

```powershell
# Default (Edge-TTS voice + OBS / VRAM integrations + the HTTP API)
pip install -e ".[cloud-tts,integrations,api]"

# With offline Piper TTS as well
pip install -e ".[cloud-tts,integrations,api,local-tts]"
```

The `api` extra is not optional in practice: it installs FastAPI and uvicorn,
which the Tauri front end spawns to reach the engine. Leave it out and the app
has no backend to talk to.

Python 3.10 or newer is required.

---

## Reading the logs

Logs are written automatically on every run, into a `logs/` folder under the
user data directory. **Where that directory lands depends on how you
installed**, and almost everyone reading this is in the first row:

| How you installed | Log folder |
|---|---|
| From source (`pip install -e .`) — the normal case | `<repo>/logs/` — the repository root you cloned into |
| Frozen / packaged build, Windows | `%APPDATA%\OpenCohost\logs\` |
| Frozen / packaged build, Linux / macOS | `~/.config/OpenCohost/logs/` |

The `%APPDATA%` path only applies to a frozen build: outside one,
`opencohost/config/storage.py` resolves the user data directory to the repo
root, and `LOG_DIR` follows it (`opencohost/config/settings.py`).

Log files are named `opencohost_<YYYYMMDD_HHMMSS>.log`. For verbose output, set the environment variable `OPENCOHOST_DEBUG=1` before launching.

---

## Honest expectations

One developer maintains this, across LLM orchestration, TTS, live chat, OBS and a
desktop UI. That is the honest constraint behind everything below:

- **No guaranteed response time.** Issues get read, but they are reviewed when I have capacity.
- **No SLA.** This is not a commercial product.
- **Best effort.** Some issues will stay open if they are hard to reproduce or outside current scope. An issue left open is a backlog entry, not a dismissal.
- **Pull requests are welcome.** If you can fix a bug or improve the docs, a PR is usually the fastest path to a resolution — and for front-end, styling, or design work, the UI lives in its own repository and is an easy place to start.

Thanks for your patience, and for anything you report.

---

## Contributing

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for setup instructions and [CONTRIBUTING.md](CONTRIBUTING.md) for pull request guidelines, commit conventions, and the test suite.
