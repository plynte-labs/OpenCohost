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
- **Arming is preparation, not injection.** `arm`/`link` manage card state
  in the database. Injecting the prompt block into a live Kira turn
  additionally requires the running app to attach the card to the active
  agenda topic (`topic.editorial_card_id`), and that attachment is not yet
  wired to any operator path — runtime auto-attach by topic match is the
  planned follow-up. Until it lands, prepared cards are not consumed by
  the agenda.

## Idempotency and retries

| Command | Semantics | Safe to retry? |
|---|---|---|
| `create` | **Upsert by topic slug.** Re-sending the same topic updates the existing card (id, status, and use_count are preserved). Duplicate calls never create duplicate rows. | Yes, always |
| `list` / `show` | Read-only. | Yes, always |
| `arm` | DRAFT → ARMED transition. Calling it on a card that is already ARMED (or used/expired) exits 1. | Yes — exit 1 means "already done or not eligible", not corruption |
| `link` | Activates an ARMED card for a topic slug. Exits 1 if the card is not ARMED. | Yes — same convention as `arm` |

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

DRAFT → ARMED. ARMED is the eligible-for-attachment state; it does not by
itself cause prompt injection (see Execution model).

### `link <topic_id> <card_id>`

Store-level activation (ARMED → ACTIVE) for the card's topic slug. This
updates card state only — it does not attach the card to a live agenda
topic inside a running app.

`topic_id` must equal the card's topic slug; if they do not match the command exits 1 with an error describing the mismatch.

## Card lifecycle

```
DRAFT --arm--> ARMED --link/agenda--> ACTIVE --used by Kira--> USED
                 \--expires_at reached--> EXPIRED
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
