# ADR-067: Remote Voice Gateway (Talk to Kira from a Phone Browser)

**Date**: 2026-10-20
**Status**: Accepted 2026-10-20 (owner approved the track and decided voice in, voice out and the publishing exception, see "Owner decisions"). Implementation: T1 to T4 committed (`5bf761a`, `9fb6df4`, `16cf2ee`, `743af00`); T5 (desktop panel) and T6 (this document) follow.
**Feature document**: `odd/tasks/remote-voice-gateway-20261020.md`
**Related**: `docs/guides/remote-phone-access.md` (owner setup guide), the API auth rules in `opencohost/api/auth.py`

**Language**: English. ADR-065 is English and ADR-066 is Spanish, so there is no fixed convention; English was kept because this ADR is mostly security reasoning that is quoted next to code.

---

## Context

The product surface is the Tauri desktop app over a FastAPI host bound to `127.0.0.1`. The owner wants to talk to Kira from a phone browser, away from the PC, through their own domain.

The main API cannot be exposed:

- `API_AUTH_ENFORCED` defaults off, and GET requests are never authenticated.
- Dangerous routes exist on it, for example `/api/system/shutdown`, `/api/memoria/purge` and `/api/speech/say`.

Microphone capture (WhisperLive) and TTS playback both run on the PC, so a phone cannot use them directly.

## Decision

Add a **second, separate FastAPI app** (the gateway) in `opencohost/gateway/`.

- It listens on its own loopback port, `127.0.0.1:8780` by default (`OPENCOHOST_REMOTE_GATEWAY_PORT` overrides it).
- It is **opt-in**: it starts only when `OPENCOHOST_REMOTE_GATEWAY=1` (`config/settings.py`). With the flag off, nothing listens on 8780 and the main app is unchanged.
- It runs in a daemon thread started by the main lifespan after the dispatcher exists, and is stopped before `host.stop()`.
- It does not inherit the main app's routes, middleware, CORS or audit stack.
- It serves an **allowlist** and nothing else; every other path returns 404:

| Route | Auth | Purpose |
|---|---|---|
| `GET /` and `GET /phone.js` | none | the phone page |
| `GET /status` | none | minimal readiness |
| `POST /pair` | one-time code | exchange a code for a device token |
| `POST /turn` | device token | send text as a `direct` turn |
| `GET /reply?after=` | device token | poll the final reply |
| `GET /whoami` | device token | token check |

> **Note (2026-10-20):** The avatar routes (`GET /avatar.js`, `GET /animations/{clip}.vrma` and the protected `GET /avatar/model`) and the `img-src` `blob:` CSP change are recorded in ADR-068.

> **Note (2026-10-20, phone messages in the desktop conversation):** the owner decided that messages sent from the phone are recorded, with their text, in the desktop conversation under the category "Mensaje remoto" / "Remote message". The gateway appends each ACCEPTED `/turn` (never a refused, rate limited or invalid one) to an in-memory `RemoteTurnFeed` (`opencohost/gateway/feed.py`: at most 50 entries, entries older than 30 minutes dropped, monotonically increasing `seq`). The operator reads it through `GET /api/remote/messages?after=<seq>` on the MAIN API (port 8765, operator token, 409 `gateway_disabled` when the gateway is off); the gateway app on 8780 does not expose it. R8 boundary: the text lives only in process memory and reaches only the operator's own UI. It is never logged, persisted, written to the event log or metrics, or put in a prompt, and it is gone on restart. The reply `source`/`origin` stay `direct`, so `/reply` matching is unchanged. Gateway modules now log through `OpenCohost.gateway`, so their lines reach the log file.

- The Cloudflare tunnel targets **only** `http://127.0.0.1:8780`. Never 8765 or 8770.
- Pairing is minted on the main API (`/api/remote/*`, loopback, operator-only) and used by the desktop "Link phone" panel.

A turn enters the engine as `source="direct"` through the shared helper `submit_direct_turn` (`opencohost/api/direct_turn.py`, also used by `chat.py`). It therefore keeps memory capture and injection, owner-question priority and the guardrails. The reply returned to the phone is the final post-guardrail text from `ChatReplySink`.

### Owner decisions (2026-10-20)

- **Voice in: browser Web Speech API.** Fast to build. The tradeoff is accepted: on Chrome for Android the audio is sent to Google's speech service, so this input is **not local-first**. The text then travels to the PC.
- **Voice out: the phone's own `speechSynthesis` voices**, not Kira's Piper/Edge voice. The PC TTS pipeline is untouched.
- **Kira still speaks on the PC** for remote turns, as she does for any direct turn. Muting the PC would touch the speech engine, so it is out of scope.
- **Exception to "nothing is published to a remote"** in the project `CLAUDE.md`: the owner may expose ONLY this gateway on their own domain through Cloudflare Tunnel with Cloudflare Access. Everything else in that rule stands (no git push, PR, tag, release or build upload). Agents never run `cloudflared`, never create or change tunnels and never contact the domain.

