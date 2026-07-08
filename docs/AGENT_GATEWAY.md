# OpenCohost — Agent Context Gateway

External contract for AI coding agents (Claude Code, Codex, Gemini CLI, or any
tool that can shell out to `curl`) that want to feed context to Kira. This
document describes `/api/agent/*` — the ONLY HTTP surface external agents are
meant to call. Every other mutating endpoint in `docs/api-reference.md`
belongs to the operator's own Tauri app.

Track: `agent_context_gateway_20260705`. Design source of truth:
`conductor/tracks/agent_context_gateway_20260705/design.md`. Backing code:
`opencohost/api/auth.py`, `opencohost/api/main.py` (search `Agent gateway`),
`opencohost/api/models.py`, `opencohost/core/topic_inbox.py`,
`opencohost/core/editorial_cards.py`, `opencohost/core/agent_notices.py`.

## 1. Philosophy

**Propose, don't command.** An agent can never make Kira say or do anything
directly. Every write an agent makes lands in a human-gated inbox
(`topic_inbox`, `editorial_cards` DRAFT, `agent_notices`) — nothing is queued,
armed, or spoken until the operator reviews it in the app UI (or, for cards,
the CLI). There is no `/api/agent/chat` and no way for an agent to reach
`process_context` — see ADR-1 in `design.md` for why that surface is
deliberately absent in v1. Agents also cannot write to memoria, edit profiles,
or touch conversation history — those subsystems only ever see the streamer's
own `direct`/`ptt` sources (ADR-2).

## 2. Auth

Two static bearer tokens — `operator` and `agent` — are minted once at API
startup into a JSON file. **Use the `agent` token for everything in this
document.** The operator token is a strict superset (it also works on every
`/api/agent/*` route) — external agents should never be handed it, since
leaking it would grant full operator surface (create/delete profiles, connect
stream sources, etc.), not just the propose-only surface described here.

**Token file location** (`opencohost/config/settings.API_TOKENS_FILE` =
`USER_DATA_DIR/config/api_tokens.json`):

| Environment | Path |
|---|---|
| Packaged/frozen build (Windows) | `%APPDATA%\OpenCohost\config\api_tokens.json` |
| Dev checkout (`run-api.bat`, non-frozen Python) | `<repo root>\config\api_tokens.json` |

`USER_DATA_DIR` only resolves to `%APPDATA%` when `sys.frozen` is true (a
PyInstaller build, `opencohost/config/storage.py::get_user_data_dir`). Running
the backend straight from a dev interpreter (as `run-api.bat` does) resolves
`USER_DATA_DIR` to the repo root instead — read the token from
`config/api_tokens.json` at the repo root, not `%APPDATA%`, until a packaged
build exists.

File shape:

```json
{"version": 1, "operator": "<token>", "agent": "<token>"}
```

Send it as `Authorization: Bearer <agent-token>` on every request.

**Rotation**: delete the token file and restart the backend — a fresh pair is
minted on the next `lifespan()` start. There is no in-place rotation endpoint
in v1; anything holding the old token gets `401` afterward.

**Warn-only flag (`OPENCOHOST_API_AUTH`)**: this only affects the OPERATOR
surface (every non-`/api/agent/*` mutating endpoint). It defaults **OFF**, so
today's Tauri app and any `run-api.bat` + `curl` testing keep working with no
`Authorization` header at all — a missing/invalid operator token just logs a
warning and the request still succeeds (owner decision D2). `/api/agent/*` is
different: it is **always** enforced regardless of this flag, since it is a
brand-new surface nothing currently calls without a token. Setting
`OPENCOHOST_API_AUTH=1` before starting the backend switches the operator
surface from warn-only to `401`/`403` — do this only after every operator
client sends the token; it does not change agent-surface behavior at all.

## 3. Endpoints

Base URL: same host/port as `docs/api-reference.md` (`run-api.bat` prints the
resolved port, `8765` or `8770` fallback). All bodies/responses are JSON.

Shipped surface is **six routes** under `/api/agent/` — four an agent token
can call, plus two operator-only notice-management routes (the design's
outline undercounted these as "four routes"; documented here against the
actual code).

Common error shapes: `401 {"detail": "missing bearer token"}` or
`{"detail": "invalid token"}`; `429 {"detail": "rate_limited"}`
(60 mutating requests/minute per token) or `{"detail": "<cap message>"}` when
a store's own capacity cap is hit; `503 {"detail": "<subsystem>_unavailable"}`
when the SQLite store itself errors (rare — not part of normal usage, listed
for completeness).

