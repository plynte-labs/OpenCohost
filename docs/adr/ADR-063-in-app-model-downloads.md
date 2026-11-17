# ADR-063: OpenCohost Downloads Models In-App (Resumable, One at a Time)

**Date**: 2026-09-24  
**Status**: Accepted / Implemented (backend + API; UI pending)  
**Supersedes**: the owner decision "OpenCohost does not download models — Ollama does" (`OpenCohost_UI/src/features/controles/ModelCard.tsx:61`) and the model-download item in the Out of Scope list of `conductor/tracks/dynamic_model_management_20260608/spec.md:107-113`  
**Scope**: `opencohost/core/model_downloads/` (`manager.py`, `tags.py`, `hf_quants.py`), `opencohost/api/routers/model_downloads.py`, `opencohost/api/main.py` (lifespan wiring), `opencohost/api/models.py`; retires `_download_model_worker` and the `download_model` engine command  
**Feature document**: `odd/tasks/model-download-manager.md`

---

## Context

Until now a user without a model was told to copy `ollama run <model>` into a terminal. Catalog models that are not installed show up disabled. That was deliberate: it kept download complexity out of an early product. It is now the biggest first-run hurdle. It also fails the users the product targets. On an unstable LATAM connection, a multi-GB pull that dies near the end, or a PC that shuts down, sends a non-technical streamer back to a terminal.

A pull worker with streamed progress already existed (`_download_model_worker`), but nothing could reach it. Its only entry was a `download_model` command on the engine's command queue that no API route enqueued. It also had the wrong shape for the job:

- It ran as an engine command.
- It had no pause, cancel or retry.
- It kept no record that survives a restart.
- It activated the model implicitly after the download.

T1 (a spike against Ollama 0.30.11 on an isolated server) verified the facts this design stands on:

- `/api/pull` streams chunks `{status, digest, total, completed}`. Statuses: `pulling <digest>` per layer, then `verifying`, `writing`, `success`. Errors arrive as `{"error": ...}`.
- Closing the stream and pulling again resumes exactly from the partial.
- After a hard server kill, a re-pull resumes from Ollama's last persisted checkpoint. That checkpoint can be well below the last % the client saw.
- `hf.co/<owner>/<repo>:<quant>` pulls use the same stream shape.

## Decision

1. **A download manager outside the engine.** `ModelDownloadManager` owns the jobs. Each pull runs on its own daemon thread through `ollama.Client`, which honours `OLLAMA_HOST` like every other Ollama call. The manager never enqueues engine commands and never touches the turn path.
2. **One active job at a time; a second request is rejected (409), not queued.** A hidden queue of multi-GB downloads is surprising on a metered, unstable link. Starting a tag that already has a paused or failed job resumes that job.
3. **Pause and cancel close the stream; resume re-pulls.** Ollama owns the partial blobs, so there is no byte-range code. After a resume, progress comes only from Ollama's chunks, never from the pre-pause numbers, because after a crash Ollama may resume from an earlier checkpoint. Cancel leaves Ollama's partials on disk: Ollama has no API to delete them, and a later pull of the same tag reuses them.
4. **Bounded auto-retry on network errors.** A failed attempt is retried up to `MAX_RETRIES = 3` times, after waits of 2 s, 8 s and 30 s. Network failures include:
   - httpx transport errors and `ConnectionError`/`TimeoutError`
   - Ollama errors that look like network trouble, or report a 5xx
   - a stream that ends without `success`

   The retry budget resets whenever an attempt makes byte progress. "Not found", "rejected", "disk full" and other pull errors fail at once. The user can still retry a failed job with resume.
5. **The job record survives restarts.** It lives in `USER_DATA_DIR/config/model_downloads.json`, is written atomically, and keeps the last 10 jobs. It is written on every transition, and at most every 5 s while progress is flowing. A corrupt record fails open to an empty history. When the app shuts down, and when a job that was active is loaded again after a crash, the job comes back as `paused` with `interrupted: true`. A large download never restarts without the user.
6. **Completion refreshes discovery and emits an event.** On `success` the manager runs bounded Ollama discovery and sets `installed` accordingly. It then emits `download_done` through the host's whitelisted `_record_motor_event`. `download_start` and `download_error` use the same path, and these three events carry no detail. Progress numbers are polled from `GET /api/models/downloads`. The download does not switch the active model. Selecting it stays an explicit user action, so the old worker's implicit activation, and its history-continuity special case, are gone.
7. **Only two tag shapes are accepted:**
   - Ollama library tags, `[namespace/]name[:tag]`. The namespace has no dot, so a tag can never name another registry host.
   - `hf.co/<owner>/<repo>[:<quant>]`.

   Tags with schemes, whitespace, `..` or more than 200 characters are rejected.
8. **Hugging Face quant listing.** The public tree API (`/api/models/{owner}/{repo}/tree/main?recursive=true`) is read with a 10 s timeout. The parser is defensive:
   - Split GGUF files are summed per quant.
   - `lfs.size` wins over the top-level size.
   - `mmproj-*` files are skipped.

   Each quant gets a `vram_fit` hint (`fits`, `tight`, `too_large` or `unknown`) that compares its file size, with 20% headroom, against the total VRAM from the existing hardware guideline.
9. **Privacy.** Logs and the wire carry the model tag, numbers, state, a fixed error category and the exception type. They never carry exception text, which can embed upstream URLs or local paths.
10. **The orphan worker is retired.** `_download_model_worker`, the `download_model` command case, the `_downloading` flag, and the tests that exercised only that dead path are deleted.

## API

| Method and path | Result |
|---|---|
| `POST /api/models/downloads` `{tag}` | `202` job; `422 invalid_tag`; `409 {detail: download_active, active_job_id}`; `503 ollama_unreachable` |
| `GET /api/models/downloads` | `{jobs: [...newest first], active_job_id, max_retries}` |
| `POST /api/models/downloads/{id}/pause\|resume\|cancel` | `200` job; `404 job_not_found`; `409 invalid_transition` or `download_active`; `503 ollama_unreachable` (resume) |
| `GET /api/models/hf/{owner}/{repo}/quants` | `{owner, repo, vram_mb, quants: [{quant, size_bytes, file_count, tag, vram_fit}]}`; `422 invalid_hf_repo`; `404 hf_repo_not_found`; `502 hf_unavailable` |

## Consequences

- A user with Ollama but no model can reach `LOCAL_READY` without a terminal once the UI (T4) lands.
- After a power loss, the "Resume" prompt continues from Ollama's checkpoint. The % shown can step back, which is honest: the UI must not promise the pre-crash value.
- A pause or cancel changes the job state at once and stops applying chunks. The stream itself is closed at the next chunk, and the control thread also closes the httpx client on a best-effort basis. How fast that close interrupts a read that is blocked mid-stall has not been measured, and T6 should observe it. A stall with no pause trips the 60 s read timeout and goes through the retry path. A resume waits up to 5 s for the previous stream to close, then answers 409 `invalid_transition`.
- Known limits:
  - The HF tree API is read one page only.
  - The fit hint uses total VRAM, not free VRAM.
  - How often Ollama checkpoints partials is unmeasured (T1 took one sample).
