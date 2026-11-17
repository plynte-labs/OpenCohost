# Usage Activity

The **Activity** panel shows how much OpenCohost has been used on this PC: calls, tokens and compute time, by day, week, month or year. The data stays on this device, in a file that only your Windows account can read. It contains no conversation content.

## Quick path

1. Open the chat stats chip, then select **Activity**.
2. Switch between day, week, month and year.
3. To keep a copy, export JSON or CSV. To clear the history, use **Delete all**. Both ask for confirmation first.

## What the panel shows

- A calendar heatmap of activity by day.
- Calls, tokens and compute time, broken down by:
  - model (local or cloud);
  - call kind: turn, guardrail retry, vision, web search, topic scout, memoria judge, reflection;
  - turn source: direct, PTT, chat, agenda, accumulated or other;
  - profile.
- KV-cache reuse, labelled **real** or **estimated**.
- LAYA decision-layer activity.

## What is stored

Numbers and fixed labels only:

- dates, call counts, token counts, compute times and failure counts;
- call kind, turn source and provider (local or cloud);
- LAYA decision id, mode, acting layer and status;
- model names and profile ids;
- cache counts.

## What is never stored

- chat text, from viewers or from you;
- prompts sent to the model;
- replies;
- images;
- URLs;
- search queries;
- raw runner-log lines.

## Where it is stored

- File: `<data root>/data/usage/usage_ledger.db` (SQLite).
- Installed Windows builds: the data root is `%APPDATA%\OpenCohost`.
- Running from source: the data root is the project folder.
- `OPENCOHOST_DATA_ROOT` overrides the data root in both cases.

## Protection

- The file is restricted to the owner with a Windows ACL when it is created.
- Limit: on Windows, any program running as your user can still read files you own. DPAPI does not change that.
- That is why the ledger holds no conversation content. Reading it shows counts and labels, not chats.

## No data leaves the device

- Nothing is sent anywhere. There is no remote collection.
- Export saves a file only when you ask for one. What you do with that file is your choice.

## Export and delete

| Action | Result | Confirmation |
|---|---|---|
| Export | JSON or CSV download of all daily rows | Required |
| Delete all | Removes every ledger row | Required |

## Turning it off

- Set `OPENCOHOST_USAGE_LEDGER=0`, then restart OpenCohost.
- The ledger is on by default. While it is off, no new usage is recorded.

## Cache numbers

- **Real** reuse is shown only when OpenCohost started Ollama itself. It is read from the runner log that OpenCohost writes at that start.
- Otherwise the panel shows an **estimate**, labelled as one.
- Cloud models report no cache numbers.

## Compute time

- Local calls: model load, prompt processing (prefill) and generation (decode) time.
- Cloud calls: wall-clock time of the request.
- There is no electricity or cost estimate.

## LAYA activity

- Counts of decisions asked, how many LAYA acted on, and the outcome status.
- Decision values and reasons are not stored.

## Verify it yourself

- [ ] Open `usage_ledger.db` with any SQLite viewer.
- [ ] Check that the tables hold only dates, counts, time totals and fixed labels.

## Next step

For everything that leaves your machine, see [PRIVACY.md](PRIVACY.md).
