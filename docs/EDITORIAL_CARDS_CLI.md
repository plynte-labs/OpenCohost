# Editorial Cards CLI — Agent & Operator Contract

Command-line interface for creating and managing editorial cards — curated
context that Kira injects into agenda prompts via `<editorial_context>`.
Designed for humans and for external agents (e.g. research or chat-watch
agents) that shell out to it.

## Invocation

```
python -m opencohost.editorial_cli [--db PATH] [--json] <subcommand> ...
```

- Dev checkout: any interpreter with the repo on `sys.path`, run from repo root.
- Packaged install: use the installed environment's interpreter
  (`<install>\python.exe -m opencohost.editorial_cli ...`).
- Default DB: the same `cards.db` the app uses
  (`opencohost.config.settings.EDITORIAL_CARDS_DB`). Override with `--db`
  for sandboxes or tests.

## Execution model (read this before automating)

- **One-shot process.** Every invocation starts, performs exactly one
  operation, prints, and exits. There is no daemon, no open terminal, no
  session, and no in-memory state between calls. Order of effects is the
  order in which you invoke commands.
- **Crash-safe.** Each operation is a single SQLite transaction. If the
  process (or the machine) dies mid-call, the operation either fully
  committed or never happened. There is no partial state to clean up and no
  stale lock: SQLite releases locks when the process exits.
- **Concurrent with the app.** The store opens a fresh connection per
  operation on both sides, so the CLI can run while OpenCohost is open.
  Writers serialize; a concurrent writer waits up to ~5s (sqlite3 default)
  before failing with "database is locked". Treat that error as retryable.
- **Arming makes a card eligible for runtime auto-attach.** Once armed, the
  agenda controller automatically matches the card to incoming topics at
  generation time using normalized token overlap and trigger keywords.
  `link` remains the explicit override for deterministic attachment
  regardless of topic text.

## Idempotency and retries

| Command | Semantics | Safe to retry? |
|---|---|---|
| `create` | **Upsert by topic slug.** Re-sending the same topic updates the existing card (id, status, and use_count are preserved). Duplicate calls never create duplicate rows. | Yes, always |
| `create --expires` | Same as `create` with an optional expiry date (YYYY-MM-DD or ISO 8601). Past dates are rejected. | Yes, always |
| `list` / `show` | Read-only. | Yes, always |
| `arm` | DRAFT → ARMED transition. Calling it on a card that is already ARMED (or used/expired) exits 1. Once ARMED, the runtime agenda controller may auto-attach the card to matching topics. | Yes — exit 1 means "already done or not eligible", not corruption |
| `link` | Activates an ARMED card for a topic slug (explicit override, bypasses auto-attach matching). Exits 1 if the card is not ARMED. | Yes — same convention as `arm` |
| `rearm` | Moves a USED or EXPIRED card back to ARMED. Use `--clear-expiry` to also null out `expires_at`. Exits 1 on DRAFT or ACTIVE cards. | Yes — exit 1 means not eligible |
| `disable` | Moves any non-USED card to EXPIRED. Idempotent on already-EXPIRED cards. Cannot disable USED cards (history preserved). | Yes — idempotent |
| `delete` | Hard-deletes a card and its associated ratings. Irreversible. Exits 1 if not found. | No — destructive; verify card_id first |

Retry policy for agents: on exit 1, do **not** blind-retry — read the error,
the state is already decided. On "database is locked", retry with backoff.
On exit 2, your invocation is malformed — fix the arguments.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Validation failure, not found, or invalid state transition |
| 2 | Usage error (bad arguments) |

## Output contract

- Human mode (default): readable text on stdout, one-line errors on stderr.
- Agent mode (`--json`): single JSON object on stdout; errors are
  `{"error": "..."}` on stderr. Always parse stderr when the exit code is
  nonzero.

## Subcommands

### `create`

```
create --topic T --summary S --take TAKE
       [--counterpoint C]... [--hook H]... [--trigger W]...
```

Or pipe a JSON object (agent-friendly):

```
echo '{"topic": "...", "summary": "...", "streamer_take": "..."}' \
  | python -m opencohost.editorial_cli create --from-json
```

Field limits (validation errors exit 1): `topic` ≤ 120 chars, `summary`
≤ 1200, `streamer_take` ≤ 800; list fields ≤ 8 items, ≤ 240 chars each.
Raw chat or scraped page dumps are rejected by design — cards carry curated
takes, not transcripts.

New cards start in `DRAFT`. Kira does not see DRAFT cards.

### `list [--json]`

All cards: id, topic, status, updated_at, use_count.

### `show <card_id> [--json]`

Full card detail.

### `arm <card_id>`

DRAFT → ARMED. Once armed, the agenda controller automatically matches the
card to incoming topics at generation time using token overlap + trigger
keywords. If the card's normalized tokens match a queued topic with a score
≥ 0.8 (and no ambiguous competitor within 0.1), the card is activated and its
prompt block is injected into the next Kira turn for that topic.

### `link <topic_id> <card_id>`

Store-level activation (ARMED → ACTIVE) for the card's topic slug. This is
the explicit deterministic override — bypasses auto-attach matching entirely.
`topic_id` must equal the card's topic slug; if they do not match the command
exits 1 with an error describing the mismatch.

