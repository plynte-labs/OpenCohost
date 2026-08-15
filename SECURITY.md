# Security Policy

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities privately via [GitHub Security Advisories](https://github.com/plynte-labs/opencohost/security/advisories/new).
If you are unable to use the advisory form, contact the maintainers through the private contact channel listed on the repository's Security tab.

We will acknowledge your report within **5 business days** and aim to provide an initial assessment within **10 business days**. Please include:

- A clear description of the vulnerability and its potential impact
- Steps to reproduce or a minimal proof-of-concept (no weaponized exploit code, please)
- The version or commit you tested against
- Any suggested mitigations you have already considered

We will coordinate a fix and disclosure timeline with you before publishing anything publicly.

---

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release (`master`) | Yes |
| Older releases | No — please upgrade |

---

## Security Posture

### Local-first, but not fully offline

OpenCohost is designed to keep **your data on your machine**. Specifically:

- **Under the shipped defaults**, viewer chat messages, LLM prompts, conversation context, and memory stay on your machine. Your locally installed Ollama instance handles all inference over loopback. There is exactly one opt-in exception, below.
- **Kira's outgoing spoken text is sent to Microsoft Edge-TTS** (a cloud service) for voice synthesis. This is the default TTS engine. If you install the `local-tts` extra (Piper TTS), you can switch to a fully offline voice and eliminate this outbound request.
- **A cloud LLM provider is the opt-in exception, and it is off by default.** `active_provider` defaults to `"local"` (`opencohost/config/llm_provider.py`), and an absent, unreadable, or corrupt provider config all resolve back to local-only. If you deliberately point OpenCohost at an OpenAI-compatible endpoint, the **complete prompt** — system prompt, active persona, saved memorias, personalization block, **and the filtered viewer-chat context** — is sent to that endpoint on every turn. That is strictly more than Edge-TTS ever receives, and the provider's retention and training policy, not this one, governs what happens to it. See [PRIVACY.md](docs/PRIVACY.md#optional-cloud-llm-providers-opt-in).

So: one cloud destination by default (Edge-TTS), and a second only if you turn it on.

There is **no telemetry, no analytics, and no crash reporting** built into OpenCohost. The application does not phone home.

### Viewer chat is untrusted input

Viewer chat messages (from Twitch, the supported default platform — or from YouTube's unofficial endpoint if you opt into it) are treated as **untrusted external data** throughout the pipeline. They are processed by the LLM co-host under a system prompt that includes anti-injection guardrails. Do not assume the LLM will resist every adversarial prompt, especially when running smaller local models. OpenCohost is supervised software — a human operator is expected to be present at all times.

### Local attack surface

**OpenCohost opens an inbound port.** The product surface is a local HTTP server: the Tauri shell spawns `uvicorn opencohost.api.main:app --host 127.0.0.1 --port 8765 --workers 1` (`OpenCohost_UI/src-tauri/src/backend.rs`) and drives Kira's engine through it. Treat that port as part of the attack surface:

- **It binds loopback only (`127.0.0.1:8765`) in every documented run form.** Nothing outside the machine can reach it as shipped.
- **It is not authenticated by default.** Bearer-token auth exists (`opencohost/api/auth.py`), but enforcement for mutating `/api/*` calls is behind `OPENCOHOST_API_AUTH`, which is **off** by default, and read-only `GET` requests are open in v1. The one exception is `GET /api/stream/chat-live/messages`, which serves raw viewer chat and requires the operator token unconditionally. In practice, any process running as any user on the same machine can drive the engine.
- **Binding `--host 0.0.0.0` exposes that control surface to your LAN.** Do not do it. CORS only restricts browser callers — it does nothing against `curl` or a script hitting the port directly. If you need remote access, put an authenticating proxy in front of it. This warning is also carried in `README.md` and in the `opencohost/api/main.py` module docstring.
- Outbound local connections: the Ollama client talks to `127.0.0.1:11434`, and OBS WebSocket connects to a locally configured host (default `localhost:4455`).
- Settings and logs are stored under the user data directory — the repo root when running from source, the platform application-data directory in a frozen build. No data is written outside of that directory.

### Credentials and secrets

The detect-secrets pre-commit hook (`Yelp/detect-secrets`) is enforced on this repository to prevent accidental credential commits. If you are contributing, run `pre-commit install` after setting up the dev environment.

Do not store API keys, tokens, or passwords in settings files or source code. If you discover a committed secret in the repository history, please report it privately using the channel above.

---

## Out of Scope

The following are **not** considered security vulnerabilities for the purposes of this policy:

- Attacks that require physical access to the machine running OpenCohost
- Vulnerabilities in third-party dependencies (Ollama, Edge-TTS, OBS) — report those upstream
- Self-XSS or social-engineering attacks
- Denial-of-service against the local Ollama server by a process already running on the same machine
- Issues only reproducible on unsupported versions

---

## Disclosure Policy

We follow a **coordinated disclosure** model. We ask reporters to keep vulnerability details private until a fix is released and both parties agree on a disclosure date. We will credit reporters in release notes unless they prefer to remain anonymous.
