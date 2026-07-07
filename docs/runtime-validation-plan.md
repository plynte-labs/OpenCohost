# OpenCohost — Backend Runtime Validation Plan

The standing release gate. The reliability fixes (commit `c02d0a9`) are
verified at **unit** level; this plan confirms them at **runtime** against the
live API + real engine. Until every gate drill PASSES, the audit verdict stays
**NO-GO** (audit report: engram `sdd/backend-reliability-audit/report`, id 2931).

- **Base URL:** `http://127.0.0.1:8765` (run-api.bat; falls back to `:8770` if 8765 is busy).
- **Launch:** `run-api.bat` (binds `127.0.0.1`, `--workers 1`).
- **Two drills (9, 10) are NOT local-first gates** — they document the deferred
  pre-LAN security track (auth/exfil/SSRF). Record results, don't block GO on them.

Legend: **[GATE]** blocks GO · **[DOC]** evidence only.

---

## 0. Preconditions [GATE]

1. Launch `run-api.bat`; confirm the banner shows `--host 127.0.0.1 --workers 1`.
2. Single-worker backstop: in a second shell run
   `E:\Miniconda\envs\flux_env\python.exe -m uvicorn opencohost.api.main:app --host 127.0.0.1 --port 8766 --workers 2`.
   - **PASS:** the extra worker(s) die on the `EngineHost` msvcrt lockfile
     (`RuntimeError`, not a clean `_check_single_worker` message). Confirms the
     lockfile — not the env-var check — is the real guard (audit P3 / contract §2).

## 1. Happy-path smoke + latency buckets [GATE]

Hit all 46 endpoints from the live Tauri client (or curl). Record latency per
endpoint; bucket per contract §3:
- **A (fast):** `/api/health` `/api/status` `/api/chat/last-reply` `/api/music/state` `/api/agenda` `/api/avatar/config` `/api/obs/config`
- **B (moderate):** `/api/models` `/api/obs/test` `/api/music/import` `/api/music/track/{id}/audio`
- **C (heavy):** `/api/chat/turn` `/api/commands`
- **PASS:** every endpoint returns its expected shape; **group A stays responsive
  throughout every drill below** (this is the core fast-path invariant).

## 2. P1 — OBS-test bound [GATE]  · validates WU1

The blocker. Point obs/test at a blackhole address and hammer it while polling health.

```powershell
# 10 concurrent obs/test at a black hole
1..10 | ForEach-Object { Start-Job { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8765/api/obs/test" -Body '{"host":"10.255.255.1","port":4455}' -ContentType "application/json" } }
# meanwhile, in another shell, poll the fast path:
1..30 | ForEach-Object { (Measure-Command { Invoke-RestMethod "http://127.0.0.1:8765/api/health" }).TotalMilliseconds; Start-Sleep -Milliseconds 300 }
```
- **Pre-fix expectation (what the audit found):** each obs/test pins ~21s, process
  thread count climbs, `/api/health` latency spikes/stalls.
- **PASS (post-fix):** obs/test returns at/near the 5s bound; `/api/health` stays
  flat (low ms) throughout; process thread count returns to baseline after.

## 3. Music-import idempotency + lock scope [GATE]  · validates WU4, WU5

1. Import the **same file** twice (retry / double-click); try with the same
   Idempotency-Key, a rotating one, and none.
   - **PASS:** exactly **one** track in `/api/music/library` and **one** copy on
     disk in the managed music dir (pre-fix: duplicates).
2. Import a ~200MB file while hammering `/api/music/library` and `/api/music/mood`.
   - **PASS:** library/mood stay responsive during the copy (pre-fix: serialized
     behind the 200MB copy under the lock).

## 4. Music-audio TOCTOU [GATE]  · validates WU5

Loop `GET /api/music/track/{id}/audio` while issuing `DELETE /api/music/track/{id}`
concurrently for the same track.
- **PASS:** no `500` / truncated stream. A delete during an in-flight stream
  returns a retryable `503 music_write_failed` and the track stays registered;
  after the stream ends, delete returns `200`.