### `rearm <card_id> [--clear-expiry]`

Moves a USED or EXPIRED card back to ARMED so it can be attached again. Use
`--clear-expiry` to also null out `expires_at`; without it, a card with a
past `expires_at` will be immediately re-expired on the next check.
Exits 1 on cards in DRAFT or ACTIVE state (not eligible).

**Recovery — card stuck in ACTIVE:** if Kira's generation fails after
auto-attach, the card may remain in ACTIVE status indefinitely. Recovery:
`disable <id>` (moves it to EXPIRED), then `rearm <id> --clear-expiry`
(moves it back to ARMED with no expiry). The card is then available for
the next matching topic.

### `disable <card_id>`

Moves any non-USED card to EXPIRED. Idempotent when the card is already
EXPIRED. Cannot disable a USED card — history is preserved.

### `delete <card_id>`

Hard-deletes a card and all its associated ratings. Irreversible.
Exits 1 if the card does not exist.

### `create --expires <DATE>`

Create (or update) a card with an optional expiry date. Accepts `YYYY-MM-DD`
or full ISO 8601 format. Dates in the past are rejected with exit 1. Expired
cards are excluded from auto-attach candidates and cannot be armed.

## Topic inbox (`topic ...`)

Agents can also propose *stream topics* (not cards) for human review. A
proposal is just a title + angle; the operator approves or discards it in the
app UI. **Approval is never available via CLI** — `topic approve` always exits
1 with an explanation. This is a deliberate human-only gate.

Proposals are untrusted input: validation runs at propose time AND again at
read time inside the app, so writing rows directly to SQLite does not bypass
the gate. Limits: title 120 chars, angle 600 chars, 8 tags (40 chars each),
source 80 chars, 30 pending proposals; code/HTML content is rejected.
Re-proposing a title that normalizes to the same slug
(accents/emoji/case-insensitive) updates the existing pending row instead of
duplicating it; punctuation-only titles never dedupe.

**Word-boundary caveat for agents**: the code/HTML filter rejects text
containing any of these words (case-insensitive, whole-word): `function`,
`class`, `import`, `from`, `select`, `insert`, `update`, `delete`, `drop`,
`script`. A title like "Lessons from the 90s" fails with "title contains
code or HTML" — rephrase (e.g. "Lessons of the 90s"). This mirrors the
app-wide agenda filter; refining it is a pending product decision.

### `topic propose --title TEXT --angle TEXT [--tag TEXT]... [--source TEXT]`

Insert (or dedupe-update) a proposal. Prints `<ti_id> proposed` (or the full
row with `--json`). Exit 1 on validation failure or when the inbox is full.
`--from-json` reads `{"title", "angle", "tags", "source"}` from stdin instead.

### `topic list [--json]`

Pending proposals with the angle visible. Text mode hides rows that fail
read-time validation (count is reported); `--json` returns
`{"valid": [...], "invalid": [...]}` with `invalid_reason` per bad row.

### `topic discard <topic_id>`

Discard a pending proposal. Exit 1 if the id is unknown or not pending.

### `topic approve <topic_id>`

Always refused (exit 1): approval happens in the OpenCohost app, in the
agenda suggestions panel, where the operator reads title and angle first.

## Card lifecycle

```
DRAFT --arm--> ARMED --auto-attach/link--> ACTIVE --used by Kira--> USED
  |               \--expires_at reached--> EXPIRED
  +--disable--> EXPIRED                        |
                    \--rearm [--clear-expiry]--> ARMED
USED --rearm--> ARMED
```

## Typical agent loop

```bash
# 1. Push curated context (idempotent — rerun freely)
echo "$CARD_JSON" | python -m opencohost.editorial_cli --json create --from-json

# 2. Arm it (exit 1 if already armed — that is fine, move on)
python -m opencohost.editorial_cli arm "$CARD_ID" || true

# 3. Verify state
python -m opencohost.editorial_cli show "$CARD_ID" --json
```

## Direct host mode

When the host speaks directly to Kira (PTT, LiveVoice, or manual entry), a
matching ARMED card's context is automatically injected into the prompt —
no operator action required.

**How it works**: the host's transcribed or typed query is matched against all
ARMED, non-expired cards using the same token-overlap + trigger algorithm as
agenda auto-attach (`select_card`). If exactly one card wins with score >= 0.8,
its `<editorial_context>` block is prepended to the user message before Kira
sees it.

**Non-consuming**: the card stays ARMED. The injection does not activate,
mark-used, or consume the card — it remains available for the agenda path to
pick up later.

**Triggers are the primary hook**: direct host queries are conversational ("hey
kira what do you think about the gta delay"), so token overlap against a card
TOPIC will often be low. Add `triggers` to your card (e.g. `"gta 6"`,
`"delay"`) so any host query containing those words fires the injection. The
global 0.8 threshold applies; a trigger hit scores 0.9.

**Chat never triggers injection**: anything originating from the chat or
SmartAggregator (force-kira, suggestions, context snapshots) never triggers
card injection. Only the direct host path qualifies.
