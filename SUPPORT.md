# Support

OpenCohost is an open-beta project built and maintained by one developer. There is no commercial support, guaranteed response time, or SLA. Help arrives on a best-effort basis, from me and from whoever else in the community has hit the same thing.

---

## Before asking for help

1. Check the [Quick Start guide](docs/QUICKSTART.md) — most setup issues are covered there.
2. Search [existing Issues](https://github.com/plynte-labs/opencohost/issues) and [Discussions](https://github.com/plynte-labs/opencohost/discussions) to see if your question has already been answered.
3. If the app fails to start, check the log files first (see [Logs](#reading-the-logs) below).

---

## Where to get help

| Type of problem | Where to go |
|---|---|
| Bug or unexpected behavior | [GitHub Issues](https://github.com/plynte-labs/opencohost/issues) |
| Setup / configuration questions | [GitHub Discussions](https://github.com/plynte-labs/opencohost/discussions) |
| Feature ideas | [GitHub Discussions](https://github.com/plynte-labs/opencohost/discussions) — Ideas category |
| Security vulnerability | [Open a private Security Advisory](https://github.com/plynte-labs/opencohost/security/advisories/new) (do NOT open a public issue) |

### Opening a bug report

Use the **Bug Report** issue template. Include:

- OpenCohost version (shown in the title bar or `python -m opencohost --version`)
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

The default voice uses **Microsoft Edge-TTS**, a free cloud synthesis service. This requires an active internet connection. Kira's outgoing spoken text is sent to Microsoft's servers for synthesis; all other data (viewer chat, prompts, LLM context, conversation memory) stays on your machine.

If you want fully offline voice, install the `local-tts` extra:

```powershell
pip install -e ".[local-tts]"
```

Then select a Piper TTS voice in Settings. See [docs/QUICKSTART.md](docs/QUICKSTART.md) for model download instructions.

### Dependency installation errors

Use the recommended install command with the correct extras for your setup:

```powershell
# Default (Edge-TTS voice + OBS / VRAM integrations)
pip install -e ".[cloud-tts,integrations]"

# With offline Piper TTS as well
pip install -e ".[cloud-tts,integrations,local-tts]"
```

Python 3.10 or newer is required.

---

## Reading the logs

Logs are written automatically on every run. Find them at:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\OpenCohost\logs\` |
| Linux / macOS | `~/.local/share/OpenCohost/logs/` |

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
