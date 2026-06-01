# Editorial Cue Cards MVP

## Status

Planning only. Do not implement until the user explicitly starts this track.

This track replaces the earlier framing of **Research Sidecar + Knowledge Cards**. The research discussion showed that Kira should not try to become a search engine or autonomous oracle. The stronger product direction is **editorial cue cards**: compact, streamer-curated notes that Kira can use once, on demand, to help conduct the stream.

## Decision Summary

Build a small, optional module that lets the streamer/operator prepare **Editorial Cue Cards** from curated information before or between stream segments, attach them to Agenda/Cohost blocks, then activate one card deterministically through the Agenda/Cohost UI so Kira can use it as temporary context for a single response.

The feature is not a model upgrade. It is a production/show tool.

```text
SmartAggregator = what the chat/community is doing now
Editorial Cue Cards = what the streamer wants to bring into the show
LLM Engine = final response composer
```

## Why We Reached This Conclusion

The original idea explored several possible directions:

- Kira searches Google/cloud directly.
- Kira scrapes wikis or websites.
- Kira uses a Research Sidecar to fetch gaming answers.
- Kira receives Knowledge Cards from external research.
- Kira stores curated topics for later discussion.
- Kira activates cue cards through PTT/voice commands.

The useful product insight was this:

> Kira does not beat Google as a search tool. Kira can beat Google as a cohost that turns curated information into live show material.

If the streamer already checks Google AI, a wiki, or an article, Kira repeating that answer back to the streamer adds little value. The value appears when Kira can help present, remember, and reintroduce the topic to the audience with the channel's voice.

## What We Are Designing Now

An **Editorial Cue Cards MVP** with these properties:

- Created from operator-curated information, usually via clipboard/manual entry.
- Stored locally in SQLite.
- Activated deterministically through Agenda/Cohost UI or block selection.
- Injected as ephemeral user-level context, never as permanent identity/system prompt.
- Used for one turn, then marked `used` and purged from the model context.
- Off by default, isolated from SmartAggregator, and aligned with the existing Agenda/Cohost flow.

## What We Explicitly Discarded

### Auto-search / Kira as oracle

Discarded because it competes with Google/Perplexity/search products, adds latency, and makes source quality hard to control.

### Scraping-first sidecar

Discarded because Cloudflare, DOM changes, TOS, rate limits, and blocked pages make it fragile for live production.

### Browser automation against Google AI

Discarded for MVP because it depends on UI automation, logged-in browser state, captcha behavior, focus handling, and private tab safety.

### Auto-source selection

Discarded because ranking sources is expensive, hallucination-prone, and turns the feature into an editorial verification system.

### Vector database in MVP

Discarded for MVP because semantic retrieval introduces thresholds, false positives, embedding costs, and non-deterministic debugging. SQLite/FTS5 is enough for a small number of cards.

### Fine-tuning / LoRA

Deferred. It is viable later, but only after collecting clean examples of what actually works. Training before that risks making Kira rigid or amplifying bad data.

### Prompt-only prompt-injection defense

Discarded as insufficient. The main defense is structured data, size limits, and not injecting raw copied pages.

### Always-on card injection

Discarded because it would make Kira rigid, distract her from the live moment, consume tokens, and risk turning her into a note reader.

### PTT/voice activation in MVP

Discarded for MVP because Whisper transcription and semantic command routing are non-deterministic in live conditions. A misheard PTT command could activate the wrong card, leave a card floating, or require impossible live debugging. PTT activation may be reconsidered later, but the MVP should validate value through deterministic Agenda/Cohost controls first.

## Product Goal

Let the streamer quickly prepare or capture a topic, then have Kira bring it into the stream at the right moment as a cohost.

Examples:

- A controversy about game monetization.
- A patch note discussion.
- A news item the streamer wants to frame cautiously.
- A running joke or recurring topic.
- A game/lore detail that should be discussed with the audience.

## MVP Scope

### In scope

- Local `EditorialCardStore` backed by SQLite.
- One card active at a time.
- Card lifecycle: `draft -> armed -> active -> used -> expired`.
- Pre-stream or between-segment manual/clipboard-first preparation.
- Minimal fields required: `topic`, `summary`, `streamer_take`.
- Optional fields: `counterpoints`, `discussion_hooks`, `triggers`.
- Deterministic activation by Agenda/Cohost block selection or explicit UI action.
- Ephemeral prompt block for a single request.
- Post-use utility rating.
- Deduplication via `topic_slug` and update/upsert flow.

