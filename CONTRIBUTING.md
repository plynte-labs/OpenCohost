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
git clone https://github.com/plynte-labs/opencohost.git
cd opencohost
```

**Recommended install for local development:**

```shell
# pip
pip install -e ".[cloud-tts,integrations,dev]"

# uv
uv pip install -e ".[cloud-tts,integrations,dev]"
```

**Available extras:**

| Extra | What it installs | When you need it |
|---|---|---|
| `cloud-tts` | `edge-tts` | Default voice synthesis (Microsoft Edge-TTS, cloud). Required unless you use `local-tts` only. |
| `local-tts` | `piper-tts`, `onnxruntime` | Fully offline voice synthesis via Piper TTS. |
| `integrations` | `obsws-python`, `nvidia-ml-py` | OBS WebSocket control, NVIDIA VRAM monitoring. |
| `youtube-chat` | `pytchat` | Unofficial YouTube live chat. Opt-in — read [PRIVACY.md](docs/PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial) before installing. Twitch needs no extra. |
| `dev` | `pytest`, `pre-commit`, `detect-secrets` | Test runner and pre-commit hooks — required for development. |

You can combine extras:

```shell
# Both cloud and local TTS, plus integrations and dev tools
pip install -e ".[cloud-tts,local-tts,integrations,dev]"
```

> **Note on TTS:** `cloud-tts` (Edge-TTS) is the default. If you install only `local-tts`, the app automatically uses Piper. If you install neither, TTS synthesis is unavailable. See [Trust Model](#trust-model--what-leaves-the-machine) for what data each option sends over the network.

---

## Running the App

```shell
python -m opencohost
```

If the package is installed into an environment whose `Scripts/` directory is on your `PATH`, the registered GUI script also works:

```shell
opencohost
```

Enable verbose logging with:

```shell
# Windows PowerShell
$env:OPENCOHOST_DEBUG = "1"; python -m opencohost

# bash / zsh
OPENCOHOST_DEBUG=1 python -m opencohost
```

Log files are written to `%APPDATA%\OpenCohost\logs\` on Windows and `~/.local/share/OpenCohost/logs/` on Linux/macOS.

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

The test suite uses `pytest` with `testpaths = tests`, `-v --tb=short` defaults, and the markers `slow`, `integration`, and `offline`. No network access or running Ollama instance is required to run the offline-marked tests.

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
- **Thread safety:** the UI runs on CustomTkinter's main thread. All background-to-UI updates MUST go through `UIState` observer callbacks or `root.after()`. Do not call CTk widgets directly from threads.
- **No absolute paths in source:** use `settings.py` path helpers (`USER_DATA_DIR`, `LOGS_DIR`, etc.) for any file I/O. The `no-abs-paths` hook enforces this.
- **No raw chat in logs or prompts:** viewer usernames and raw chat text must never be passed to external services. The Smart Aggregator compacts them locally into an intent summary before anything reaches the LLM.

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

OpenCohost is designed to keep viewer data local. Here is an honest summary of every network connection the application makes:

**Outbound (data leaves your machine):**

| Service | What is sent | When |
|---|---|---|
| **Microsoft Edge-TTS** (cloud) | Kira's synthesized spoken text only — sentence-sized fragments of the AI's generated response. No viewer chat, no usernames, no prompts, no conversation history. | Every time Kira speaks, when `cloud-tts` is installed and `local-tts` is not set as exclusive. |

**Local loopback only (nothing leaves the machine):**

| Service | What is sent |
|---|---|
| **Ollama** `127.0.0.1:11434` | Kira's system prompt + conversation history + a compacted intent summary produced by the Smart Aggregator from viewer chat. Viewer usernames and raw chat text are never forwarded to Ollama directly. |
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
- The relevant log excerpt from `%APPDATA%\OpenCohost\logs\` (Windows) or `~/.local/share/OpenCohost/logs/` (Linux/macOS).
- Steps to reproduce.

If you believe you have found a security issue, please do **not** open a public issue. Contact the maintainers directly via the email listed on the GitHub profile.
