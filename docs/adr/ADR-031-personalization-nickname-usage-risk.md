# ADR-031: Nickname injection — strengthen retrieval without turning Kira fake-friendly

**Date**: 2026-07-10
**Status**: Proposed — no code change yet
**Branch**: `maintenance/big-file-audit-small-fixes-20260629`
**Author**: Claude Code orchestrator (documenting an owner runtime finding, 2026-07-09 CTK session)
**Scope**: Decision record only. No code changes in this ADR. Captures a risk the owner explicitly weighed before any fix is attempted, so the next person to touch `opencohost/core/personalization.py` or the injection call site doesn't "fix" this the naive way and regress the character.

---

## Context

The personalization store (`opencohost/core/personalization.py`) holds a global, profile-independent streamer identity — nickname, occupation, interests, custom instructions — persisted to disk and cached on mtime. `build_injection_block()` (`personalization.py:159-205`) renders it as a read-only `<perfil_streamer>` block and `llm_engine.py:1437-1443` prepends that block to the enriched prompt on every turn. The UI (`opencohost/ui/personalization_panel.py:131`) labels the field "Apodo" (Spanish for nickname); the owner's stored value is `Apodo="Franguh"`.

In the 2026-07-09 CTK runtime session, the owner observed a split behavior: Kira used occupation/interests naturally and unprompted (talking Dark Souls, self-identifying as "soy Kira OpenCohost"), but when asked directly for the streamer's nickname, she did **not** retrieve it from the injected block.

The field is in the prompt every turn (`_FIELD_LABELS` includes `("nickname", "nickname")`, `personalization.py:37`) — this isn't a missing-injection bug, it's a retrieval-under-direct-question failure. The naive fix is to make the nickname more prominent or imperative in the block (e.g., an instruction like "always use this nickname when addressing the streamer").

## Decision

Do **not** ship the naive strengthening alone. The owner explicitly weighed the risk: an imperative, unqualified nickname instruction risks Kira overusing it — "¿qué te parece, Franguh?" every turn — which reads as fake-friendly and breaks the character's dry-sarcasm baseline (Kira does not glad-hand).

Any strengthening of nickname retrieval must ship paired with an explicit usage constraint in the same instruction, not as a separate follow-up:

> Use the nickname sparingly, only when naturally addressing the streamer, never more than once per response, never as filler.

The nickname field itself stays exactly where it is in the injection block (`personalization.py:180-188`) — this is not a "move it" or "duplicate it" fix. The change under consideration is scoped to *how* the field is framed for the model (label strength / accompanying instruction), not whether it's present.

Before shipping any version of this change, it must be validated in a live runtime session against a gemma4-class model — the model family actually driving the observed failure — to confirm (a) retrieval improves on direct ask and (b) usage frequency stays within the "sparingly" bound rather than swinging to overuse. A synthetic/unit test cannot verify LLM instruction-following behavior; this is a real-model check, same discipline as the measure-first precedent in ADR-029.

## Consequences

- **If done right**: Kira answers "what's my nickname?" correctly without adding a tic that undermines her personality on every other turn.
- **If done naively** (imperative instruction, no usage constraint, no runtime validation): risk of a visible personality regression — a chatty, over-familiar Kira — that would likely need to be reverted, costing a second round-trip through validation.
- **No code changes yet.** This ADR exists so the constraint (usage-scoped instruction + gemma4 runtime validation) is attached to the fix *before* implementation starts, not discovered after a bad first attempt ships.
- Scope stays narrow: this does not touch occupation/interests/custom_instructions, which are already retrieved naturally and are out of scope for this decision.

## Status

**Proposed.** Open — no implementation, no owner sign-off on the exact instruction wording yet. Next step: draft the paired instruction text, then validate against gemma4 in a live CTK session per the Decision above.

---

## References

- **Injection block build**: `opencohost/core/personalization.py:159-205` (`build_injection_block`), field order `personalization.py:36-41` (`_FIELD_LABELS`).
- **Injection call site**: `opencohost/core/llm_engine.py:1429-1443` (personalization block prepended to `enriched`).
- **UI field / label**: `opencohost/ui/personalization_panel.py:131` ("Apodo").
- **Design precedent**: `sdd/kira-personalization-onboarding-20260705` (design.md §1/§2), referenced in `personalization.py:3`.
- **Runtime finding**: owner CTK session, 2026-07-09 (Apodo="Franguh" not retrieved on direct ask; occupation/interests retrieved naturally).
- **Companion**: [ADR-029](./ADR-029-prompt-efficiency-kv-cache.md) — same measure-first-against-a-real-model discipline applied to a different runtime claim.
