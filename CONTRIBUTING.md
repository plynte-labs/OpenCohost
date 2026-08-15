# Contributing to OpenCohost

Thank you for your interest in contributing. This guide covers everything you need to get a working dev environment, run the test suite, and submit a pull request.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Development Setup](#development-setup)
- [Running the App](#running-the-app)
- [Running Tests](#running-tests)
- [Pre-commit Hooks](#pre-commit-hooks)
- [Code Style](#code-style)
- [Commit Convention](#commit-convention)
- [Pull Request Flow](#pull-request-flow)
- [Trust Model — What Leaves the Machine](#trust-model--what-leaves-the-machine)
- [Reporting Issues](#reporting-issues)

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python ≥ 3.10 | 3.12 recommended |
| [Ollama](https://ollama.com/) | Required for LLM features; install and run `ollama serve` |
| Git | Any recent version |
| `uv` or `pip` | Either works; examples use both |
| [Node.js](https://nodejs.org/) | Any current LTS — needed to run the Tauri front end |
| [pnpm](https://pnpm.io/) `11.5.2` | Version pinned via `packageManager` in `OpenCohost_UI/package.json`; `corepack enable` picks it up automatically |
| [Rust + Cargo](https://rustup.rs/) | No `rust-toolchain.toml` in this repo — the Rust version is unpinned, any recent stable toolchain works |
| [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) | Windows 11 ships it by default; install manually on Windows 10 |
| MSVC Build Tools (C++ workload) | Required for the `rustc` MSVC target on Windows — install via [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) |

The last five rows are only needed to build/run `OpenCohost_UI/` (`pnpm tauri:debug`) — see [Running the App](#running-the-app) below.

Windows is the primary supported platform. Linux and macOS are in progress — contributions that improve cross-platform support are welcome.

Pull at least one model before running the app:

```shell
ollama pull llama3
```

Suggested starter models from the built-in catalog: `llama3`, `qwen3:4b`, `phi4-mini`.

---

## Development Setup

Clone the repo and install in editable mode with the extras you need:

```shell
git clone --recursive https://github.com/plynte-labs/opencohost.git
cd opencohost
```

`--recursive` matters: the Tauri front end lives in its own repository and is
wired in as a submodule at `OpenCohost_UI/`. A plain `git clone` leaves that
directory empty, and every `pnpm tauri:debug` instruction below then fails on a
path that does not exist. If you already cloned without it:

```shell
git submodule update --init --recursive
```

**Recommended install for local development:**

```shell
# pip
pip install -e ".[cloud-tts,integrations,api,dev]"

# uv
uv pip install -e ".[cloud-tts,integrations,api,dev]"
```

**Available extras:**

| Extra | What it installs | When you need it |
|---|---|---|
| `cloud-tts` | `edge-tts` | Default voice synthesis (Microsoft Edge-TTS, cloud). Required unless you use `local-tts` only. |
| `local-tts` | `piper-tts`, `onnxruntime` | Fully offline voice synthesis via Piper TTS. |
| `integrations` | `obsws-python`, `nvidia-ml-py` | OBS WebSocket control, NVIDIA VRAM monitoring. |
| `api` | `fastapi`, `uvicorn` | The HTTP API — required to run the Tauri product surface (`pnpm tauri:debug`). Also required just to *collect* the test suite: 40+ test modules import fastapi at module scope. |
| `youtube-chat` | `pytchat` | Unofficial YouTube live chat. Opt-in — read [PRIVACY.md](docs/PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial) before installing. Twitch needs no extra. |
| `dev` | `pytest`, `pre-commit`, `detect-secrets` | Test runner and pre-commit hooks — required for development. |

You can combine extras:

```shell
# Both cloud and local TTS, plus integrations, API, and dev tools
pip install -e ".[cloud-tts,local-tts,integrations,api,dev]"
```

> **Note on TTS:** `cloud-tts` (Edge-TTS) is the default. If you install only `local-tts`, the app automatically uses Piper. If you install neither, TTS synthesis is unavailable. See [Trust Model](#trust-model--what-leaves-the-machine) for what data each option sends over the network.

---

## Running the App

The product is the Tauri shell. It spawns the Python backend itself — nothing
else needs to be started:

```shell
cd OpenCohost_UI
pnpm tauri:debug
```

That script also sets `OPENCOHOST_DEBUG=1`. It warns you if a backend is already
listening on 8765/8770, because Tauri will reuse that one instead of spawning a
managed child, and your debug env var will not reach it.

To run the backend on its own (headless, or against a separately served front
end), use `run-api.bat`, or:

```shell
uvicorn opencohost.api.main:app --host 127.0.0.1 --port 8765 --workers 1
```

### Legacy CustomTkinter shell

`python -m opencohost` still launches the old CTk desktop app. It is **frozen
legacy** — superseded 2026-08-13, retained but not maintained. Do not add
features to `opencohost/ui/`. Note that host flags armed by `EngineHost` (the
speech router, and therefore LLM output streaming) never engage under it, so it
is not a valid surface for runtime validation. See the "Surfaces" section of
`CLAUDE.md`.

Running from source — the normal case for every contributor — logs are written to `<repo>/logs/`: outside a frozen build, `USER_DATA_DIR` resolves to the repo root (`opencohost/config/storage.py`). Only a frozen/packaged build writes to `%APPDATA%\OpenCohost\logs\` on Windows or `~/.config/OpenCohost/logs/` on Linux/macOS.

---

## Running Tests

```shell
# Verify the full test collection
python -m pytest --collect-only -q

# Run everything
python -m pytest -q

# Core model management and inference recovery
python -m pytest tests/test_llm_tiers.py tests/test_model_panel.py tests/test_heavy_model_inference_recovery.py -q

# Health monitor and integration resilience
python -m pytest tests/test_health_monitor.py tests/test_health_integration.py tests/test_app_shell_obs_resilience.py -q

# Any single file
python -m pytest tests/test_<area>.py -q

# Skip slow tests
python -m pytest -m "not slow" -q

# Skip tests that require external services
python -m pytest -m "not integration" -q
```

The test suite uses `pytest` with `testpaths = tests` and `-v --tb=short` defaults. Five markers are defined in `pytest.ini`:

| Marker | Meaning |
|---|---|
| `slow` | Slow tests — deselect with `-m "not slow"`. |
| `integration` | Requires external services. |
| `offline` | Runs without network access. |
| `realenv` | Real Ollama + a real model. Opt-in via `OPENCOHOST_REALENV_TESTS=1`; auto-skipped otherwise. |
| `live_cloud` | Real cloud provider, real API key, real network. Opt-in via `OPENCOHOST_LIVE_CLOUD_TESTS=1`; auto-skipped otherwise. |

No network access or running Ollama instance is required to run the offline-marked tests. The two env-gated markers skip themselves, so a plain `python -m pytest -q` never hits Ollama or a cloud provider unless you set those variables.

---

## Pre-commit Hooks

Two hooks run on every `git commit`:

| Hook | What it does |
|---|---|
| `detect-secrets` | Scans staged files for secrets against a committed baseline (`.secrets.baseline`). Blocks the commit if new secrets are detected. |
| `no-abs-paths` | Blocks drive-letter absolute paths (e.g. `C:\Users\...`) from being committed in `.py`, `.md`, `.yaml`, `.toml`, and `.json` files. Use relative paths or environment variables instead. A line that genuinely needs one — a documentation example, like this row — opts out with the `path-ok` pragma.<!-- path-ok --> |

Install the hooks after your editable install:

```shell
pre-commit install
```

Run manually at any time against all files:

```shell
pre-commit run --all-files
```

If `detect-secrets` flags a false positive, update the baseline:

```shell
detect-secrets scan --baseline .secrets.baseline
git add .secrets.baseline
```

---

## Code Style

- **Python version target:** 3.10-compatible syntax (no `match`/`case`, no PEP 695 generics).
- **Formatter:** no project-wide formatter is enforced yet. Keep diffs clean — do not reformat lines unrelated to your change.
- **Type hints:** preferred on all new public functions and method signatures.
- **Comments and docstrings:** English. Explain *why*, not just what.
- **Thread safety:** the engine is multi-threaded and the host owns the boundary. In the product surface, background-to-UI updates leave the engine as events through `EngineHost` and reach the front end over HTTP/SSE — never touch UI state from an engine thread. In the legacy CTk shell the equivalent rule is `UIState` observer callbacks or `root.after()`; never call CTk widgets directly from threads.
- **No absolute paths in source:** use `settings.py` path helpers (`USER_DATA_DIR`, `LOGS_DIR`, etc.) for any file I/O. The `no-abs-paths` hook enforces this.
- **Raw chat is contained, not redacted.** Viewer usernames and raw chat text must never be written to logs or handed to a third-party service. They *do* reach the local LLM: the Smart Aggregator compacts chat into an intent summary when one is available, but the highlighted message is passed verbatim as `{user}: {text}`, and the fallback context is a raw `- {user}: {text}` list (`smart_aggregator/chat_reaction.py`). Every such path must route the text through `wrap_untrusted_chat()` (`smart_aggregator/kira_agenda_controller.py`), which fences it in read-only data delimiters and collapses any `===` run in the body so viewer text cannot forge its way out of the fence. If you add a path that puts chat in a prompt, wrap it. Remember that enabling a cloud LLM provider sends that same prompt off the machine — see [Trust Model](#trust-model--what-leaves-the-machine).

---

## Commit Convention

This project uses [Conventional Commits](https://www.conventionalcommits.org/).

```
<type>(<scope>): <short summary>
```

Common types:

| Type | Use for |
|---|---|
| `feat` | New user-visible feature |
| `fix` | Bug fix |
| `refactor` | Code change with no behavior change |
| `test` | Adding or updating tests |
| `docs` | Documentation only |
| `chore` | Build, tooling, dependency updates |
| `perf` | Performance improvement |

Examples:

```
feat(tts): add piper fallback when edge-tts is unavailable
fix(health-monitor): handle connection refused on ollama startup
test(llm-tiers): cover tier downgrade on VRAM pressure
docs(contributing): add pre-commit setup steps
```

Keep the summary line under 72 characters. Add a body paragraph if the change needs context that the summary cannot convey.

---

## Pull Request Flow

1. **Fork** the repo and create a branch from `master`:
   ```shell
   git checkout -b feat/your-feature-name
   ```

2. **Make your changes.** Keep commits focused — one logical change per commit.

3. **Run tests** before pushing:
   ```shell
   python -m pytest -q
   ```

4. **Push your branch** and open a pull request against `master` on `plynte-labs/opencohost`.

5. **PR description:** explain what the change does, why it is needed, and how you tested it. Reference any related issues with `Closes #<number>` if applicable.

6. **Review:** a maintainer will review your PR. Please respond to feedback within a reasonable time. If the review requests changes, push new commits — do not force-push over existing review history.

7. **Merge:** the maintainer merges after approval. Squash merging is used to keep the `master` history linear.

**Branch naming:**

| Prefix | Use for |
|---|---|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `docs/` | Documentation only |
| `refactor/` | Refactoring |
| `test/` | Test additions or corrections |
| `chore/` | Tooling, dependencies |

---

## Trust Model — What Leaves the Machine

OpenCohost is designed to keep viewer data local. Here is an honest summary of every network connection the application makes. OpenCohost also *listens* on one port — the FastAPI engine surface on `127.0.0.1:8765` — which is covered in [SECURITY.md](SECURITY.md#local-attack-surface) rather than here.

**Outbound (data leaves your machine):**

| Service | What is sent | When |
|---|---|---|
| **Microsoft Edge-TTS** (cloud) | Kira's synthesized spoken text only — sentence-sized fragments of the AI's generated response. No viewer chat, no usernames, no prompts, no conversation history. | Every time Kira speaks, when `cloud-tts` is installed and `local-tts` is not set as exclusive. |
| **An OpenAI-compatible LLM provider** (cloud) — **opt-in, off by default** | The **entire** message array that would otherwise go to Ollama: system prompt, active persona, saved memorias, personalization block, and the viewer-chat context that survived filtering — usernames included. Strictly more than Edge-TTS receives. POSTed to `{base_url}/chat/completions` (`core/providers/cloud/cloud_llm_client.py`). | Only when you set `active_provider` to something other than `"local"`. It defaults to `"local"`, and an absent or corrupt `config/llm_provider.json` resolves back to local-only. Retention is the provider's policy, not ours. |

**Local loopback only (nothing leaves the machine):**

| Service | What is sent |
|---|---|
| **Ollama** `127.0.0.1:11434` | Kira's system prompt + conversation history + viewer-chat context. The Smart Aggregator compacts chat into an intent summary when one is available, but raw usernames and message text still reach the model — the highlighted message goes in verbatim as `{user}: {text}`, and the fallback context is a raw `- {user}: {text}` list. All of it is fenced by `wrap_untrusted_chat()` first. Containment, not redaction. **This is also exactly the payload that leaves the machine if you enable a cloud provider (see above).** |
| **OBS WebSocket** `localhost:4455` | Avatar state name (`"idle"`, `"speaking"`) and local image paths only. |
| **Piper TTS server** `127.0.0.1` | Kira's sentence fragments — same as Edge-TTS, but processed entirely on your machine. |

**Read-only inbound (OpenCohost receives data but does not upload viewer content):**

| Service | What is transmitted |
|---|---|
| **Twitch IRC** `irc.chat.twitch.tv:6667` | Default platform. OpenCohost sends an anonymous guest nick, a channel JOIN, and PONG keepalives. No viewer data is transmitted. |
| **YouTube** via `pytchat` (opt-in) | Not installed by default. OpenCohost sends a YouTube video ID to `pytchat`, which polls YouTube's *unofficial* live-chat endpoint — not the official Data API, and not permitted by YouTube's Terms of Service. OpenCohost receives messages; it does not upload chat or user data. See [PRIVACY.md](docs/PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial). |

**Fully offline (no network, ever):**

- Conversation memory (`historial` deque and `MemoryDigest`) is RAM-only and is never written to disk.
- All config, profile, and log files are written to the local user data directory only.
- There is no telemetry, analytics reporting, crash uploading, or usage tracking of any kind.

**To avoid all cloud TTS:** install with `local-tts` instead of (or in addition to) `cloud-tts`. When the app's "Local TTS only" option is enabled, Edge-TTS is never called.

---

## Reporting Issues

Please open an issue at [github.com/plynte-labs/opencohost/issues](https://github.com/plynte-labs/opencohost/issues).

Include:
- Your OS and Python version.
- Ollama version and the model you were using.
- The relevant log excerpt from `<repo>/logs/` (running from source — the normal case) or `%APPDATA%\OpenCohost\logs\` / `~/.config/OpenCohost/logs/` (frozen build only).
- Steps to reproduce.

If you believe you have found a security issue, please do **not** open a public issue. Contact the maintainers directly via the email listed on the GitHub profile.
