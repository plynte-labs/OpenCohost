# OpenCohost Repo and Safety Audit

Date: 2026-06-07

## Scope

This audit checks whether the current `audit/packaging-readiness` branch is safe
to use as a public OpenCohost migration source.

Commands/evidence used:

- `git status --short`
- `git ls-files`
- `git ls-files -ci --exclude-standard`
- `.gitignore` review
- `scripts/git-safety-check.ps1` review
- secret keyword scan over tracked text files
- hardcoded local path / external service scan
- focused inspection of tracked config files

## Summary

The branch is not public-repo ready yet.

The current `.gitignore` protects many future runtime/private artifacts, and the
pre-commit hook blocks obvious staged secrets/runtime files. However, several
already-tracked files are ignored now but still present in Git history/index, so
they would be published unless explicitly removed or migrated from a clean base.

## Findings

### CRITICAL - Engram local memory database is tracked

Evidence:

- `.engram/graph.db` is tracked.
- `.engram/config.json` is tracked.
- SQLite inspection found local Engram tables including `nodes`, `edges`,
  `stats`, `query_cache`, and `pattern_cache`.

Risk:

- The database may contain agent memory, prior prompts, implementation context,
  decisions, or private project notes.
- This should not be part of a public OpenCohost repository.

Recommendation:

- Remove `.engram/` from the public migration source.
- If the target repo is public, do not simply push current history unless the
  history strategy is explicitly accepted.
- Keep Engram as local/operator memory only.

### HIGH - `Documents/` is ignored but tracked

Evidence:

- `git ls-files` includes:
  - `Documents/ROADMAP_COMERCIAL.md`
  - `Documents/Resilencia.md`
  - `Documents/futureserver.md`
  - `Documents/koromi.md`
  - `Documents/models.md`
  - `Documents/uso.md`
- `Documents/` is listed in `.gitignore`, but ignore rules do not remove
  already-tracked files.

Risk:

- These look like personal/internal research, roadmap, commercial, and planning
  notes rather than curated public product documentation.
- Some ignored local screenshots are also present in the local folder, though
  they are not currently tracked.

Recommendation:

- Remove `Documents/` from public migration unless each file is explicitly
  reviewed and promoted into curated public docs.

### HIGH - `config/music_library.json` is tracked with local absolute paths

Evidence:

- `config/music_library.json` is tracked.
- It contains absolute `assets\music\...` paths.
- It includes original music filenames/titles.
- `.gitignore` now ignores `config/music_library.json`, but the file remains
  tracked.

Risk:

- Public portability blocker: paths only work on the current machine.
- Potential licensing/privacy issue around personal music library metadata.

Recommendation:

- Remove this runtime file from public migration.
- Provide an empty/default template only if the app needs one.

### MEDIUM - `config/avatar.yaml` contains machine-specific absolute paths

Evidence:

- `config/avatar.yaml` is tracked.
- `state_images` point to `assets\avatar\kira\...`.

Risk:

- External users will not have those paths.
- This is not a secret, but it is a portability blocker.

Recommendation:

- Convert to relative/default asset resolution or generate per-user runtime
  config.

### MEDIUM - `perfiles.json` is tracked with persona prompts

Evidence:

- `perfiles.json` is tracked and contains Kira/persona prompts.

Risk:

- Not necessarily a secret, but it is product identity content and should be
  intentionally reviewed before public release.
- Some profiles are clearly prototype/operator-specific.

Recommendation:

- Decide whether to ship curated default personas or move this to user data with
  a sanitized example file.

### MEDIUM - hardcoded local setup assumptions remain documented/in code

Evidence examples:

- `README.md` references `python` and
  `python`.
- `core/health_monitor.py` has `QwenProcessManager.XTTS_PYTHON` set to
  `python`.
- `mudanza.py` references `modelos_f5`.
- Local services assume:
  - Ollama on `127.0.0.1:11434`
  - Qwen/Flask TTS on `127.0.0.1:5000`
  - LiveAudio websocket on `127.0.0.1:8765`
  - OBS websocket default `localhost:4455`

Risk:

- External users cannot reproduce setup from a clean machine without tailored
  instructions.
- Some assumptions are acceptable local-first defaults, but they need explicit
  documentation or config indirection.

Recommendation:

- Treat Qwen env path, local service ports, Ollama, OBS, YouTube OAuth, and
  audio devices as setup requirements in the public README/checklist.
- Make the Qwen Python executable configurable before packaging/public release.

### PASS - OAuth/token generated files are ignored

Evidence:

- `.gitignore` covers:
  - `data/stream_admin/oauth_client.json`
  - `data/stream_admin/oauth_tokens.json`
  - `data/stream_admin/tokens/`
  - `data/`
- `config/stream_admin.yaml` uses environment placeholders for YouTube OAuth
  credentials.

Risk:

- Existing tracked source code contains variable names like `client_secret` and
  `password`, which can trigger simple scanners, but no concrete OAuth secret was
  found in the audited config.

Recommendation:

- Keep generated OAuth files out of Git.
- Consider extending the safety hook to block `.engram/` and `Documents/`.

### PASS WITH LIMITATION - pre-commit hook blocks obvious staged artifacts

Evidence:

- `scripts/git-safety-check.ps1` blocks `.env`, `data/`, `modelos_f5/`, `temp/`,
  `logs/`, runtime binary/media/model/database extensions, OAuth token JSON, and
  token directories.

Limitation:

- It does not remove or protect already-tracked ignored files from history.
- It does not currently block `.engram/` or `Documents/` paths directly.

Recommendation:

- Extend blocked path patterns with:
  - `^\.engram/`
  - `^Documents/`
  - `^config/music_library\.json$`

## Release Blockers Before Public Migration

1. Remove `.engram/` from the public migration source.
2. Remove or explicitly curate `Documents/`.
3. Remove `config/music_library.json` from public migration or replace with a
   sanitized template.
4. Fix or document machine-specific paths in `config/avatar.yaml`,
   `core/health_monitor.py`, and `README.md`.
5. Decide whether `perfiles.json` ships as curated public defaults or moves to
   user data/template.
6. Extend the safety hook to block `.engram/`, `Documents/`, and tracked runtime
   config candidates before public-release work continues.

## Go / No-Go

Current status: NO-GO for public repository migration.

This is not a runtime blocker for local development. It is a publication blocker:
the current branch can continue as an internal/audit branch, but OpenCohost
public migration should wait until the blockers above are resolved or explicitly
accepted.

## Status update — 2026-06-10 (public_repo_migration_20260610)

The `public_repo_migration_20260610` SDD change (PR chain #12-#16 on tracker
`feat/public-repo-migration`) resolved most blockers above:

1. `.engram/` — untracked and gitignored (was still tracked, including the
   memory database `graph.db`; caught during track closure, fixed on PR4 branch).
2. `Documents/` — tracked files sanitized of paths/identifiers in PR4.
   OPEN: the curate-vs-remove decision for public scope (notably
   `ROADMAP_COMERCIAL.md`, which is business content).
3. `config/music_library.json` — untracked (vestigial user state; runtime reads
   from user data dir).
4. Machine-specific paths — fixed: `resolve_xtts_python()` chain in
   `config/storage.py`, relative `config/avatar.yaml` paths, README rewritten.
5. `perfiles.json` — curated public defaults shipped in
   `config/default_profiles.json` (6 profiles, attribution stripped); live file
   untracked.
6. Guard — `.pre-commit-config.yaml` with detect-secrets (pinned) + the
   `tools/check_abs_paths.py` drive-letter hook; full-tree run exits 0.

Additional outcomes beyond the original audit: identity rename to
OpenCohost/plynte-labs (pyproject, LICENSE, AppData, UI), bilingual README,
agent docs stripped of internal tooling references (`CLAUDE.local.md` holds
machine-local context, untracked), `opencode.json` untracked.

Remaining before public push: merge the PR chain, owner manual items (OBS
password rotation, `detect-secrets audit .secrets.baseline`), the `Documents/`
curation decision, and the fresh-history export to `plynte-labs/opencohost`.

Go / No-Go: GO once the PR chain merges and the items above close.