**`422` has two distinct shapes, not one** — no custom
`RequestValidationError` handler is registered on this app
(`opencohost/api/main.py`), so which shape you get depends on where the
rejection happens:

- **Store-level validation** (the field runs correctly through the wire
  model and fails a store/dataclass rule — e.g. an oversized field, a
  code/HTML match, a malformed `expires_at`): `{"detail": "<validation
  message>"}`, a plain string, surfaced verbatim from the underlying store.
- **Model-level validation** (the request never reaches the handler because
  a Pydantic field constraint fails — e.g. a blank/whitespace `agent`, a
  missing required field, or a wrong JSON type): FastAPI's default
  validation-error body, where `detail` is a **list** of error objects:
  `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`.

The "blank agent" case in the next section is a model-level failure (`agent`
is a Pydantic `AgentName` constraint in `opencohost/api/models.py`, checked
before any store is reached), so it returns the list shape, not the string
shape. A client must handle both.

### POST /api/agent/topics

Propose a stream topic for human review. Maps 1:1 to
`TopicInboxStore.propose(title, angle, tags, source=agent)`.

```
Request:  {"agent": "claude-code", "title": str, "angle": str = "", "tags": [str] = []}
Response: {"id": "ti_...", "status": "proposed", "deduped": bool}
```

Caps: `title` ≤ 120 chars, `angle` ≤ 600 chars, `tags` ≤ 8 items × ≤ 40 chars
each, `agent` ≤ 80 chars. `title`/`angle` are rejected (`422`) if they match
the code/HTML filter — see the "word-boundary caveat" in
`docs/EDITORIAL_CARDS_CLI.md`, which applies here verbatim. Pending-inbox cap
is 30 rows (`429` once reached).

Status codes: `200`, `401`, `422` (blank `agent`, empty/oversized fields, or
code/HTML match), `429` (rate limit or 30-pending cap).

### GET /api/agent/status

Read-only counts, any valid token (agent or operator), never rate limited.

```
Response: {"topics_pending": int, "cards": {"draft": int, "armed": int, ...}, "notices_undismissed": int}
```

Status codes: `200`, `401`.

### POST /api/agent/cards

Upsert an editorial cue card. Maps to `EditorialCardStore.upsert` with the
`EditorialCard` dataclass's own validation.

```
Request:  {"agent": str, "topic": str, "summary": str, "streamer_take": str,
           "counterpoints": [str] = [], "discussion_hooks": [str] = [],
           "triggers": [str] = [], "expires_at": "<iso8601>" | null}
Response: {"id": "ec_...", "topic_slug": str, "status": "draft" | "used" | "expired", "demoted": bool}
```

`status` is the card's **current** status after the upsert, not always
`"draft"`: a brand-new card is `"draft"`, but `upsert` preserves whatever
status an existing card already had (`opencohost/core/editorial_cards.py`).
Demotion only forces `ARMED`/`ACTIVE` cards back to `DRAFT` (see section 6) —
it does not touch `USED` or `EXPIRED`. So re-upserting a topic whose card was
already fired by Kira (`USED`) or has passed its `expires_at` (`EXPIRED`)
returns `200` with that status echoed back and `demoted: false`. Treat
`status` as one of `draft | armed | active | used | expired`, not a literal.

Caps: `topic` ≤ 120 chars, `summary` ≤ 1200, `streamer_take` ≤ 800; list
fields ≤ 240 chars per item, **≤ 8 items kept per field** — items beyond the
8th are **silently truncated**, not rejected (`EditorialCard._clean_list`
returns `cleaned[:MAX_ITEMS]`, `opencohost/core/editorial_cards.py`). This is
the opposite of `POST /api/agent/topics`, where a `tags` list over 8 items IS
a `422` (`opencohost/core/topic_inbox.py`). A 9th `counterpoint` or
`discussion_hook` gets `200` and is dropped with no signal in the response —
sort by priority if you send more than 8. `agent` ≤ 80 chars. Cards do **not**
run the code/HTML word filter that topics/notices use — only length caps
apply (the dataclass also rejects raw pasted chat/page dumps at the field
level, but that check is not reachable from this wire model, which has no
`raw_chat`/`raw_page` field). `expires_at` must parse as ISO-8601 or the
request is rejected. No count cap — a card is a slug upsert, never a new row
per call, so only the rate limiter applies.

Status codes: `200`, `401`, `422` (field validation or a malformed
`expires_at`), `429` (rate limit only). See section 6 for `demoted`.

### POST /api/agent/notice

