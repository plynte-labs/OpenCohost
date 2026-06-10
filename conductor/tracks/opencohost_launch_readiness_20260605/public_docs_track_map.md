# OpenCohost Public Documentation Track Map

This map breaks public documentation into small, evidence-backed tracks. Each
track should be implemented and reviewed independently so contributors can trust
the docs without needing to reconstruct the whole repository.

## Track Rules

Every documentation track must:

- be written in English,
- start from real source/config/test evidence,
- separate current behavior from future plans,
- list what was verified and what remains unknown,
- avoid private local data, raw chat, tokens, and local memory contents,
- link to deeper docs instead of repeating broad claims.

## Documentation Track Matrix

| Order | Track | Output | Primary evidence | Required acceptance |
|---:|---|---|---|---|
| 1 | Agent and contributor index | `docs/INDEX.md` or `docs/agents/index.md` | Root docs, module docs produced so far, `AGENTS.md`, `AGENT_HANDOFF.md` | Routes readers by task; does not claim module details before module docs exist. |
| 2 | Work methodology | `docs/METHODOLOGY.md` | `AGENTS.md`, Conductor track conventions, recent hardening workflow, commit history | Explains controlled validation, Conductor usage, review boundaries, and no-blind-expansion rule. |
| 3 | Testing guide | `docs/TESTING.md` | `tests/`, `pytest.ini`, `pyproject.toml`, known commands in `AGENT_HANDOFF.md` | Catalogs tests by subsystem; distinguishes automated/focal/manual/opt-in runtime validation. |
| 4 | Architecture overview | `docs/ARCHITECTURE.md` | `main.py`, `ui/`, `core/`, `smart_aggregator/`, `stream_admin/`, `config/`, prior committed tracks | Explains system boundaries and ownership without overpromising implementation status. |
| 5 | UI shell module | `docs/modules/ui-shell.md` | `ui/app_shell.py`, `ui/app.py`, panel modules, UI tests | Documents Tk mainloop ownership, queued UI callbacks, panel boundaries, and UI validation expectations. |
| 6 | Runtime speech module | `docs/modules/runtime-speech.md` | `core/llm_engine.py`, `ui/voice_control.py`, `ui/ptt_manager.py`, speech lifecycle tests | Documents speech ownership, direct vs agenda interaction, cleanup rules, and stale-state protections. |
| 7 | TTS/audio module | `docs/modules/tts-audio.md` | `server_qwen.py`, `core/sentence_splitter.py`, `core/streaming_speech.py`, TTS/audio tests | Documents light/heavy TTS, Qwen boundaries, audio limitations, and runtime smoke constraints. |
| 8 | SmartAggregator module | `docs/modules/smart-aggregator.md` | `smart_aggregator/`, `ui/smart_aggregator_ui.py`, aggregator tests | Documents agenda/chat boundaries, raw chat privacy policy, filter behavior, and diagnostics limits. |
| 9 | Stream integrations module | `docs/modules/stream-integrations.md` | `stream_admin/`, `ui/stream_admin_ui.py`, OBS-related code/tests, `config/stream_admin.yaml` | Documents OBS, YouTube/OAuth, token privacy, service setup assumptions, and integration validation. |
| 10 | Health/runtime safety module | `docs/modules/runtime-safety.md` | `core/health_monitor.py`, `ui/crash_reporting.py`, health/crash tests | Documents health pill, startup checks, crash evidence, fatal logs, and known native/runtime blind spots. |
| 11 | Product identity and persona | `docs/PRODUCT.md` or README section | `perfiles.json`, profile tests, UI labels, OpenCohost track decisions | Explains OpenCohost/Kira/VoiceAI naming boundaries and what is product vs internal legacy. |
| 12 | Trust and security model | `docs/TRUST_MODEL.md` and/or `SECURITY.md` | `.gitignore`, `scripts/git-safety-check.ps1`, OAuth config, repo safety audit | Documents local-first privacy, token boundaries, raw chat handling, and vulnerability reporting. |
| 13 | Contributor guide | `CONTRIBUTING.md` | Methodology/testing/security docs | Defines PR expectations, evidence required, forbidden changes, and review workflow. |
| 14 | License and notices | `LICENSE`, `THIRD_PARTY_NOTICES.md` | Dependency list, model/assets usage, existing license file | States project license and third-party/model/asset obligations without guessing. |
| 15 | README public entry point | `README.md` | Validated docs above | Summarizes product, quick start, current limitations, and links to validated docs. |
| 16 | Final documentation audit | `docs/DOCS_AUDIT.md` or launch report section | All public docs and evidence checklists | Confirms no unsupported claims, no private data, and no future work presented as current behavior. |

## Module Track Template

Each module documentation track should use this structure:

```markdown
# <Module Name>

## Current State

Describe only behavior verified from source, tests, or committed changes.

## Key Files

| File | Role | Evidence status |
|---|---|---|
| `path/to/file.py` | What this file owns. | Verified / partial / needs follow-up |

## Ownership Boundaries

Explain what this module owns and what it must not mix with.

## Tests and Validation

List relevant tests and what they prove. Mark missing tests explicitly.

## Known Limitations

Describe verified gaps or runtime assumptions.

## Deferred Work

List planned work only if it is documented in a track or roadmap.

## Verification Checklist

- [ ] Files listed exist.
- [ ] Responsibilities match source.
- [ ] Test claims match real tests.
- [ ] Future work is labeled as deferred.
- [ ] No private data or raw chat is exposed.
```

## Evidence Requirements by Track

### Index

The index must not become a fake architecture document. It should route readers
to docs and clearly mark missing docs as planned.

Minimum evidence:

- existing root docs,
- created public docs,
- current module track map.

### Methodology

The methodology doc should explain how OpenCohost work is done, not how generic
open-source projects work.

Minimum evidence:

- `AGENTS.md`,
- recent hardening tracks,
- Conductor track structure,
- known validation rules.

### Testing

The testing guide requires deeper test discovery before writing final text.

Minimum evidence:

- full test file list,
- grouped test scenarios,
- test commands from `AGENT_HANDOFF.md`,
- pytest configuration,
- known opt-in/runtime smoke boundaries.

### Architecture

The architecture overview should be a map, not a replacement for module docs.

Minimum evidence:

- `main.py`,
- `ui/`,
- `core/`,
- `smart_aggregator/`,
- `stream_admin/`,
- `config/`,
- related tests and prior completed tracks.

## Future Plans Placement

Future work belongs in one of three places:

1. Conductor tracks for active or pending design/implementation work.
2. A curated `docs/ROADMAP.md` after public wording is approved.
3. A short `Deferred Work` section inside a module doc when the plan explains a
   current boundary.

Future plans must not be included in `Current State`.

## Review Strategy

Review each documentation track with this process:

1. Read the doc.
2. Check every important claim against its evidence.
3. Mark unsupported claims as remove, relabel, or verify.
4. Confirm English-only public documentation.
5. Confirm no private data or local memory is leaked.
6. Confirm links and filenames resolve.

## First Implementation Slice

The first implementation slice should be `docs/INDEX.md` plus a minimal
`docs/METHODOLOGY.md` skeleton, because those documents define navigation and
work rules without requiring every module claim to be complete.

The README should wait until architecture, testing, and core module docs have
been verified.
