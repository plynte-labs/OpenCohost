# ADR-016: Public Repository Strategy — MIT License + Curated Fresh-History Export

**Date**: 2026-06-23
**Status**: Accepted
**Scope**: Public GitHub launch strategy, licensing, documentation curation, and repository migration.
**Related track**: `conductor/tracks/opencohost_repo_export_20260610/`

## Context

OpenCohost is moving from a private development repository to a public GitHub
repository under `plynte-labs/opencohost`.

The project is intended to be a public proof of work and community-friendly
open-source product. The owner explicitly chose to keep the MIT License.

The public-readiness audit found that the current working tree is much cleaner
than earlier migration snapshots, but the private repository still contains
process-heavy history and tracked internal tooling/docs that are not ideal as a
public onboarding surface.

## Decision

OpenCohost will launch publicly with:

1. **MIT License retained.**
2. **Fresh-history export** to the new public repository.
3. **Private historical repo retained** as the internal archive.
4. **Curated public documentation** instead of raw agent/handoff/process notes.
5. **Public trust documents** before flipping visibility to public.

## Rationale

### MIT is a community strategy, not exclusivity protection

MIT does not stop copying. It permits use, modification, redistribution, and
commercial use as long as the license notice is preserved.

That is acceptable for this project. In an agentic era where code can be
reproduced quickly, the stronger advantage is not secrecy over every line of
code. The stronger advantage is:

- public proof of engineering ability,
- community trust,
- a recognizable project identity,
- fast iteration,
- useful demos,
- clear contribution paths,
- and a maintainership story people can follow.

The project should protect raw operational process and credentials, not the
source code intended to be open.

### Fresh history reduces irreversible publication risk

Anything pushed to a public repository can be cached, indexed, forked, or cloned
before it is deleted. A fresh-history export gives the public repo a clean,
reviewable starting point and keeps historical process notes private.

### Public docs must replace internal docs

Internal agent files and handoff notes are useful for development, but they make
poor public onboarding material. Public users need:

- quickstart,
- contribution rules,
- security policy,
- privacy/trust model,
- support expectations,
- issue templates,
- honest limitations.

They should not need to reverse-engineer the project from private workflow notes.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Publish current repo/history as-is | Leaks too much internal process context and makes cleanup irreversible after publication. |
| Rewrite full history with filtering | More complex than a fresh export and still risks missing process-heavy commits. |
| Use a more restrictive license | Reduces adoption and conflicts with the owner's portfolio/community goal. |
| Keep repository private | Protects the code but loses the open-source/community/portfolio benefit. |

## Consequences

### Positive

- Public repo starts with a clean story.
- MIT makes the project easy to use, fork, learn from, and contribute to.
- Internal historical context remains available privately.
- Contributors get purpose-built documentation instead of raw operational notes.

### Negative

- Public history loses detailed private development lineage.
- Public contributors will not see every historical decision unless curated into
  docs/ADRs.
- Anyone can legally reuse the code under MIT terms.

### Required follow-up

- Remove tracked ignored internal tooling from the export tree.
- Fix README setup instructions that currently reference missing `requirements.txt`.
- Add public collaboration, security, privacy, and trust-model docs.
- Audit the exact export tree with secret scanning and pre-commit hooks.
- Verify from a fresh clone before making the new repo public.

## Acceptance criteria

- [ ] `LICENSE` remains MIT.
- [ ] Public repo is created from a fresh curated tree, not the private repo history.
- [ ] Private runtime artifacts and internal tooling are absent from the public tree.
- [ ] Public README setup works on a fresh clone.
- [ ] Security/privacy/trust docs exist before public visibility.
- [ ] The old private repo remains available as historical archive.