Leave a short note for the operator. New store, same shape as the topic
inbox: `AgentNoticeStore.propose(text, source=agent)`.

```
Request:  {"agent": str, "text": str}
Response: {"id": "an_...", "deduped": bool}
```

Caps: `text` ≤ 280 chars, `agent` ≤ 80 chars, same code/HTML word filter as
topics. Undismissed-board cap is 20 (`429` once reached).

Status codes: `200`, `401`, `422`, `429` (rate limit or 20-undismissed cap).

### GET /api/agent/notices (operator-only)

```
Response: {"notices": [{"id": str, "text": str, "source": str, "created_at": str}]}
```

Only rows that pass read-time validation are returned (hostile rows written
directly to SQLite stay quarantined, never surfaced).

Status codes: `200`, `401` (no/invalid token), `403` (a valid **agent** token
hit this route — an agent can create notices but never read the board back).

### POST /api/agent/notices/{notice_id}/dismiss (operator-only)

```
Response: {"dismissed": bool}
```

Status codes: `200`, `401`, `403` (agent token), `404` (unknown id or already
dismissed), `429` (rate limit).

## 4. Idempotency

No `Idempotency-Key` header is needed anywhere in this gateway — each store's
own dedupe rule IS the idempotency contract:

- **Topics and cards** dedupe by **slug** (normalized title/topic).
  Re-sending an IDENTICAL `title`/`topic` always matches the same row in
  both stores. Normalization differs per store: topic slugs strip ALL
  accents (case/emoji-insensitive too), while card slugs only fold the
  Spanish set `áéíóúüñ` — a card re-sent with other diacritics changed
  (e.g. `São Paulo` vs `Sao Paulo`) creates a separate card, so keep card
  `topic` strings byte-stable across retries. `POST /api/agent/topics` reports this via `deduped: true`;
  `POST /api/agent/cards` always upserts silently (the response's `demoted`
  flag is the signal to watch there, not a `deduped` field — cards have no
  count cap to protect, so silent upsert is safe).
- **Notices** dedupe by the exact pair **(source, text)** among still-
  undismissed rows — repeat POSTs with the same `agent` and `text` return the
  existing row's id and `deduped: true`, even while the 20-row cap is full.

Retry freely on network failure; a retried POST never creates a duplicate —
**with one caveat**: `POST /api/agent/topics` skips dedupe entirely when the
`title` normalizes to an **empty slug** (a punctuation-only or emoji-only
title, e.g. `"🔥🔥🔥"` or `"---"`). `TopicInboxStore.propose` only matches
existing rows `if slug:` (`opencohost/core/topic_inbox.py`) — an empty slug
never matches anything, so every retry of such a title inserts a **new**
pending row until the 30-row pending cap is hit. Cards can't hit this (an
empty `topic_slug` is rejected with `422 "topic_slug is required"` at
creation time instead), and notices dedupe by exact `(source, text)` pair,
not slug, so they are unaffected. Give topic titles at least one
alphanumeric character to stay retry-safe.

## 5. Provenance

Every agent write requires a non-blank `agent` name (≤ 80 chars) — the API
model (`AgentName` in `opencohost/api/models.py`) rejects a blank/whitespace
value with `422` before it ever reaches a store. That name is stored as-is:

- `topic_inbox.source` — the app's inbox panel already renders 🤖 + source
  for any non-empty source (`topic_inbox_bridge.py`, `INBOX_SOURCE_TAG`).
- `editorial_cards.origin` — new column; `''` means operator (CLI/UI never
  set it), a non-empty value means an agent wrote or last-touched the card.
- `agent_notices.source` — shown next to every notice in the operator surface.

