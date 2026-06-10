# OpenCohost Documentation Index

This index helps humans and coding agents find the right documentation before
changing OpenCohost. It is intentionally a router, not a full architecture
document.

## Start Here

| Goal | Read first | Status |
|---|---|---|
| Understand current working rules | [`../AGENTS.md`](../AGENTS.md) and [`../AGENT_HANDOFF.md`](../AGENT_HANDOFF.md) | Current repo guidance |
| Understand how work should be planned and validated | [`METHODOLOGY.md`](METHODOLOGY.md) | Initial public skeleton |
| Understand automated and manual testing | [`TESTING.md`](TESTING.md) | Initial public guide |
| Understand the architecture | [`architecture.md`](architecture.md) | Initial public map |
| Contribute safely | `../CONTRIBUTING.md` | Planned public doc |
| Understand privacy/security boundaries | `TRUST_MODEL.md` or `../SECURITY.md` | Planned public doc |

## Current Reference Docs

These documents exist today and may contain useful context. They still need a
public documentation audit before being treated as final OpenCohost docs.

| Area | Existing references |
|---|---|
| UI architecture | [`architecture.md`](architecture.md), [`UI_ARCHITECTURE.md`](UI_ARCHITECTURE.md) |
| Runtime smoke validation | [`RUNTIME_SMOKE_HARNESS.md`](RUNTIME_SMOKE_HARNESS.md) |
| Security/privacy audit notes | [`SECURITY_PRIVACY_PENTEST_AUDIT.md`](SECURITY_PRIVACY_PENTEST_AUDIT.md) |
| Test suite audit notes | [`test_suite_audit_full.md`](test_suite_audit_full.md) |
| Cohost agenda mode | [`KIRA_COHOST_AGENDA_MODE.md`](KIRA_COHOST_AGENDA_MODE.md) |
| Live safety controls | [`LIVE_SAFETY_CONTROLS.md`](LIVE_SAFETY_CONTROLS.md) |
| Decisions | [`DECISIONS.md`](DECISIONS.md), [`adr/`](adr/) |

## Where to Go by Task

| Task | Read first | Why |
|---|---|---|
| Runtime debugging | `METHODOLOGY.md`, then planned `modules/runtime-safety.md` | Runtime work needs evidence and validation boundaries. |
| UI changes | `../AGENTS.md`, [`modules/ui-shell.md`](modules/ui-shell.md), `UI_ARCHITECTURE.md` | UI work must respect Tk mainloop ownership. |
| Speech/TTS/audio changes | `METHODOLOGY.md`, [`modules/runtime-speech.md`](modules/runtime-speech.md), planned `modules/tts-audio.md` | Audio behavior has unit-test and real-device boundaries. |
| SmartAggregator changes | `../AGENTS.md`, planned `modules/smart-aggregator.md` | Raw chat privacy and agenda/direct boundaries are strict. |
| OBS or stream admin changes | planned `modules/stream-integrations.md` | OAuth tokens and external services require careful setup notes. |
| Testing changes | [`TESTING.md`](TESTING.md), existing `test_suite_audit_full.md` | Test claims must be based on real test discovery. |
| Release/public repo work | `METHODOLOGY.md`, planned `TRUST_MODEL.md`, planned `CONTRIBUTING.md` | Public work must avoid private/runtime artifacts and unsupported claims. |

## Planned Public Documentation Set

These files should be produced in small, evidence-backed slices:

- `docs/modules/tts-audio.md`
- `docs/modules/smart-aggregator.md`
- `docs/modules/stream-integrations.md`
- `docs/modules/runtime-safety.md`
- `docs/TRUST_MODEL.md`
- `CONTRIBUTING.md`
- `THIRD_PARTY_NOTICES.md`
- public README refresh

## Documentation Rule

Do not present future plans as current behavior. If a claim is not verified from
source, tests, configuration, or an accepted design/track, label it as planned,
deferred, or unknown.
