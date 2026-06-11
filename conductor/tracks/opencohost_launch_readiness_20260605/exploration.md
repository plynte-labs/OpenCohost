# Exploration — OpenCohost Rename / Rebrand Scope

## Current State

The project still uses several identity layers:

- **VoiceAI** — repo/project/documentation/runtime identity.
- **VocalAI / VocalAIApp** — Python/UI class and factory naming.
- **Kira** — cohost persona, UI/product-facing character, prompts, avatar assets, agenda mode, OBS source naming.
- **OpenCohost** — new product/platform direction, domain, and target repo identity.

This means the rename is not a single search-and-replace. It is a brand architecture decision.

## Affected Areas

- `README.md`, `AGENTS.md`, `AGENT_HANDOFF.md`, `docs/**` — public/internal documentation naming.
- `pyproject.toml` — Python package metadata currently names the project `voiceai`.
- `main.py`, `ui/app.py`, `ui/app_shell.py` — app entry and `VocalAIApp` class/factory naming.
- `config/storage.py`, `config/avatar.yaml`, `config/storage.yaml` — local app data paths and absolute the project directory references.
- `config/logger.py`, `core/health_monitor.py` — logger names and app identifiers like `VoiceAI` / `voiceai-qwen-tts`.
- `perfiles.json`, `core/profiles.py`, `config/settings.py` — Kira persona prompts and default system prompt.
- `avatar/**`, `ui/**`, `smart_aggregator/**`, `stream_admin/**` — Kira as avatar/persona/workflow identity.
- `.opencode/**`, `opencode.json`, `docs/SDD_SKILLS_USAGE.md` — agent/workflow naming and memory project key `voiceai`.
- Tests and docs reference `VoiceAI`, `VocalAI`, and `Kira` extensively.

## Key Distinction

Do not rename every identity at once.

Recommended brand architecture:

- **OpenCohost** = platform/product/repo/domain.
- **Kira** = default cohost persona / flagship demo character.
- **VoiceAI** = legacy/internal codename during migration.
- **VocalAIApp** = technical legacy class name until code migration is justified.

## Approaches

### 1. Documentation-first rename

Rename public-facing docs, README positioning, handoff, and product language first.

- Pros: low risk, clarifies product direction, avoids breaking imports/runtime.
- Cons: leaves code/internal names mixed for a while.
- Effort: Low.

### 2. Product-shell rename

Rename app title, package metadata, visible UI strings, storage display names, and docs while preserving internal class/module names.

- Pros: gives users OpenCohost experience without destabilizing core runtime.
- Cons: requires careful storage/logging migration plan.
- Effort: Medium.

### 3. Full codebase rename

Rename modules/classes/functions/paths from VoiceAI/VocalAI to OpenCohost.

- Pros: clean long-term naming.
- Cons: high regression risk, breaks imports/tests/docs, easy to damage a functional prototype.
- Effort: High.

## Recommendation

Use a staged rename:

1. **Explore/document identity boundaries** in this track.
2. **Update public-facing docs and README** to OpenCohost.
3. **Preserve Kira as default persona**, not product name.
4. **Keep internal `VocalAIApp` / `voiceai` names temporarily** until release blockers are known.
5. Only open a dedicated implementation track if product-shell rename becomes necessary before launch.

This preserves the working prototype while giving OpenCohost a clear public identity.

## Risks

- Search-and-replace could break imports, local storage paths, OBS source names, config defaults, or tests.
- Renaming Kira too aggressively would lose the strongest persona asset.
- Renaming local data paths can orphan existing user config/cache.
- Keeping too many legacy names in public docs weakens launch clarity.

## Ready for Proposal

Yes, but as a **staged rename proposal**, not a direct implementation task.

Recommended next step: expand Phase 1 of `opencohost_launch_readiness_20260605` with an explicit task for “OpenCohost identity map and staged rename boundary.”


## User Clarification ? 2026-06-05

The immediate rename scope is **only what the user sees in the interface**.

- Kira stays.
- VoiceAI/VocalAI should disappear from user-facing UI/chassis copy.
- Repo name, classes, package metadata, imports, Engram project key, local paths, and runtime app IDs are deferred.
- Internal rename should become a separate future track because it requires stronger regression guarantees and should prove tests still pass.

This keeps the current track focused on launch-facing presentation while avoiding a risky broad rename.