## Security model

### One-time pairing code

- Held in memory only, never written to disk.
- 10 characters from an alphabet without look-alike glyphs, about 49 bits. Case and separators are ignored when entered.
- Valid for 120 seconds. Only one code is live at a time; minting a new one replaces it.
- Single use: consumed under a lock on the first successful exchange.
- Burned after 5 bad attempts.
- `POST /pair` has a 1024-byte body cap and returns one generic 401 for every failure, so a wrong, expired or used code is indistinguishable.

### Device token

- `secrets.token_urlsafe(32)` (256 bits), returned once with `Cache-Control: no-store`.
- The PC stores only its SHA-256 in `config/remote_devices.json` (atomic write, lock) with device id, name, created and last-used times and a revoked flag. The file never contains a raw token.
- Comparison uses `hmac.compare_digest` with no early exit.
- `last_used_at` is written at most once per 60 seconds.
- A revoked or unknown token gets 401 on `/turn` and `/reply`.

### Operator-only management

`/api/remote/*` (mint code, list devices, revoke) requires the operator token for any method and is **always enforced**, regardless of `API_AUTH_ENFORCED`. This is rule 1b in `opencohost/api/auth.py`. Minting a code admits a phone to an internet-facing app, so the warn-only mode does not apply.

### Turn limits

- 16 KB body cap and the same text rules as the desktop chat.
- One turn in flight and 20 turns per minute per device.
- Failures return status codes (400, 409, 429) only, never internal detail.

### Page hardening

The phone page is served with `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, plus `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`. The JavaScript is a separate file, so there are no inline scripts. The pairing code travels in the URL fragment, which is not sent to the server or to referrers, and the page removes it from the address bar after use.

### Logging

Gateway logs carry device ids, counts and outcome codes only. They never contain turn text, codes or tokens.

### Outer layer

Cloudflare Access (self-hosted application, email one-time PIN, owner's email only) in front of the hostname is the intended outer gate. The gateway's own checks are a second layer, not a replacement.

## Residual risks (accepted for v0)

From the T2 and T3 notes:

1. Anyone who can reach `/pair` can burn the live code with 5 bad guesses (a denial of pairing, not a bypass). Cloudflare Access in front mitigates this.
2. There is no per-IP rate limit on `/pair`. Access mitigates this too.
3. Manual edits to `remote_devices.json` need a restart to take effect.
4. There is no device cap.
5. A failed revoke write is lost on restart even though the response said revoked.
6. **Turn-correlation race (T3).** The engine has no turn-to-command correlation. If the owner types a direct turn on the PC at the same moment as a phone turn, the phone can receive the desktop turn's reply. The text is the owner's own, so this is a correctness issue, not a privilege one. It is documented in `turns.py`.
7. Web Speech on Android Chrome sends audio to Google (owner-accepted, see above).
8. Real browser behaviour (Web Speech, `speechSynthesis`, iOS unlock, hold-to-talk, 360 px layout) is not covered by automated tests. The manual checklist is in the owner guide.

## Alternatives rejected

- **Expose the main API.** Rejected: auth is off by default, GETs are unauthenticated and destructive routes exist. A narrow separate app is far smaller to audit.
- **Tailscale only.** Rejected for this track: it needs the client on every phone and does not give the browser a normal HTTPS origin for the microphone without extra setup. It remains a valid owner-side alternative that needs no code change.
- **Stream audio to the PC (option B).** Would keep recognition local, but needs a streaming transport, WhisperLive session handling for a remote client and much more attack surface. Deferred.
- **Kira's own voice on the phone.** Would mean shipping synthesized audio to the phone or running TTS there. Out of scope; the phone's voices are used instead.

## Consequences

- Positive: the owner can talk to Kira remotely with no change to the engine, and with the flag off the product is byte-identical.
- Negative: a new internet-facing component to maintain, and a non-local speech input path on Android.
- Remote turns use the `direct` label, so they are not distinguishable from desktop-typed turns in analytics.

### Follow-ups

- A `remote` source label (one change across every source allowlist).
- A QR code for pairing (needs a dependency decision; none exists today).
- Muting PC TTS for remote turns.
- Turn-to-command correlation, which also removes the race above.
- Streaming partial replies and interrupting Kira from the phone.
