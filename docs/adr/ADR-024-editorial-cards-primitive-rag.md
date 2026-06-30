# ADR-024: Editorial Cue Cards are a Primitive RAG — Reference / Informational

**Date**: 2026-06-29
**Status**: Reference / informational (no code change — names what already ships)
**Branch**: `maintenance/big-file-audit-small-fixes-20260629`
**Author**: Claude Code orchestrator (big-file audit pass)
**Scope**: Reference only. Documents that OpenCohost's Editorial Cue Cards subsystem already *is* a small, lexical RAG, maps its parts to the canonical RAG anatomy, and records the bridge points to a future semantic/engram-simulado upgrade. No behavior change.

---

## Why this exists

The team keeps describing Editorial Cue Cards as "a way to feed Kira prep notes." That is exactly right — and it has a name. **Recuperar contexto relevante de un store y aumentar el prompt antes de generar es la definición de RAG** (Retrieval-Augmented Generation). The cards subsystem does precisely that, just with a *lexical* retriever instead of a dense/embedding one. This ADR names it so we stop reinventing the vocabulary, and so the obvious upgrades (semantic retrieval, top-k, auto-authoring) land as deliberate RAG improvements rather than ad-hoc tweaks.

> **TL;DR** — Editorial Cards = corpus (operator-authored) + lexical retriever (token-overlap) + prompt augmentation + the LLM. That is a RAG. A *primitive* one, because retrieval is sparse/lexical, the corpus is hand-written, and cards carry an ARMED→ACTIVE lifecycle.

---

## 1. Qué son las cards (data shape + lifecycle)

An **Editorial Cue Card** is a structured, operator-authored unit of context for *one* future Kira turn. Raw chat and raw copied pages are rejected at the model boundary so they can never leak into persistence or the prompt (`editorial_cards.py:96-98`).

**Data shape** — `EditorialCard` (`editorial_cards.py:69-88`):

| Field | Role |
|---|---|
| `topic` | The subject; what the retriever scores against (`editorial_cards.py:73`) |
| `summary` | Bounded factual context (≤1200 chars) |
| `streamer_take` | The operator's angle/opinion Kira should carry |
| `counterpoints` | Optional opposing angles (≤8 items) |
| `discussion_hooks` | Optional conversation openers (≤8 items) |
| `triggers` | Extra match phrases — a trigger whose tokens are all present forces a high match score (`editorial_cards.py:78`) |
| `topic_slug` | Stable dedupe key (one card per slug) |
| `status` | Lifecycle state (see below) |

**Lifecycle** — `EditorialCardStatus` (`editorial_cards.py:25-33`):

```
DRAFT ──arm()──► ARMED ──activate_for_topic()──► ACTIVE ──mark_used()──► USED
                   ▲                                                       │
                   └──────────────── rearm() ──────────────────────────────┘
                   (EXPIRED via is_expired()/disable() from any non-USED state)
```

- **ARMED** (`store.arm()`, `editorial_cards.py:239-248`) — eligible for retrieval. The retriever only ever looks at armed, non-expired cards (`list_armed()`, `editorial_cards.py:324-333`).
- **ACTIVE** (`store.activate_for_topic()`, `editorial_cards.py:250-264`) — at most one card is active at a time; this is the card whose context the agenda path will actually inject.
- **USED** (`store.mark_used()`, `editorial_cards.py:266-282`) — consumed after a successful one-turn generation; bumps `use_count`.

The direct (host-query) path is **non-consuming**: it retrieves and injects an ARMED card's context without changing status, so the agenda path can still attach it later (`editorial_agenda_bridge.py:117-134`).

---

## 2. Anatomía RAG mapeada

The classic RAG pipeline is **Corpus → Retriever → Augmentation → Generation**. Every stage already exists here:

| RAG stage | OpenCohost implementation | Where |
|---|---|---|
| **Corpus / Store** | `EditorialCardStore` — SQLite-backed cards, queried via `list_armed()` for the retrievable set | `editorial_cards.py:172-333` |
| **Retriever** | `match_score()` (token-overlap, lexical) + `select_card()` (≥0.8 threshold, gap/short-title guards, returns a single best card or None) | `editorial_matching.py:96-122`, `:125-200` |
| **Augmentation** | `EditorialCard.to_prompt_block()` renders a bounded `<editorial_context>` JSON block; the bridge resolves it for the active/armed card | `editorial_cards.py:144-169`, `editorial_agenda_bridge.py:71-134` |
| **Injection point** | `direct_editorial_context_provider(contexto)` → `editorial_block` appended to the prompt: `enriched = f"{contexto}\n\n{editorial_block}"` | `llm_engine.py:1063-1064` (provider call at `:1055-1061`) |
| **Generation** | The LLM (Ollama) consumes the augmented prompt and produces Kira's turn | downstream of `llm_engine.py:1064` |

The retriever has two surfaces, both lexical:
- **Direct path** — `resolve_direct_context(query_text)` picks the best ARMED card for a host query (`editorial_agenda_bridge.py:117-134`), feeding `llm_engine.py`'s `direct_editorial_context_provider`.
- **Agenda path** — `auto_attach(topic)` matches an ARMED card to an incoming agenda topic and links it (`editorial_agenda_bridge.py:79-115`), later resolved by `resolve_prompt_block()` for the ACTIVE card only (`:71-77`).

---

## 3. Justificación TÉCNICA — por qué ES un RAG

RAG = **retrieve relevant context from a store, then augment the generation prompt with it.** That is, verbatim, what this subsystem does:

1. **Retrieve** — given a topic/query, score candidate cards and select the best (`select_card`, `editorial_matching.py:125-200`).
2. **Augment** — serialize the chosen card into the prompt (`to_prompt_block` → `editorial_block`, appended at `llm_engine.py:1063-1064`).
3. **Generate** — the LLM produces the turn conditioned on that augmented prompt.