## 5. Chat-turn idempotency [GATE]  · validates WU4

Against a heavy/stalling model that exceeds ~120s: `POST /api/chat/turn` with a
**stable** `Idempotency-Key`, let the UI-perceived timeout fire, retry with the
**same** key.
- **PASS:** Kira speaks **once** (pre-fix: the 120s TTL pruned the key and the
  retry double-fired). Repeat for `/api/commands` and `/api/perfiles/switch` to
  confirm effect-idempotency.

## 6. Crash-atomicity [GATE]  · validates WU2

For each config file, `kill -9` / force-close the process **during** a save, then
restart and reload:
- `avatar.yaml` (via `PUT /api/obs/config` or `/api/avatar/config`)
- `profiles.json` (via `POST/PUT/DELETE /api/perfiles`)
- `cohost_profiles.json` (via `POST /api/agenda/cohost-profiles`)
- music library json (via `POST /api/music/import` or `DELETE track`)
- **PASS:** every file survives intact (pre-fix: the music library and avatar.yaml
  could wipe to empty/defaults). A genuinely corrupt file is quarantined to
  `<file>.corrupt`, not silently emptied.

## 7. Dependency-down drills [GATE]  · validates WU1/WU2/WU3/WU5

Run each in isolation:
- **(a) Ollama hung** → at shutdown, `stop()` returns within a few seconds, no
  lingering `ollama` process (WU5 bounded unload).
- **(b) SQLite locked** → hold an external write lock on `memorias.db` >1s, then
  `POST /api/memoria/flags|delete|update` → **`503 memoria_unavailable`**, NOT
  `500` and NOT a misleading `404` (WU3).
- **(c) Filesystem read-only** → `chmod`/deny-write the config dir, then
  `PUT` config / `POST` import → **`503`** (`config_write_failed` /
  `music_write_failed`), NOT a false `200` and NOT `500` (WU2/WU3).
- **(d) Corrupt YAML/JSON** → truncate `avatar.yaml` / `cohost_profiles.json` /
  library json and reload → the operator gets a data-loss signal (`.corrupt`
  quarantine / `503 config_unreadable`), NOT a silent revert to defaults (WU2).
- **(e) OBS offline / wrong password / broken websocket** → repeat `POST /api/obs/test`
  and watch the process fd/socket count → no monotonic leak (WU1 try/finally).

## 8. Reconnect / recovery [GATE]  · contract §4

Close the Tauri app **mid-turn**, reopen, `GET /api/chat/last-reply` + `/api/status`.
- **PASS:** state is queryable on reopen; process thread count matches baseline
  (no orphans); the accepted turn either completed-and-was-documented or is
  cleanly absent — no duplicated dangerous action.

## 9. Exposure verification [DOC] — deferred auth track

- `curl` every mutating endpoint with no credential → confirm none is required
  (documents the no-auth state; the deferred security track adds the token).
- From a non-allowlisted browser origin, confirm CORS blocks the JSON `POST`
  while `curl` succeeds → proves CORS is not the auth boundary.

## 10. Exfil / SSRF confirmation [DOC] — deferred auth track

- `POST /api/music/import` with an absolute path to a `.wav` **outside** any
  managed dir → confirm it is copied and downloadable via track audio (exfil
  primitive; closed only by the deferred import redesign).
- `POST /api/obs/test` at an internal `host:port` and time the response (SSRF
  timing oracle; closed only by the deferred target allowlist).

---

## Verdict checklist

Move the verdict off **NO-GO** only when **every [GATE] drill (0–8) PASSES**.

| Verdict | Condition |
|---|---|
| **GO** | All gate drills pass; no new P0/P1 surfaced. |
| **GO WITH MINOR FIXES** | Gates pass; only P2/P3 residue, tracked. |
| **NO-GO** | Any gate drill fails, or a new P0/P1 appears. |

The [DOC] drills (9, 10) are expected to still show the no-auth / exfil surface —
that is the **pre-LAN security track**, not a local-first gate.
