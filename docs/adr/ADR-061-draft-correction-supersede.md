# ADR-061: A Correction Supersedes the Previous Draft

**Date:** 2026-09-23
**Status:** Accepted
**Track:** `odd/tasks/memory-semantic-recall.md` (T6 b, d)

## Context

Every capturable turn became its own memoria draft. Nothing in the memory code detected a
correction. So "my birthday is May 3" followed by "I got it wrong, it is May 5" left two
drafts. The wrong one could still be injected, since unjudged drafts are lexical injection
candidates, and the promotion judge could still promote it.

The 300-char cap on the draft body was also inherited without a recorded reason.

## Decision

1. **Detection is deterministic and conservative.** `memoria_store.is_correction_turn` matches
   a closed list of anchored, explicit self-correction phrases in Spanish and English: "me
   equivoqué", "quise decir", "corrijo", "corrección:", "no, en realidad es", "perdón, es",
   "I meant", "correction:", "I was wrong", "I misspoke", "actually, it's". It never matches a
   bare word. A plain "no,", "en realidad", "perdón" or "actually" is not a correction. The
   list follows the same rules as `_META_RECALL_PATTERNS`: accent-insensitive, tolerant of the
   history wrapper, linear-time, and extended only together with its test tables. No model
   call is involved, so the turn path is unchanged.
2. **Only one target: the draft of the immediately preceding pair.** It applies only when that
   pair was itself captured (`mem_captured`, which implies non-private). The target key is
   re-derived from the stored pair, the same way the capture derived it. Longer-range
   corrections ("last week I told you X") are out of scope here.
3. **Supersede in place.** `MemoriaStore.supersede_draft` rewrites that row's
   title, content and signature. It keeps the id and stable_key, bumps `revision`, clears
   `judged_at` and the retry verdict, and un-hides the row only if the judge hid it. A row the
   operator muted stays hidden. These are the same semantics as a revision bump in
   `upsert_draft`. The new content is the correction pair alone. The wrong claim is not
   carried along, because the goal is that it stops existing. A vague correction ("no, quise
   decir el 5") is then likely to be rejected by the judge. Losing it is the safe failure:
   nothing wrong gets remembered.
4. **Durable rows are immune.** A `promoted`, `curated`, `imported` or `summary` row is never
   rewritten here, and neither is a private row or another profile's row. In those cases the
   correction is stored as an ordinary new draft. Promotion runs at the next launch, so
   within a session the target is still a draft. The fact index (ADR-060) never embeds drafts.
   A superseded draft that is later promoted gets new content and a new hash, and is embedded
   fresh.
5. **The session summary follows.** The superseded title is replaced in the session's title
   list, so the mechanical summary does not keep the wrong fact either.
6. **Logs carry ids only.** On success the log line is `memoria correction superseded id=
   revision=`. The capture-failure warning no longer logs `stable_key`, because it is built
   from the pair's own significant tokens (RC-8).
7. **The draft cap stays at 300 chars (`_MEMORIA_CONTENT_MAX_CHARS`).** A read-only
   measurement on 2026-09-23 over the real profiles found:
   - 324 drafts: p50 182, p90 298. 28 of them (8.6%) reach 300 and 43 reach 290 or more.
   - Promoted rows: max 209, below the judge's 220 contract.
   - Imported rows: max 218.

   Drafts are the judge's input, so a lower cap would starve the distillation. A higher cap
   would store more of the user's raw words. Injection is already bounded by the 700-char
   budget and `clip_to_remaining`. Three distilled facts need at most 3 x 220 + 2 = 662
   chars.

## Consequences

- Existing rows are not migrated. The change applies to new writes only.
- A false-positive cue rewrites one unjudged draft from the previous turn. The table is
  narrow on purpose to limit that cost.
- Superseding promoted facts would need the judge to see existing facts and emit a
  `supersedes` id. That is an open follow-up and is not decided here.
- ADR-062 (2026-09-23) removes the injection risk named in the Context: unjudged
  drafts are no longer injection candidates, and a judge-rejected draft is scrubbed.
