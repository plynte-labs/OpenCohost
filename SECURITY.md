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
| Latest release (`main`/`master`) | Yes |
| Older releases | No — please upgrade |

---

## Security Posture

### Local-first, but not fully offline

OpenCohost is designed to keep **your data on your machine**. Specifically:

- Viewer chat messages, LLM prompts, conversation context, and memory **never leave your machine**.
- Your locally installed Ollama instance handles all LLM inference locally.
- **Kira's outgoing spoken text is sent to Microsoft Edge-TTS** (a cloud service) for voice synthesis. This is the default TTS engine. If you install the `local-tts` extra (Piper TTS), you can switch to a fully offline voice and eliminate this outbound request.

There is **no telemetry, no analytics, and no crash reporting** built into OpenCohost. The application does not phone home.

### Viewer chat is untrusted input

Viewer chat messages (from platforms such as YouTube Live) are treated as **untrusted external data** throughout the pipeline. They are processed by the LLM co-host under a system prompt that includes anti-injection guardrails. Do not assume the LLM will resist every adversarial prompt, especially when running smaller local models. OpenCohost is supervised software — a human operator is expected to be present at all times.

### Local attack surface

Because OpenCohost is a local desktop application:

- The HTTP server used to communicate with Ollama listens on `localhost` only (`127.0.0.1:11434`).
- OBS WebSocket connections are made to a locally configured host (default `localhost`).
- No inbound network ports are opened by OpenCohost itself.
- Settings and logs are stored in the user's application-data directory; no data is written outside of that directory or the project folder.

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