### Out of scope

- Autonomous web search.
- Scraping.
- Browser automation.
- Cloud AI as a mandatory dependency.
- Vector database or embeddings.
- Multi-card blending.
- Automatic chat-triggered topic injection.
- PTT/voice-command activation.
- Hotkey/clipboard live ingestion.
- Permanent modification of Kira's base system prompt.
- Raw chat persistence or cloud export.
- Fine-tuning/LoRA.

## Integration With Existing VoiceAI

This feature should fit the existing architecture rather than reinventing it.

### SmartAggregator remains unchanged

SmartAggregator keeps handling live chat filtering, compact context, and vibe/intent summaries. Editorial cards must not change raw chat privacy rules or filtering behavior.

### LLM Engine composes context

The LLM Engine should receive an optional editorial block only for the active request. The base profile/personality remains the system prompt. The card is context, not identity.

Suggested composition:

```text
[System] Kira base profile/personality
[User/context] Agenda/Cohost active block, if present
[User/context] SmartAggregator compact chat context, if present
[User/context] <editorial_context> active cue card JSON </editorial_context>, if present
[User] Agenda/Cohost response trigger or current user input
```

After generation, the editorial block must not be appended to long-term conversation history.

### Agenda/Cohost is the MVP activation surface

The first implementation should behave like an extension of Agenda/Cohost Mode, not as a separate voice router. The operator prepares cards before the stream or between segments, links one card to an Agenda/Cohost block, and activates it through the existing control flow for that block.

This keeps the MVP deterministic and avoids known non-deterministic failure modes from PTT/Whisper command routing.

### UI should stay minimal

The MVP should not create a large editorial dashboard. Prefer the smallest Agenda/Cohost-compatible flow:

- prepare cards before stream or between segments,
- paste clipboard/manual text if needed,
- fill or confirm `topic`, `summary`, `streamer_take`,
- optionally add triggers/hooks/counterpoints,
- arm card,
- attach card to an Agenda/Cohost block,
- activate card once through block/UI selection.

## Cost and Complexity Estimate

### Engineering cost

Medium-small if implemented as manual SQLite + prompt injection only.

Main work areas:

- data model and SQLite store,
- minimal Agenda/Cohost-compatible UI surface,
- prompt block builder,
- Agenda/Cohost attachment and activation path,
- tests for lifecycle, dedupe, injection, and safety.

### Runtime cost

Low. SQLite lookup and one compact prompt block are cheap.

### Hardware cost

Near zero for manual MVP. No extra model needs to run.

Optional future extractor model must be CPU-preferred and lightweight to avoid evicting Kira's main Ollama model from VRAM.

### Cognitive cost to streamer

This is the biggest risk. If card creation requires field editing during active gameplay, the feature will not be used. The MVP should prioritize pre-stream or between-segment preparation and one-action activation from Agenda/Cohost controls. Live clipboard/hotkey capture is deferred until the value is proven.

## Data Model

Minimum card shape:

```json
{
  "id": "ec_monetization_001",
  "topic_slug": "game-x-monetization-controversy",
  "status": "armed",
  "topic": "Polémica por monetización en Game X",
  "summary": "La comunidad critica cambios de precios tras el último parche.",
  "streamer_take": "Me interesa debatir si esto cruza la línea hacia pay-to-win.",
  "counterpoints": [
    "El estudio podría argumentar que son cosméticos.",
    "Algunos jugadores prefieren monetización opcional antes que DLC obligatorio."
  ],
  "discussion_hooks": [
    "¿Dónde está la línea entre cosmético y ventaja real?",
    "¿Perdonamos más estas prácticas si el juego base es bueno?"
  ],
  "triggers": ["monetización", "pay to win", "tienda", "parche"],
  "created_at": "2026-05-25T19:30:00Z",
  "updated_at": "2026-05-25T19:30:00Z",
  "expires_at": "2026-05-25T23:30:00Z",
  "last_used_at": null,
  "use_count": 0
}
```

Required fields for MVP:

- `topic`
- `summary`
- `streamer_take`

Optional but useful:

- `counterpoints`
- `discussion_hooks`
- `triggers`

## Constraints

- Maximum one active card per response.
- Summary should be short; target <= 300 characters.
- Total editorial prompt block should have a strict max length.
- Cards must not contain raw copied pages or raw chat.
- Expired cards cannot be activated.
- Used cards should not auto-reactivate without explicit operator action.
- Cards should be activated by deterministic Agenda/Cohost state, not by voice/semantic command routing in MVP.
- Duplicate topic slugs should update existing cards rather than create fragmented duplicates.
- Feature must be disabled when no card is active.

## Security and Safety

### Prompt injection

External or clipboard text is untrusted. Do not pass raw pages to Kira. Store structured fields only.

### URL handling

URL fetching is out of MVP. If added later, block `file://`, localhost, private IPs, unbounded redirects, and giant downloads.

### Privacy

No raw chat leaves VoiceAI. No raw chat is persisted into cards. Cards are streamer-curated editorial artifacts.

### Identity isolation

Cards must not modify Kira's system prompt or base profile. They are one-turn context only.

### Misuse prevention

High-risk domains such as health, legal, finance, accusations, and breaking news should require extra caution or be deferred to a later policy track.

## Edge Cases to Consider

- Operator creates duplicate cards about the same topic.
- Operator activates a card while Kira is already responding.
- Card expires between activation and generation.
- Card is too long and must be compressed or rejected.
- Card topic conflicts with live chat context.
- Agenda block has no linked card.
- Agenda block links to an expired or used card.
- Operator switches Agenda blocks while a card is active.
- Card is used once but streamer wants to reuse it later.
- Card includes unsafe prompt-like text copied from a webpage.
- Card has a strong streamer take but no counterpoints, risking echo-chamber behavior.
- Kira reads the card too literally and sounds like a note reader.
- Kira ignores the card entirely.
- Card activation causes longer latency or worse response quality.

## Evaluation and Kill Criteria

This feature should be discarded or redesigned if it does not improve the stream.

### Track after each card use

- `useful | not_useful | unsure`
- reason: `good_timing`, `too_late`, `awkward`, `too_long`, `ignored`, `wrong`, `unnecessary`, `metadata_leak`, `personality_loss`
- optional short operator note

### Continue only if

- Most manually used cards are rated useful.
- Kira stays natural and aligned with the active profile.
- Kira does not leak internal labels or JSON structure.
- Activation does not noticeably hurt live responsiveness.
- The operator does not feel the workflow interrupts the stream.

### Stop or redesign if

- Cards are mostly rated not useful or unnecessary.
- Kira becomes rigid, repetitive, or sounds like she is reading notes.
- Cards regularly distract from live chat/gameplay.
- Operator friction is too high.
- Metadata leaks occur repeatedly.
- The feature requires cloud/browser/search automation to feel valuable.
- The feature requires PTT/voice routing to feel usable in the first version.

## Acceptance Criteria

- A card can be created manually with `topic`, `summary`, and `streamer_take`.
- A duplicate `topic_slug` updates an existing card instead of creating a duplicate.
- A card can move through `draft`, `armed`, `active`, `used`, and `expired` states.
- A card can be associated with an Agenda/Cohost block or equivalent deterministic UI state.
- Only one active non-expired card can be injected into a response.
- The card is injected as an ephemeral context block, not as Kira's permanent system prompt.
- After use, the card is marked `used` and removed from subsequent prompt history.
- Normal SmartAggregator behavior works unchanged when no card is active.
- Card prompt block respects strict size limits.
- Raw chat and raw copied pages are not persisted or injected.
- Operator can rate whether the card helped after use.
- Tests cover lifecycle, dedupe/upsert, prompt injection, purge-after-use, size limits, and disabled/no-card behavior.

## Deferred Future Ideas

- Optional local extractor to propose hooks/counterpoints/triggers from clipboard text.
- Clipboard-to-draft hotkey.
- FTS5 trigger search for explicit PTT commands.
- PTT/voice activation after deterministic Agenda/Cohost activation proves valuable.
- Pre-stream topic pack builder.
- Vector search after card volume justifies it.
- Profile-specific editorial policies.
- Multi-card ranking with operator confirmation.
- News-specific card type with timestamp/source/caution policy.
- Adaptive streamer memory from card usefulness ratings.