There is one shared `agent` token, so provenance is honest-by-convention, not
cryptographically distinct per agent — accepted risk (design "Risks / edge
cases"): every agent write is human-gated regardless of which agent claimed
it.

## 6. What happens after propose

- **Topics**: a proposal sits in `topic_inbox` as `status="proposed"`. The
  operator approves or discards it **only in the app UI**
  (`topic_inbox.py` re-validates at approve time); there is no
  `POST /api/agent/topics/{id}/approve` and never will be by design (owner
  decision D1) — `POST /api/agenda/topic` (auto-approve + queue) stays
  operator-token-only and is never reachable from the agent surface.
- **Cards**: a new card always lands `DRAFT`. Kira never sees a `DRAFT` card.
  The operator arms it via the CLI (`editorial_cli.py arm <id>`) or the app
  UI; once `ARMED`, the agenda controller (or a matching direct-host query)
  can auto-attach and activate it — see `docs/EDITORIAL_CARDS_CLI.md`.
- **Demotion-on-update rule**: if an agent's `POST /api/agent/cards` upsert
  hits a card that is currently `ARMED` or `ACTIVE`, the handler forces it
  back to `DRAFT` and returns `"demoted": true`. This closes a real hole —
  without it, an agent rewriting a card's content would silently keep it
  eligible for auto-attach with unreviewed text. The operator must re-arm
  before the new content can fire again. **This is API-only behavior — see
  section 7 for the one place the CLI does not do this.**
- **Notices**: sit as `status="proposed"` until an operator calls
  `POST /api/agent/notices/{id}/dismiss`. There is no UI panel for them yet
  (a follow-up track); the store and endpoints ship ahead of that UI so
  agents have a stable contract today.

## 7. CLI equivalence

`opencohost/editorial_cli.py` and `/api/agent/*` both front the same stores,
but the CLI has a broader surface (arming, linking, lifecycle management) that
the agent gateway deliberately does not expose — arming/activating a card is
an operator-only decision (design trust-tier matrix).

| CLI command (`editorial_cli.py`) | `/api/agent/*` equivalent | Notes |
|---|---|---|
| `create` / `create --from-json` | `POST /api/agent/cards` | Same `EditorialCardStore.upsert`, same field caps. **Behavioral difference below.** |
| `create --expires <DATE>` | `POST /api/agent/cards` with `expires_at` | Not a format difference — both accept `YYYY-MM-DD` and ISO-8601 (`_parse_iso8601_utc` in `opencohost/api/main.py` uses `datetime.fromisoformat`, which parses date-only strings identically to the CLI). **Real difference below (past-date check).** |
| `list`, `show` | `GET /api/agent/status` (counts only) | No agent-reachable endpoint returns full card content — status is aggregate counts by design (R8-style: no title/summary/take leaks to the agent token). |
| `arm` | — none — | Operator-only; the demotion rule above is the API's substitute safety net. |
| `link` | — none — | Operator-only (explicit topic-slug override). |
| `rearm` | — none — | Operator-only recovery path. |
| `disable` | — none — | Operator-only. |
| `delete` | — none — | Operator-only, irreversible; no agent-reachable delete anywhere in this gateway. |
| `topic propose` | `POST /api/agent/topics` | Same `TopicInboxStore.propose`, same caps and slug dedupe. |
| `topic list` | `GET /api/agent/status` (`topics_pending` count only) | CLI shows full valid/invalid rows; the API gives a count, not content. |
| `topic discard` | — none — | Operator-only (app UI). |
| `topic approve` | — none (always refused on the CLI too) | Both surfaces require the app UI for approval — this is the one place CLI and API already agree. |
| — none — | `POST /api/agent/notice`, `GET/POST .../notices[/dismiss]` | No CLI equivalent exists; the notice board is API-only. |

**Two behavioral differences to know:**

1. **Demotion on re-create**: `editorial_cli.py create` reuses
   `EditorialCardStore.upsert` directly and does **not** demote an `ARMED`/
   `ACTIVE` card on re-create (see `editorial_cards.py`'s `upsert`, which
   preserves `status` as-is) — that silent-rewrite gap is exactly what
   section 6 above closes, but only for the `/api/agent/cards` path. A human
   operator re-running `create` from the CLI on an already-armed card still
   leaves it armed with the new content, no re-approval prompted. This is
   intentional: the CLI is a trusted-operator tool; the demotion rule exists
   specifically because the API path accepts untrusted agent input.
2. **Past-date rejection on `--expires`**: the CLI refuses to create a card
   with an expiry in the past (`editorial_cli.py`: `"expires date is in the
   past"`). `POST /api/agent/cards` has no equivalent check — an agent can
   send an `expires_at` in the past and get `200`, creating a card that is
   instantly `EXPIRED` (silently useless, no error). If an agent-authored
   card never seems to fire, check whether its `expires_at` was already in
   the past at creation time.

## 8. Deferred: MCP wrapper

A thin stdio MCP server exposing `kira_propose_topic`, `kira_upsert_card`,
`kira_post_notice`, `kira_gateway_status` as MCP tools (each a direct HTTP
call to `/api/agent/*` with the agent token) is **spec'd but deferred**. Every
target agent (Claude Code, Codex, Gemini CLI) can already call this REST
contract via `curl`/`fetch` with zero new surface to install or run; an MCP
server is a second process with its own lifecycle to maintain. Ship it only
if v1 REST usage shows real friction — the contract above is designed so the
MCP tool schemas would fall out of it 1:1 later.