It is **sparse retrieval + augmentation, without an embedding step**. The differences from a "modern" RAG are *implementation choices at the retriever and corpus, not a change in category*:

| Dimension | This subsystem (primitive) | "Modern" RAG |
|---|---|---|
| **Retrieval** | Lexical — token-overlap / Jaccard-ish ratio over normalized tokens, plus trigger short-circuit (`match_score`, `editorial_matching.py:96-122`) | Dense — embedding similarity (cosine over vectors) |
| **Index** | None — linear scan over `list_armed()` cards | Vector index (FAISS / pgvector / HNSW) |
| **Corpus origin** | Operator-authored, structured, validated; raw chat/page rejected (`editorial_cards.py:96-98`) | Scraped/chunked documents, embedded |
| **Unit selection** | Single best card with ambiguity guards (gap ≥ 0.1, short-title guard); returns None rather than guess (`editorial_matching.py:188-200`) | top-k chunks, often re-ranked |
| **Lifecycle** | Stateful ARMED→ACTIVE→USED with single-active invariant and non-consuming direct reads | Usually stateless retrieval |

So: same anatomy, simpler organs. It is a RAG whose retriever happens to be `O(n)` lexical matching instead of vector search. Calling it "just prompt enrichment" undersells it; calling it "a vector RAG" oversells it. It is a **primitive RAG** — and naming it that makes the upgrade path (Section 5) legible.

---

## 4. Justificación NO técnica — la analogía

Pensá en un **conductor de TV o radio** que prepara **fichas (cue cards)** antes del programa. Cada ficha tiene un tema, un resumen, su opinión ("mi bajada"), un par de contraargumentos y ganchos para arrancar la charla. Durante el vivo, cuando surge ese tema, el conductor **saca la ficha justa para ese momento** y la usa — sin que un productor se la dicte palabra por palabra al oído.

Eso es exactamente lo que hacen las Editorial Cue Cards con Kira:
- El **operador** escribe las fichas antes (DRAFT) y las deja listas (ARMED).
- Cuando llega el tema, el sistema **elige la ficha correcta** (retriever) y se la **pasa a Kira** (augmentation) para *esa* respuesta.
- Kira responde con ese contexto, la ficha queda **USED**, y la charla sigue.

El operador prepara el material; el sistema saca la ficha indicada en el momento indicado. Nadie le dicta a Kira en vivo — esa es justamente la gracia de un RAG: el contexto correcto aparece solo, recuperado del montón.

---

## 5. Áreas de mejora a futuro

Naming it a RAG makes the roadmap obvious — each item below is a known RAG upgrade. **Bridge** marks the ones that feed directly into the future **engram simulado** track (cards as a self-populating, semantic memory rather than a hand-written deck).

| Improvement | Today | Upgrade | engram-simulado bridge? |
|---|---|---|---|
| **Semantic retrieval** | Token-overlap lexical match (`match_score`) — misses paraphrase/synonyms ("GTA 6" vs "Rockstar's new game") | Embeddings + cosine similarity, lexical as fallback | **Bridge** — shared embedding store is the backbone of a simulated memory |
| **Top-k vs single-card** | `select_card` returns exactly one card or None (`editorial_matching.py:125-200`) | Retrieve top-k, re-rank, optionally fuse multiple cards into the block | Indirect |
| **Auto-authoring of cards** | 100% operator-written via `create_or_update_card` (`editorial_agenda_bridge.py:29-50`) | Derive candidate cards from the live conversation/agenda automatically | **Bridge (direct)** — this *is* the engram-simulado idea: memory that writes itself |
| **Freshness / expiry** | Optional `expires_at` + `is_expired()` only (`editorial_cards.py:136-142`); no decay/recency in scoring | Recency-weighted scoring, auto-expire stale topics, freshness boost | Indirect |
| **Match evaluation** | `EditorialCardRating` captures post-use useful/not-useful (`editorial_cards.py:43-66`) but does not feed retrieval | Close the loop: use ratings to tune thresholds / re-rank, measure retrieval precision | **Bridge** — rating signal trains the simulated memory's relevance |

Notes:
- The **0.8 threshold** and **0.9 trigger floor** (`editorial_matching.py:111-119`, `:150`) are hand-tuned constants. A semantic retriever would re-baseline these — treat them as lexical-era defaults, not invariants.
- The retriever is currently a **linear scan** over armed cards. That is fine at deck-sized corpora; an embedding index only becomes necessary once auto-authoring inflates the corpus (the engram-simulado regime), which is the natural trigger to introduce a vector store.

---

## Consequences

- **Positive**: shared vocabulary. "Editorial Cards = primitive RAG" lets us discuss retrieval quality, corpus growth, and augmentation budget with the standard RAG toolbox, and frames the engram-simulado track as "swap the lexical retriever for a semantic one + let the corpus author itself."
- **No code change**: this ADR describes shipping behavior as of the branch above. The `<editorial_context>` block, lifecycle, and lexical matcher are unchanged.
- **Forward reference**: the *engram simulado* track named here is a future direction, not an existing track. The bridge rows in Section 5 are where it would plug in.

---

## Related code

- `opencohost/core/editorial_cards.py` — `EditorialCard`, `EditorialCardStore`, lifecycle, `to_prompt_block`
- `opencohost/core/editorial_matching.py` — `match_score`, `select_card` (lexical retriever)
- `opencohost/core/editorial_agenda_bridge.py` — `resolve_direct_context`, `auto_attach`, `resolve_prompt_block`
- `opencohost/core/llm_engine.py:1051-1066` — injection site (`direct_editorial_context_provider` → `editorial_block`)
