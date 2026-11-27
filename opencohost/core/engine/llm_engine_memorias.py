"""
opencohost/core/engine/llm_engine_memorias.py
""" 
import dataclasses
import re
import threading
import time
from typing import Optional

from opencohost.config import settings as _settings
from opencohost.config.storage import atomic_write_text
from opencohost.core.memory import episodic_merge
from opencohost.core.memory.pasted_image import strip_pasted_image_block
from opencohost.core.memory.memoria_store import (
    MEMORIAS_MAX_PINNED_INJECT,
    STRONG_LEXICAL_SHARED_TOKENS,
    MemoriaStore,
    _SCORING_STOPWORDS,
    _append_within_budget,
    _injection_text,
    _significant_tokens,
    is_correction_turn,
    is_recall_question,
    is_topical_recall_question,
    recall_topic_anchors,
)

# Module object, not names off it: `_eng.X` resolves at CALL time, so the suite's
# monkeypatches on llm_engine are seen. Safe at module level in THIS direction --
# llm_engine is already in sys.modules when it imports us.
#
# `Optional` and `MemoriaStore` are imported directly above and NOT routed through
# `_eng` on purpose: both are used in ANNOTATIONS, which evaluate at class-body
# time during llm_engine's PARTIAL import. `_eng.X` there only resolves for names
# bound above llm_engine's mixin-import line -- true for these two today, by luck,
# and silently false the day that line moves. Nobody monkeypatches either, and
# memoria_store does not import llm_engine, so a direct import has no cycle.
# `mixin_freevar_audit.py` fails the build on any `_eng` outside a method body.
from opencohost.core import llm_engine as _eng
from opencohost.core.engine import turn_timing

_SESSION_TOPICS_INIT_LOCK = threading.Lock()
_SESSION_TOPICS_MAX_PENDING = 8
_DURABLE_FACT_STATUSES = frozenset({"promoted", "curated", "imported"})
_CROSS_LANG_SEMANTIC_MIN_SCORE = 0.55
_LEDGER_CONTEXT_PREFIX_RE = re.compile(r"^\s*(?:contexto|context)\s*:\s*", re.IGNORECASE)
_WEAK_CORRECTION_VERBS: frozenset[str] = frozenset({
    "use", "uses", "using", "used",
    "play", "plays", "playing", "played",
    "live", "lives", "living", "lived",
    "work", "works", "working", "worked",
    "have", "has", "having", "had",
    "run", "runs", "running", "ran",
    "prefer", "prefers", "preferred", "preferring",
    "like", "likes", "liked", "liking",
    "want", "wants", "wanted", "wanting",
    "get", "gets", "got", "getting",
    "make", "makes", "made", "making",
    "take", "takes", "took", "taken", "taking",
    "keep", "keeps", "kept", "keeping",
    "go", "goes", "went", "going", "gone",
    "uso", "usa", "usan", "usaba", "usando",
    "vivo", "vive", "viven", "vivia", "viviendo", "vivi",
    "juego", "juega", "juegan", "jugaba", "jugando", "jugue",
    "tengo", "tiene", "tienen", "tenia", "tuve", "teniendo",
    "trabajo", "trabaja", "trabajan", "trabajaba", "trabajando", "trabaje",
    "gusta", "gustan", "gusto", "gustaba",
    "quiero", "quiere", "quieren", "queria", "quise",
    "hago", "hace", "hacen", "hacia", "hice", "haciendo",
    "voy", "va", "van", "iba", "fui",
})


def _correction_anchors(text: str) -> set[str]:
    """Extract non-stopword topic anchors after stripping ledger ``context:`` labels."""
    stripped = _LEDGER_CONTEXT_PREFIX_RE.sub("", text or "")
    return set(_significant_tokens(stripped)) - _SCORING_STOPWORDS


def _correction_anchor_rank(corr_anchors: set[str], cand_text: str) -> Optional[tuple[int, int]]:
    """Score correction anchor overlap, rejecting single generic-verb matches.

    Requires ``>= 2`` shared non-stopword anchors OR ``>= 1`` strong (non-generic-verb)
    shared anchor, returning ``(strong_count, total_shared)`` for ranking.
    """
    shared = corr_anchors & _correction_anchors(cand_text)
    if not shared:
        return None
    strong = len(shared - _WEAK_CORRECTION_VERBS)
    if len(shared) >= 2 or strong >= 1:
        return (strong, len(shared))
    return None


def _digest_fragment_key(clean_user: str) -> Optional[str]:
    """Identity of an evicted user turn, shared with the session topic index."""
    try:
        from opencohost.core.context.session_topics import fragment_key

        return fragment_key(clean_user)
    except Exception:
        return None


class MemoriaCaptureMixin:
    
    @staticmethod
    def _summary_recall_allowed(contexto: str, recall_stats: Optional[dict]) -> bool:
        """memory-grounding T6: may this turn use the summary (recency) branch?

        Needs ALL of: a meta recall query; the M1 flag on (default OFF, the
        stored summaries are keyword bags); no topical anchor (a topical
        question like "the Tesla we discussed" must miss honestly, never get
        unrelated summaries); and a v5 decision that does not contradict it
        (ACCEPTED_*, or none because v5 did not run).
        """
        if not _settings.MEMORY_SUMMARY_RECALL_ENABLED:
            return False
        if not _eng.is_meta_recall_query(contexto):
            return False
        if recall_topic_anchors(contexto):
            return False
        decision = (recall_stats or {}).get("decision")
        if decision in (None, "", "DISABLED"):
            return True
        return str(decision).startswith("ACCEPTED")

    @staticmethod
    def _unanchored_meta_recall_facts_allowed(contexto: str) -> bool:
        """When summary recall is disabled, serve durable facts on unanchored meta-recall."""
        if _settings.MEMORY_SUMMARY_RECALL_ENABLED:
            return False
        return bool(_eng.is_meta_recall_query(contexto) and not recall_topic_anchors(contexto))

    def _durable_meta_recall_lines(
        self, profile_id: str, primary_rows, *, lang: Optional[str] = None,
    ) -> tuple[list[str], int]:
        """Pinned + newest durable non-summary lines (never raw drafts or summaries)."""
        candidates = [
            r for r in primary_rows
            if r["status"] in _DURABLE_FACT_STATUSES and not self._is_summary_row(r)
        ]
        if not candidates and lang is not None:
            all_rows = self._get_memoria_store().list_injection_candidates(profile_id, lang=None)
            candidates = [
                r for r in all_rows
                if r["status"] in _DURABLE_FACT_STATUSES and not self._is_summary_row(r)
            ]
        if not candidates:
            return [], 0
        lines = _eng.build_recency_lines(candidates)
        pinned_count = min(
            sum(1 for r in candidates if r["pinned"]),
            MEMORIAS_MAX_PINNED_INJECT,
            len(lines),
        )
        return lines, pinned_count

    def _cross_lang_semantic_fallback(
        self,
        profile_id: str,
        lines: list[str],
        primary_rows,
        fact_query_vec,
    ) -> int:
        """Rescue high-cosine (>= 0.55) durable non-summary facts from other languages."""
        all_rows = self._get_memoria_store().list_injection_candidates(profile_id, lang=None)
        fb_scores, _ = self._fact_semantic_scores(
            profile_id,
            all_rows,
            fact_query_vec,
            lang=None,
            ignore_orphans=False,
            reconcile_rows=all_rows,
        )
        if not all_rows or not fb_scores:
            return 0
        primary_ids = {r["id"] for r in primary_rows}
        candidates = [
            r for r in all_rows
            if r["id"] not in primary_ids
            and r["status"] in _DURABLE_FACT_STATUSES
            and not self._is_summary_row(r)
        ]
        if not candidates:
            return 0
        matched = [
            (fb_scores[r["id"]], r)
            for r in candidates
            if fb_scores.get(r["id"], -1.0) >= _CROSS_LANG_SEMANTIC_MIN_SCORE
        ]
        if not matched:
            return 0
        matched.sort(key=lambda pair: pair[0], reverse=True)
        used = sum(len(line) for line in lines) + max(0, len(lines) - 1)
        added = 0
        for _, row in matched[:3]:
            before = len(lines)
            used = _append_within_budget(
                lines, used, _injection_text(row), _settings.MEMORIAS_MAX_INJECT_CHARS,
                clip_to_remaining=True,
            )
            if len(lines) > before:
                added += 1
        return added

    @staticmethod
    def _is_summary_row(row) -> bool:
        """T3: identify session summary rows that must only surface on meta-recall."""
        try:
            return row["status"] == "summary"
        except Exception:
            return getattr(row, "status", None) == "summary"

    @staticmethod
    def _sanitize_history_context(context: str) -> str:
        """Strip obvious prompt-injection attempts from chat context."""
        lowered = context.lower()
        for marker in _eng.INJECTION_MARKERS:
            if marker in lowered:
                return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", context)[:300]
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", context)[:800]

    def _get_memoria_store(self) -> MemoriaStore:
        with self._memoria_store_lock:
            if self._memoria_store is None:
                self._memoria_store = _eng.MemoriaStore(_eng.MEMORIAS_DB)
            return self._memoria_store
        
    def _build_memoria_draft(
        self,
        user_content: str,
        asst_content: str,
        *,
        source: Optional[str],
        private,
        recall_turn: Optional[bool] = None,
    ) -> Optional[tuple[str, str, str, str, str]]:
        """Pure, no-I/O: full gate chain + draft builder for one (user,
        assistant) pair, or None if any gate fails.

        Single source of truth for R1-R4/RC-1, shared by eviction-capture
        (T3, above), B1 capture-at-commit (memoria_quality_20260717), and F4
        flush (slice 4 — close-flush and profile-switch both iterate live-window
        pairs through this same chain instead of duplicating gate logic). MUST
        be called while _history_lock is held — reads the pair's own tags
        (state-at-event), never a switch's current state. Order: source
        allowlist -> not agenda-sentinel -> MEMORIAS_ENABLED -> private tag is
        False -> profile_id set -> significant-token minimum (is_capturable, via
        derive_stable_key, LAST — a signal gate, never a substitute for
        provenance, Judge-B forward slice 2 N1). Returns (profile_id,
        stable_key, title, content, signature) or None. C1: content comes from
        _build_memoria_content (24-word user budget), NOT the 8-word digest
        ledger line; the Kira side is never passed (owner-only-drafts).
        """
        if source not in _eng._DIGEST_CAPTURE_SOURCES:
            return None
        if user_content.startswith("[agenda segura"):
            return None
        # S3: OCR'd text from a pasted image is never memory. An image-only
        # turn strips to "" and dies at the significant-token gate below.
        user_content = strip_pasted_image_block(user_content)
        if not _eng.MEMORIAS_ENABLED:
            return None
        if private is not False:
            return None
        profile_id = self._current_profile_id
        if profile_id is None:
            return None
        # C1 (memoria_quality_20260717): user-side-alone signal gate — a pair
        # whose USER turn is a greeting/filler (< 2 significant tokens) dies
        # even when Kira's reply is rich. Distinct from is_capturable below,
        # which scores the COMBINED pair >= 3: a verbose Kira answer must not
        # drag a contentless "hola" into a stored memoria.
        if _eng.significant_token_count(user_content) < 2:
            return None
        # NO meta-recall veto here, deliberately — do not re-add one.
        #
        # It was added and then removed on 2026-08-14. The idea was that a
        # recall question is a READ of memory and should not become memory, so
        # `is_meta_recall_query` was consulted here. Two blind reviews killed
        # it, and the measurements agreed: that predicate was written as an
        # INJECTION ROUTER, where a false positive costs one turn's retrieval
        # quality. Here it costs the memory FOREVER — all three capture paths
        # (commit, eviction, close-flush) share this builder, so a vetoed pair
        # never reaches the promotion judge at all.
        #
        # It over-matched badly, and no length threshold rescues it:
        #   "quiero que recuerdes que mi cumpleaños es el 3 de mayo"  8 tokens
        #   "el juego que hablamos ayer lo compro mañana"             7 tokens
        #   "no me acuerdo si te lo conté, pero firmé con el sponsor" 10 tokens
        # The first is an EXPLICIT request to remember. All three carry durable
        # facts, and all three matched. Bare questions score 2-5, so any cut
        # that catches them also catches the request-to-remember.
        #
        # The owner's actual complaint ("guarda memorias de casi todo") was
        # never about the row — it was about being TOLD. That is fixed where it
        # belongs, by moving the notice to the promotion sweep. A recall
        # question still gets captured, still goes to the judge, and the judge
        # still rejects it silently; the owner simply never hears about it.
        # The judge is the right arbiter: of the 7 meta-recall rows in the live
        # store it kept 2, which a regex would have destroyed.
        # C1: skip canned guardrail-fallback assistant lines. D4 commits
        # guardrail-blocked exchanges to history so they are not lost, but the
        # fallback line itself carries zero memory signal. Literal match against
        # the active locale's fallback set (the same lines D4 actually speaks).
        if (asst_content or "").strip() in _eng.i18n_active.guardrail_fallback_lines():
            return None
        # owner-only-drafts (2026-10-18, owner decision): a draft is the
        # owner's words alone, on EVERY turn. Kira's reply is generated, not
        # observed: on 2026-10-17 a normal turn stored her invented CV and a
        # "Sí, busqué eso por ti" that never ran. Memory T7 already did this for
        # recall turns only; the Kira side is now dropped for all of them, so
        # content, title, signature and key all come from the user text and all
        # three capture paths (commit, eviction, flush) agree. The pair stays
        # capturable (no veto, see above). *recall_turn* is kept for callers
        # and no longer changes the result.
        del recall_turn
        asst_content = ""
        signature_text = f"{user_content} {asst_content}"
        stable_key = _eng.derive_stable_key(profile_id, signature_text)
        if stable_key is None:
            return None
        return (
            profile_id, stable_key, _eng.build_title(signature_text),
            self._build_memoria_content(user_content, asst_content),
            _eng.build_signature(signature_text),
        )
        
    def _resolve_turn_lang(self, lang: Optional[str] = None) -> str:
        """Primary language subtag for this turn: explicit *lang*, then the
        active profile's locale, then the active i18n bundle, else 'unknown'."""
        from opencohost.core.memory.memoria_store import normalize_memoria_lang

        if lang is not None:
            if str(lang).strip().lower() == "unknown":
                return "unknown"
            norm = normalize_memoria_lang(str(lang))
            if norm != "unknown":
                return norm
        raw_profile_locale = getattr(self, "_current_profile_locale", None)
        if raw_profile_locale:
            norm = normalize_memoria_lang(str(raw_profile_locale))
            if norm != "unknown":
                return norm
        try:
            active_mod = getattr(_eng, "i18n_active", None)
            if active_mod and hasattr(active_mod, "get_active_bundle"):
                code = active_mod.get_active_bundle().code
            else:
                from opencohost.i18n import active as _i18n_act
                code = _i18n_act.get_active_bundle().code
            return normalize_memoria_lang(code)
        except Exception:
            return "unknown"

    def _dispatch_switch_flush(
        self,
        drafts: list[tuple[str, str, str, str, str]],
        summary_profile_id: str | None = None,
        summary_titles: list[str] | None = None,
        *,
        lang: Optional[str] = None,
    ) -> None:
        """F4 (task 4.15) — dispatch profile-switch flush upserts to a
        dedicated worker thread, fire-and-forget (RC-2/RC-3).

        Runs off the Tk thread with NO time budget (task 4.16 — unlike
        close-flush, there IS more UI to protect, so a slow disk must never
        stall it; bounding by thread placement instead of wall-clock is
        sufficient here since nothing is waiting on this thread).

        W2a (memoria_recall_20260718): also carries the departing profile's
        session-summary payload, written on the SAME worker thread after the
        draft upserts. A worker is still spawned when there are no drafts to
        flush but enough tracked titles to summarize.
        """
        departing_lang = getattr(self, "_departing_switch_locale", None)
        self._departing_switch_locale = None
        flush_lang = self._resolve_turn_lang(lang if lang is not None else departing_lang)
        summary_titles = summary_titles or []
        if not drafts and len(summary_titles) < _eng.MEMORIAS_SUMMARY_MIN_TITLES:
            return
        threading.Thread(
            target=self._run_switch_flush,
            args=(drafts, summary_profile_id, summary_titles, flush_lang),
            daemon=True,
        ).start()
        
    def _write_session_summary(
        self, profile_id: str | None, titles: list[str], lang: Optional[str] = None
    ) -> None:
        """W2a (memoria_recall_20260718) — write ONE mechanical (NO LLM) session
        summary memoria for *profile_id* from this session's captured *titles*.

        Runs on the close-flush (calling) thread and the switch-flush worker
        thread — the same bounded, fail-open teardown paths it rides — so it
        must never call the model (EngineHost.stop has a 2s flush budget; a
        profile switch must never block on inference) and never crash the
        caller. The write itself is a single bounded upsert (insert_summary,
        MemoriaStore.WRITE_TIMEOUT_SECONDS); it holds no internal wall-clock
        budget — on the close path flush_memorias gates the call on remaining
        budget (R4), so this durable-tier write never pushes app close past it.

        Tiering (the 200-row MEMORIAS_PROFILE_CAP): per-turn drafts are the
        EXPENDABLE pool (pruned oldest-first); this status='summary' row is the
        DURABLE tier the W2b meta-router surfaces on recall queries, capped
        separately by MemoriaStore._prune_summaries. Fewer than
        MEMORIAS_SUMMARY_MIN_TITLES captured memorias -> no summary.

        Silent by construction: it emits NO 'memoria_captured' notice, because
        this runs at teardown / off the Tk thread. (It used to say "unlike
        _capture_memoria" — stale since 2026-08-14: capture is silent too now,
        and the only emitter is the promotion sweep.) The summary row is
        written via insert_summary — never re-entering _session_memoria_titles
        — so it is never itself re-summarized.
        """
        if not profile_id:
            _eng.logger.info("memoria session-summary skipped: no active profile")
            return
        clean = [t for t in titles if t and t.strip()]
        if len(clean) < _eng.MEMORIAS_SUMMARY_MIN_TITLES:
            # Paired with the success line below so a session close always
            # leaves EXACTLY ONE trace. Absence of all three then means this
            # method was never called at all — which is the hypothesis the
            # instrumentation exists to test.
            _eng.logger.info(
                "memoria session-summary skipped: titles=%d < min=%d",
                len(clean), _eng.MEMORIAS_SUMMARY_MIN_TITLES,
            )
            return
        content = "; ".join(clean)
        title = _eng.build_title(content) or "session summary"
        summary_lang = self._resolve_turn_lang(lang)
        try:
            self._get_memoria_store().insert_summary(
                profile_id, title, content, signature=_eng.build_signature(content), lang=summary_lang,
            )
        except Exception as exc:
            _eng.logger.warning(
                "memoria session-summary write failed (fail-open): %s profile_id=%s",
                type(exc).__name__, profile_id,
            )
        else:
            # Success used to be SILENT, which is why a six-week-old store with
            # 145 rows had ZERO status='summary' rows and nobody could tell
            # which of three things was happening: never called (teardown never
            # runs), called and failing, or called with too few titles. The
            # early returns above now have a matching negative line, so one
            # real session close answers it. Metadata only — the summary text
            # is derived memoria content and never goes to the log.
            _eng.logger.info(
                "memoria session-summary written: profile_id=%s titles=%d chars=%d",
                profile_id, len(clean), len(content),
            )
    
    def _commit_history(
        self,
        contexto: str,
        dialogo: str,
        *,
        source: str = "direct",
        history_text: Optional[str] = None,
    ) -> None:
        # AC3.3 (design-fase2.md §3 WU3 [v2]): any history commit invalidates an
        # in-flight/cached pregen — a reply generated against pre-commit history
        # must never speak. _speak_pregenerated already TOOK its own entry (via
        # _take_pregen_if_match at pop) BEFORE calling this, so its commit cannot
        # retro-invalidate the turn being spoken; it only kills OTHER stale
        # pregens. Bump the epoch (an in-flight worker's store dies on the epoch
        # check) AND clear a cached draft (the epoch alone does not cover it —
        # the pop-time take does not re-check the epoch).
        with self._prefetch_lock:
            self._prefetch_epoch += 1
            self._prefetched_agenda = None
            self._prefetch_done.clear()

        # F2 (session-2107-fixes): a canned guardrail/recovery line is spoken and
        # shown, but it is not conversation. Stored as an assistant turn it was
        # replayed into later prompts and the already-said block, and the model
        # parroted it. The blocked exchange stays out of history (pairs only).
        if (dialogo or "").strip() in (
            set(_eng.i18n_active.guardrail_fallback_lines())
            | set(_eng.i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES)
        ):
            return

        if source.startswith("kira-agenda"):
            safe_context = "[agenda segura: prompt interno omitido]"
        else:
            # agenda_ptt_commit_raw_text: when a caller supplies the honest
            # turn text (PTT path), store THAT instead of the raw contexto
            # (which, for agenda-driven turns, is the full prompt template).
            # Sanitizer pipeline is unchanged — it applies to whatever text
            # ends up in safe_context.
            safe_context = self._sanitize_history_context(history_text if history_text else contexto)

        committed_memoria_capture: Optional[tuple[str, str, str, str, str]] = None

        with self._history_lock:
            # D1 — eviction capture: before appending the new turn, check whether
            # the deque is at maxlen. If so, the oldest pair (user+assistant at
            # indices 0 and 1) will be auto-evicted. Capture them as one ledger line.
            # We only capture non-agenda turns; agenda turns already masked to
            # "[agenda segura…]" so their "eviction" produces no useful ledger line.
            maxlen = self.historial.maxlen  # capacity: (_max_turns [+ K-1 under v2 rotation]) * 2
            at_capacity = maxlen is not None and len(self.historial) >= maxlen
            # ADR-064 / F1: under the v2 layout the history rotates in chunks of
            # K pairs so the first history message (the KV-cache anchor) moves
            # only every K turns. Legacy: 1 pair, dropped by the deque itself.
            rotate_pairs = self._history_rotation_pairs()
            n_evict_pairs = max(1, min(rotate_pairs, len(self.historial) // 2)) if at_capacity else 0
            pending_memoria_captures: list = []
            capture_pairs = n_evict_pairs if not source.startswith("kira-agenda") else 0
            for pair_i in range(capture_pairs):
                u_idx = pair_i * 2
                # historial is guaranteed to hold pairs (user, assistant).
                if u_idx + 1 >= len(self.historial):
                    break
                u_entry = self.historial[u_idx]
                evicted_user_content = u_entry.get("content", "")
                evicted_asst_content = self.historial[u_idx + 1].get("content", "")
                # Skip capture when the evicted pair is agenda-origin (user slot is
                # the sentinel), to prevent real agenda replies from leaking into
                # the digest via the eviction path.
                evicted_is_agenda = evicted_user_content.startswith("[agenda segura")
                # D1 — source allowlist gate (fail-closed): only capture when the
                # evicted pair's own source tag is in _DIGEST_CAPTURE_SOURCES.
                # Missing or unknown source values are treated as not-capturable.
                evicted_source = u_entry.get("source")
                if evicted_source not in _eng._DIGEST_CAPTURE_SOURCES:
                    _eng.logger.debug(
                        "Eviction capture skipped: source=%r not in digest allowlist",
                        evicted_source,
                    )
                elif not evicted_is_agenda:
                    from opencohost.core.memory.memoria_store import strip_history_wrapper
                    # S3: the digest ledger is memory too: no pasted-image text.
                    clean_u = strip_pasted_image_block(strip_history_wrapper(evicted_user_content))
                    turn_key = f"{clean_u}::{evicted_asst_content}"
                    digested_keys = getattr(self, "_digested_turn_keys", None)
                    if not clean_u.strip():
                        pass  # image-only turn: nothing of the owner's to remember
                    elif digested_keys is None or turn_key not in digested_keys:
                        ledger_line = self._build_ledger_line(
                            clean_u,
                            evicted_asst_content,
                        )
                        self._memory_digest.append(ledger_line, key=_digest_fragment_key(clean_u))
                        if digested_keys is not None:
                            digested_keys.add(turn_key)

                    # T3 — memorias draft (R1-R4). _build_memoria_draft re-runs
                    # the source+agenda checks internally (redundant here, since
                    # the elif above already guarantees them — but that makes it
                    # the single, self-contained gate chain reusable by F4 flush
                    # (slice 4), which iterates the LIVE window's pairs instead
                    # of only the evicted one). is_capturable (via
                    # derive_stable_key) stays LAST — a signal/token-count gate,
                    # never a substitute for provenance (Judge-B forward, slice
                    # 2 N1).
                    #
                    # FIX2 (memoria_quality_20260717): skip the memoria re-capture
                    # when this pair was already captured at commit-time (B1) —
                    # a byte-identical re-upsert only bumps revision/updated_at
                    # and flaps the panel order. The digest ledger above is a
                    # SEPARATE in-memory feature and is always appended.
                    if not u_entry.get("mem_captured", False):
                        draft = self._build_memoria_draft(
                            evicted_user_content, evicted_asst_content,
                            source=evicted_source,
                            private=u_entry.get("private"),
                        )
                        if draft is not None:
                            pending_memoria_captures.append(draft)
            if rotate_pairs > 1 and at_capacity:
                # v2 chunk rotation: drop the K oldest pairs at once (after the
                # hooks above have seen every one of them).
                for _ in range(min(n_evict_pairs * 2, len(self.historial))):
                    self.historial.popleft()

            # Append-time privacy tagging (R2/R4): both pair entries carry the
            # CURRENT switch state at the moment they enter historial. Capture
            # later reads this tag from the EVICTED entry itself (state-at-event),
            # never the switch's state at eviction time — forward-only, no
            # retro-capture and no retro-hide of an already in-flight window.
            # Snapshotted ONCE here (not re-read per append): set_memorias_private
            # does not hold _history_lock, so a concurrent flip landing between
            # the two appends must not split the pair's tag (B-S1) — the flip
            # then correctly applies forward, to the next turn's pair instead.
            priv = self._memorias_private
            # T6(b) (ADR-061): collect up to the last 3 eligible captured pairs
            # BEFORE appending this turn (so none are lost on eviction).
            recent_captured_pairs = []
            for idx in range(len(self.historial) - 2, -1, -2):
                u_msg = self.historial[idx]
                a_msg = self.historial[idx + 1]
                if (
                    u_msg.get("source") in _eng._DIGEST_CAPTURE_SOURCES
                    and u_msg.get("private") is False
                ):
                    recent_captured_pairs.append((u_msg, a_msg))
                    if len(recent_captured_pairs) >= 3:
                        break
            previous_pair = (
                (self.historial[-2], self.historial[-1]) if len(self.historial) >= 2 else None
            )
            # clock-and-compact-polish-20261010 T3: the model active at commit;
            # a compact prompt drops entries from before the latest switch.
            model = getattr(self, 'current_model', None)
            committed_user_entry = {
                'role': 'user', 'content': safe_context, 'source': source,
                'private': priv, 'model': model,
            }
            self.historial.append(committed_user_entry)
            self.historial.append({
                'role': 'assistant', 'content': dialogo, 'source': source,
                'private': priv, 'model': model,
            })

            committed_memoria_capture = self._build_memoria_draft(
                safe_context, dialogo, source=source, private=priv,
            )
            correction_target = self._correction_target(
                committed_memoria_capture,
                safe_context,
                previous_pair,
                recent_pairs=recent_captured_pairs,
                anchored_only=bool(recent_captured_pairs),
            )
            immediate_correction_fallback = (
                self._correction_target(committed_memoria_capture, safe_context, previous_pair)
                if correction_target is None and previous_pair is not None
                else None
            )
            # WU1 shadow: allocate monotonic sequence and enqueue under _history_lock (pure RAM, < 5us).
            mem = getattr(self, "_memory", None)
            _rt = mem.runtime if mem is not None else getattr(self, "_memory_runtime", None)
            _run = mem.run_id if mem is not None else getattr(self, "_memory_run_id", None)
            # Gate misses are logged ONCE per reason per session so the owner
            # can see why nothing is written; reason codes only, never content.
            _v5_pid = getattr(self, "_current_profile_id", None)
            # S3: the evidence journal holds the owner's own words only; the
            # pasted-image block (OCR'd text) is stripped, and an image-only
            # turn writes nothing.
            _v5_user_text = strip_pasted_image_block(safe_context)
            if _rt is None or _run is None:
                self._memory_v5_note_once("skip", "no_runtime")
            elif source not in ("direct", "ptt", "owner-bundle"):
                self._memory_v5_note_once("skip", "source_not_eligible")
            elif priv is not False:
                self._memory_v5_note_once("skip", "private")
            elif _v5_pid is None:
                self._memory_v5_note_once("skip", "no_profile")
            elif not _v5_user_text.strip() and _v5_user_text != safe_context:
                self._memory_v5_note_once("skip", "image_only")
            if _rt is not None and _run is not None:
                if (
                    source in ("direct", "ptt", "owner-bundle")
                    and priv is False
                    and (_v5_user_text.strip() or _v5_user_text == safe_context)
                ):
                    pid = _v5_pid
                    if pid is not None:
                        try:
                            from datetime import datetime, timezone
                            from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot

                            shadow_occurred_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                            snap_u = CommittedTurnSnapshot(
                                committed_turn_id="",
                                profile_id=pid,
                                run_id=_run,
                                stream_sequence=0,
                                role="user",
                                source=source,
                                occurred_at=shadow_occurred_at,
                                content=_v5_user_text,
                                is_private=False,
                            )
                            snap_a = CommittedTurnSnapshot(
                                committed_turn_id="",
                                profile_id=pid,
                                run_id=_run,
                                stream_sequence=0,
                                role="assistant",
                                source=source,
                                occurred_at=shadow_occurred_at,
                                content=dialogo,
                                is_private=False,
                            )
                            if mem is not None:
                                mem.record_turn_exchange(snap_u, snap_a)
                            else:
                                assigned = _rt.record_turn_exchange(snap_u, snap_a)
                                if assigned:
                                    self._memory_stream_seq = assigned[1]
                        except Exception as exc:
                            self._memory_v5_note_once(
                                "drop",
                                "snapshot_construction_or_enqueue_exception",
                                exc_type=type(exc).__name__,
                            )
                            try:
                                if mem is not None:
                                    mem.record_drop(2, "snapshot_construction_or_enqueue_exception")
                                else:
                                    _rt.record_drop(2, "snapshot_construction_or_enqueue_exception")
                            except Exception:
                                pass

        for pending_memoria_capture in pending_memoria_captures:
            self._capture_memoria(*pending_memoria_capture)
        if committed_memoria_capture is not None:
            if correction_target is None and is_correction_turn(safe_context):
                correction_target = (
                    self._find_unjudged_draft_correction_target(
                        committed_memoria_capture, safe_context
                    )
                    or immediate_correction_fallback
                )
            if (
                correction_target is not None
                and self._supersede_memoria(committed_memoria_capture, *correction_target)
            ) or self._capture_memoria(*committed_memoria_capture):
                committed_user_entry["mem_captured"] = True
        # session-topic-folder-20261016: owner-only, off-turn-path, fail-open.
        # S3: both consumers are memory too, so they see the owner's words only.
        if _v5_user_text.strip() or _v5_user_text == safe_context:
            self._session_topics_submit(source, priv, _v5_user_text, dialogo)
            # owner-reflection-memory-20261015: same owner gate, RAM ring only, inert when off.
            self._owner_reflection_submit(source, priv, _v5_user_text, dialogo)

    # ------------------------------------------------------------------
    # session-topic-folder-20261016 (T2): capture into the in-session topic
    # index. Fed from the SAME owner-only gate as the memoria capture (never
    # from the raw history window); the MiniLM embed runs in a background
    # single-thread executor AFTER the reply, never on the turn path.
    # ------------------------------------------------------------------
    def _session_topics_index(self):
        with _SESSION_TOPICS_INIT_LOCK:
            idx = getattr(self, "_session_topics", None)
            if idx is None:
                from opencohost.core.context.session_topics import SessionTopicIndex

                idx = SessionTopicIndex(
                    assign_threshold=_settings.SESSION_TOPICS_ASSIGN_THRESHOLD,
                    retrieve_threshold=_settings.SESSION_TOPICS_RETRIEVE_THRESHOLD,
                    min_words=_settings.SESSION_TOPICS_MIN_WORDS,
                    max_topics=_settings.SESSION_TOPICS_MAX_TOPICS,
                    max_fragments=_settings.SESSION_TOPICS_MAX_FRAGMENTS,
                )
                self._session_topics = idx
                self._session_topics_seq = 0
                self._session_topics_pending = 0
                self._session_topics_executor = None
            return idx

    def _session_topics_retrieval_possible(self, mem) -> bool:
        """F3: capture embeds only when the turn's recall CAN produce a query
        vector (facts on, a fact index, a profile with fact embeddings), so no
        embed is spent where retrieval can never hit. Never raises."""
        try:
            if not _settings.MEMORY_FACTS_ENABLED:
                return False
            pid = getattr(self, "_current_profile_id", None)
            cache = getattr(getattr(mem, "fact_index", None), "cache_store", None)
            if not pid or cache is None:
                return False
            return cache.count_fact_embeddings(pid) > 0
        except Exception:
            return False

    def _session_topics_take_turn_vec(self, clean: str):
        """F2: the vector the turn's own recall computed for this same text
        (consumed once), or None."""
        from opencohost.core.context.session_topics import fragment_key

        stash = getattr(self, "_session_topics_turn_vec", None)
        self._session_topics_turn_vec = None
        if stash and stash[0] == fragment_key(clean):
            return stash[1]
        return None

    def _session_topics_submit(self, source, private, safe_context, dialogo) -> None:
        """Queue one owner exchange for the topic index. Never raises."""
        try:
            if not (_settings.SESSION_TOPICS_ENABLED and _settings.MEMORY_SEMANTIC_ENABLED):
                return
            if source not in _eng._DIGEST_CAPTURE_SOURCES or private is not False:
                return
            if not safe_context or safe_context.startswith("[agenda segura"):
                return
            mem = getattr(self, "_memory", None)
            worker = getattr(mem, "worker", None) if mem is not None else None
            if worker is None:
                return
            clean = _eng.strip_history_wrapper(safe_context)
            if len(clean.split()) < _settings.SESSION_TOPICS_MIN_WORDS:
                return
            vec = self._session_topics_take_turn_vec(clean)
            if vec is None and not self._session_topics_retrieval_possible(mem):
                return
            from opencohost.core.context.prompt_assembler import ledger_kira_summary

            # A recall reply is an answer from possibly absent memory: it must
            # not come back later as Kira's own "context" (self-poisoning).
            kira = "" if is_recall_question(clean) else ledger_kira_summary(dialogo)
            idx = self._session_topics_index()
            with _SESSION_TOPICS_INIT_LOCK:
                if self._session_topics_pending >= _SESSION_TOPICS_MAX_PENDING:
                    return
                self._session_topics_pending += 1
                self._session_topics_seq += 1
                seq = self._session_topics_seq
                generation = idx.generation
                try:
                    if self._session_topics_executor is None:
                        from concurrent.futures import ThreadPoolExecutor

                        self._session_topics_executor = ThreadPoolExecutor(
                            max_workers=1, thread_name_prefix="session-topics"
                        )
                    self._session_topics_executor.submit(
                        self._session_topics_job, worker, idx, clean, kira, seq, generation, vec
                    )
                except Exception:
                    # Nothing will run the job that decrements the counter.
                    self._session_topics_pending = max(0, self._session_topics_pending - 1)
                    raise
        except Exception as exc:
            _eng.logger.debug("session_topics submit skipped: %s", type(exc).__name__)

    def _session_topics_job(self, worker, idx, clean, kira, seq, generation, vec=None) -> None:
        try:
            if vec is None:
                # Fallback only: never wait on the worker lock a turn may need.
                vec = worker.embed_query(
                    clean, timeout_s=float(_settings.SESSION_TOPICS_EMBED_TIMEOUT_S), block=False
                )
            if vec and idx.add_fragment(vec, clean, kira, seq, generation=generation) is not None:
                self._session_topics_view_schedule()
        except Exception as exc:  # fail open: a missing fragment is harmless
            _eng.logger.debug("session_topics embed failed: %s", type(exc).__name__)
        finally:
            with _SESSION_TOPICS_INIT_LOCK:
                self._session_topics_pending = max(0, self._session_topics_pending - 1)

    def _session_topics_block(
        self, source, recall, history_snapshot, *, query_text=None, compact=False
    ) -> str:
        """Retrieval for this turn: pure dot products against in-memory
        centroids using the vector memory recall already computed (no embed,
        no I/O). Returns the ready ``<session_topics>`` block or "". Never raises.
        *query_text* lets commit reuse the turn's vector; *compact* means the
        assembler will drop the block (logged as ``dropped``)."""
        try:
            if not (_settings.SESSION_TOPICS_ENABLED and _settings.MEMORY_SEMANTIC_ENABLED):
                return ""
            if source not in _eng._DIGEST_CAPTURE_SOURCES:
                return ""
            from opencohost.core.context.session_topics import fragment_key, render_session_topics_block

            idx = getattr(self, "_session_topics", None)
            started = time.monotonic()
            # T3: the turn's own query vector (embedded once, T2) serves the
            # in-memory topic lookup even when fact ranking got none (cold
            # index, budget overrun); fact_query_vec stays preferred.
            vec = None
            if recall is not None:
                vec = getattr(recall, "fact_query_vec", None) or getattr(recall, "query_vec", None)
            if vec and query_text:
                self._session_topics_turn_vec = (fragment_key(_eng.strip_history_wrapper(query_text)), vec)
            topics = idx.topic_count() if idx is not None else 0
            if topics == 0:
                self._session_topics_log("miss", "empty", 0, started)
                return ""
            if not vec:
                self._session_topics_log("miss", "no_vec", topics, started)
                return ""
            in_window = {
                fragment_key(_eng.strip_history_wrapper(m.get("content", "")))
                for m in history_snapshot
                if isinstance(m, dict) and m.get("role") == "user"
            }
            with self._history_lock:
                in_window |= set(self._memory_digest.keys)
            hit = idx.query(vec, exclude_keys=in_window, max_return=_settings.SESSION_TOPICS_MAX_LINES)
            if hit is None:
                self._session_topics_log("miss", "no_match", topics, started)
                return ""
            if compact:
                self._session_topics_log("dropped", "compact_profile", topics, started)
                return ""
            block = render_session_topics_block(
                hit,
                ctx_label=_eng.i18n_active.ledger_context_label(),
                kira_label=_eng.i18n_active.ledger_kira_label(),
                max_lines=_settings.SESSION_TOPICS_MAX_LINES,
                max_chars=_settings.SESSION_TOPICS_MAX_CHARS,
            )
            self._session_topics_log(
                "hit", None, topics, started,
                fragments=hit.total_fragments, returned=len(hit.fragments), score=hit.score,
            )
            return block
        except Exception as exc:
            _eng.logger.debug("session_topics retrieval skipped: %s", type(exc).__name__)
            return ""

    @staticmethod
    def _session_topics_log(result, reason, topics, started, *, fragments=0, returned=0, score=None) -> None:
        """[SESSION_TOPICS] telemetry: counts and scores only, never any text."""
        parts = [f"result={result}"]
        if reason:
            parts.append(f"reason={reason}")
        parts.append(f"topics={topics}")
        if result == "hit":
            parts.append(f"fragments={fragments} returned={returned} score={score:.2f}")
        parts.append(f"ms={(time.monotonic() - started) * 1000:.1f}")
        _eng.logger.info("[SESSION_TOPICS] " + " ".join(parts))

    def _session_topics_reset(self) -> None:
        """Forget the session's topics and delete the view (clear_history /
        profile switch / memoria purge). Best effort, never raises."""
        try:
            idx = getattr(self, "_session_topics", None)
            if idx is not None:
                idx.reset()
            self._session_topics_turn_vec = None
            with _SESSION_TOPICS_INIT_LOCK:
                timer = getattr(self, "_session_topics_view_timer", None)
                self._session_topics_view_timer = None
            if timer is not None:
                timer.cancel()
            self._session_topics_remove_view()
        except Exception:
            pass

    def reset_session_topics(self) -> None:
        """Engine-facing reset for API routers (memoria purge/delete, profile delete).

        Also clears the owner-reflection ring and cancels a running job, so purged or
        deleted memories cannot be re-distilled from exchanges that predate the purge."""
        self._session_topics_reset()
        self._owner_reflection_reset()

    @staticmethod
    def _session_topics_remove_view() -> None:
        try:
            from pathlib import Path

            Path(_settings.SESSION_TOPICS_VIEW_FILE).unlink(missing_ok=True)
        except Exception as exc:
            _eng.logger.debug("session_topics view removal skipped: %s", type(exc).__name__)

    def _session_topics_startup_cleanup(self) -> None:
        """A view left by a previous session (or flag state) never outlives it."""
        self._session_topics_remove_view()

    def _session_topics_shutdown(self) -> None:
        """Teardown: flush the debounced view, stop the executor. Never raises."""
        try:
            if getattr(self, "_session_topics", None) is not None:
                self._session_topics_view_flush()
        except Exception:
            pass
        try:
            with _SESSION_TOPICS_INIT_LOCK:
                executor = getattr(self, "_session_topics_executor", None)
                self._session_topics_executor = None
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    # T4: debounced, best-effort session.md view. All failures are swallowed;
    # nothing here runs on the turn path (timer thread / background executor).
    def _session_topics_view_schedule(self) -> None:
        try:
            with _SESSION_TOPICS_INIT_LOCK:
                if getattr(self, "_session_topics_view_timer", None) is not None:
                    return
                timer = threading.Timer(
                    max(0.0, float(_settings.SESSION_TOPICS_VIEW_DEBOUNCE_S)), self._session_topics_view_write
                )
                timer.daemon = True
                timer.start()
                self._session_topics_view_timer = timer
        except Exception as exc:
            _eng.logger.debug("session_topics view schedule skipped: %s", type(exc).__name__)

    def _session_topics_view_write(self) -> None:
        try:
            with _SESSION_TOPICS_INIT_LOCK:
                self._session_topics_view_timer = None
            idx = getattr(self, "_session_topics", None)
            if idx is None or not _settings.SESSION_TOPICS_ENABLED:
                self._session_topics_remove_view()
                return
            from pathlib import Path

            path = Path(_settings.SESSION_TOPICS_VIEW_FILE)
            snapshot = idx.snapshot()
            if not snapshot:
                path.unlink(missing_ok=True)
                return
            from datetime import datetime, timezone
            from opencohost.core.context.session_topics import render_session_markdown

            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            atomic_write_text(path, render_session_markdown(snapshot, generated_at=stamp))
        except Exception as exc:
            _eng.logger.debug("session_topics view write skipped: %s", type(exc).__name__)

    def _session_topics_view_flush(self) -> None:
        """Cancel the pending timer and write now (tests, shutdown)."""
        with _SESSION_TOPICS_INIT_LOCK:
            timer = getattr(self, "_session_topics_view_timer", None)
            self._session_topics_view_timer = None
        if timer is not None:
            timer.cancel()
        self._session_topics_view_write()

    def _session_topics_flush(self, timeout: float = 5.0) -> None:
        """Wait until queued captures finished (tests, shutdown)."""
        executor = getattr(self, "_session_topics_executor", None)
        if executor is not None:
            executor.submit(lambda: None).result(timeout=timeout)

    def _correction_target(
        self,
        capture,
        user_text: str,
        previous_pair,
        *,
        recent_pairs=None,
        anchored_only: bool = False,
    ) -> Optional[tuple[str, str]]:
        """T6(b) (ADR-061) - when *capture* is an explicit correction, return
        ``(stable_key, title)`` for the target draft to supersede.

        Scans up to *recent_pairs* (last 3 eligible non-private captured pairs)
        for shared non-stopword topic anchor overlap first; falls back to
        immediate *previous_pair* only when *anchored_only* is False. MUST run
        while _history_lock is held (pure RAM only, never I/O)."""
        if capture is None or not is_correction_turn(user_text):
            return None
        corr_anchors = _correction_anchors(user_text)
        pairs = list(recent_pairs) if recent_pairs is not None else (
            [previous_pair] if previous_pair is not None else []
        )
        best_match: Optional[tuple[int, int, int, tuple[str, str]]] = None
        for idx, pair in enumerate(pairs):
            if pair is None:
                continue
            cand_user, cand_asst = pair
            if (
                not cand_user.get("mem_captured", False)
                or cand_user.get("private") is not False
                or cand_user.get("source") not in _eng._DIGEST_CAPTURE_SOURCES
            ):
                continue
            built = self._build_memoria_draft(
                cand_user.get("content", ""), cand_asst.get("content", ""),
                source=cand_user.get("source"), private=cand_user.get("private"),
            )
            if built is None or built[1] == capture[1]:
                continue
            anchor_rank = _correction_anchor_rank(corr_anchors, cand_user.get("content", ""))
            if anchor_rank is not None:
                rank = (anchor_rank[0], anchor_rank[1], -idx, (built[1], built[2]))
                if best_match is None or rank[:3] > best_match[:3]:
                    best_match = rank
        if best_match is not None:
            return best_match[3]
        if anchored_only or previous_pair is None:
            return None
        prev_user, prev_asst = previous_pair
        if not prev_user.get("mem_captured", False) or prev_user.get("private") is not False:
            return None
        prev = self._build_memoria_draft(
            prev_user.get("content", ""), prev_asst.get("content", ""),
            source=prev_user.get("source"), private=prev_user.get("private"),
        )
        if prev is None:
            return None
        return prev[1], prev[2]

    def _find_unjudged_draft_correction_target(
        self, capture: tuple[str, str, str, str, str], user_text: str,
    ) -> Optional[tuple[str, str]]:
        """Unlocked fallback: match explicit correction against unjudged non-private
        drafts in the store requiring >=2 shared anchors or >=1 non-generic anchor."""
        profile_id, committed_key, *_ = capture
        corr_anchors = _correction_anchors(user_text)
        if not corr_anchors:
            return None
        try:
            drafts = self._get_memoria_store().list_unjudged_drafts(profile_id, limit=25)
        except Exception:
            return None
        best: Optional[tuple[int, int, str, str, str]] = None
        for row in drafts:
            skey = row["stable_key"] or ""
            if not skey or skey == committed_key:
                continue
            anchor_rank = _correction_anchor_rank(corr_anchors, row["content"] or "")
            if anchor_rank is not None:
                updated = row["updated_at"] or ""
                candidate = (anchor_rank[0], anchor_rank[1], updated, skey, row["title"] or "")
                if best is None or candidate[:3] > best[:3]:
                    best = candidate
        return (best[3], best[4]) if best is not None else None

    def _supersede_memoria(
        self, capture: tuple[str, str, str, str, str], prev_key: str, prev_title: str,
    ) -> bool:
        """I/O: rewrite the previous draft with the correction (ADR-061).
        MUST be called AFTER _history_lock releases. False — nothing rewritten
        (the target was promoted/curated meanwhile, or the write failed open) —
        tells the caller to store an ordinary draft instead. Logs ids only."""
        profile_id, _stable_key, title, content, signature = capture
        try:
            result = self._get_memoria_store().supersede_draft(
                profile_id, prev_key, title, content, signature=signature,
            )
        except Exception as exc:
            _eng.logger.warning(
                "memoria correction supersede failed (fail-open): %s profile_id=%s",
                type(exc).__name__, profile_id,
            )
            return False
        if result is None:
            return False
        row_id, revision = result
        _eng.logger.info("memoria correction superseded id=%s revision=%s", row_id, revision)
        # The session summary must not keep the wrong title either.
        if title:
            with self._history_lock:
                if profile_id == self._current_profile_id:
                    titles = self._session_memoria_titles
                    if prev_title in titles:
                        titles[len(titles) - 1 - titles[::-1].index(prev_title)] = title
                    else:
                        titles.append(title)
                        if len(titles) > _eng._SESSION_MEMORIA_TITLES_CAP:
                            del titles[0]
        return True
                
    def _memory_v5_note_once(
        self, kind: str, reason: str, *, exc_type: Optional[str] = None
    ) -> None:
        """Owner-visible Memory v5 turn diagnostics, once per (kind, reason).

        ``kind="skip"`` (a gate was unmet) logs INFO; ``kind="drop"`` (a
        qualifying turn was lost) logs WARNING with the exception TYPE only.
        Goes through the OpenCohost logger so it reaches
        ``logs/opencohost_*.log``. Never carries turn content.
        """
        seen = getattr(self, "_memory_v5_logged_reasons", None)
        if seen is None:
            seen = set()
            self._memory_v5_logged_reasons = seen
        key = (kind, reason, exc_type)
        if key in seen:
            return
        seen.add(key)
        if kind == "drop":
            _eng.logger.warning(
                "memory_v5: drop reason=%s exc=%s", reason, exc_type or "none"
            )
        else:
            _eng.logger.info("memory_v5: %s reason=%s", kind, reason)

    def flush_memorias(self, budget_seconds: float = 2.0) -> None:
        """F4 (task 4.12) — bounded flush of the live memorias window on
        clean app close (R14).

        Snapshots eligible pairs under _history_lock (same gate chain as
        eviction capture via _collect_flush_drafts), releases the lock, then
        upserts each draft on the CALLING thread under a wall-clock budget.
        Running on the calling thread (sanctioned Tk-thread exception, RC-3
        — there is no more UI to protect after close, agenda_persistence.py
        precedent) is safe specifically because it is time-bounded: stops
        early and logs ONCE (count only, never content — RC-8) if the budget
        is exceeded. Fail-open throughout: never raises, never blocks close.
        """
        self._session_topics_shutdown()
        try:
            with self._history_lock:
                drafts = self._collect_flush_drafts()
                # W2a: snapshot this session's titles + profile under the same
                # lock, then clear (a double close never writes two summaries).
                summary_profile_id = self._current_profile_id
                summary_titles = list(self._session_memoria_titles)
                self._session_memoria_titles.clear()
                flush_lang = self._resolve_turn_lang()
        except Exception:
            _eng.logger.warning("memoria close-flush snapshot failed (fail-open)")
            return

        # R4: one wall-clock budget spans the WHOLE flush (drafts + summary), so
        # the durable-tier summary write below can never push close past it.
        deadline = _eng.time.monotonic() + budget_seconds
        if drafts:
            flushed = 0
            for profile_id, stable_key, title, content, signature in drafts:
                if _eng.time.monotonic() > deadline:
                    _eng.logger.warning(
                        "memoria close-flush budget exceeded, stopping early: flushed=%d remaining=%d",
                        flushed, len(drafts) - flushed,
                    )
                    break
                try:
                    self._get_memoria_store().upsert_draft(
                        profile_id, stable_key, title, content, signature=signature, lang=flush_lang
                    )
                    flushed += 1
                except Exception as exc:
                    _eng.logger.warning(
                        "memoria close-flush upsert failed (fail-open): %s profile_id=%s",
                        type(exc).__name__, profile_id,
                    )

        # W2a: ONE mechanical session summary after the draft flush (single
        # bounded upsert on the calling thread — same sanctioned Tk-thread
        # exception as the flush above, RC-3). R4: attempt it only while a
        # write's worth of budget remains — a close under a fully spent budget
        # skips it silently (the next switch/close boundary covers the session)
        # so the summary honours "never blocks close".
        if deadline - _eng.time.monotonic() > _eng._MEMORIA_SUMMARY_WRITE_BUDGET_SECONDS:
            self._write_session_summary(summary_profile_id, summary_titles, lang=flush_lang)
        else:
            _eng.logger.debug(
                "memoria close-flush budget spent, skipping session summary (profile_id=%s)",
                summary_profile_id,
            )
            
    def _capture_memoria(
        self, profile_id: str, stable_key: str, title: str, content: str, signature: str = "",
        *, provenance: Optional[str] = None, lang: Optional[str] = None,
    ) -> bool:
        """I/O: upsert a memoria draft. MUST be called AFTER _history_lock releases.

        Returns True on a successful upsert, False on the fail-open path (so
        capture-at-commit can flag a pair only when it actually persisted, and
        a failure leaves eviction/flush their retry role — FIX2).

        Fail-open (R5): any exception is logged (ids/type only, never title
        or content — RC-8) and swallowed. A memorias write must never crash
        the calling thread (engine worker loop / agenda speaker daemon).
        """
        # E1 (memoria_quality_20260717) used to fire
        # ui_callback("memoria_captured") here, on every fresh INSERT (hence
        # the `return_created` flag, now unused). Removed 2026-08-14: a
        # capture is an UNJUDGED DRAFT, and the keep/reject decision is
        # `promote_pending_drafts`, which runs once on the NEXT launch. In the
        # owner's live store 84 of 98 drafts were judge-rejected and hidden,
        # so the feed announced "Kira guardó una memoria" for rows that were
        # thrown away ~86% of the time, with an app restart between the claim
        # and the truth. The notice now lives at the sweep, where the outcome
        # is actually known.
        turn_lang = self._resolve_turn_lang(lang)
        try:
            extra = {"provenance": provenance} if provenance is not None else {}
            self._get_memoria_store().upsert_draft(
                profile_id, stable_key, title, content, signature=signature, lang=turn_lang, **extra,
            )
        except Exception as exc:
            _eng.logger.warning(
                # RC-8: never the stable_key — it is built from the pair's own
                # significant tokens, i.e. memory content.
                "memoria capture failed (fail-open): %s profile_id=%s",
                type(exc).__name__, profile_id,
            )
            return False
        # S4: a new draft re-arms a drained promotion sweep (after the quiet window).
        try:
            self._promotion_mark_dirty()
        except Exception:
            pass
        # W2a (memoria_recall_20260718): record this session's captured title
        # for the mechanical session summary. Brief _history_lock (no I/O — the
        # store write above already released it) keeps this append consistent
        # with the snapshot+clear in set_profile / flush_memorias. Tracked on
        # every persisted capture (eviction + commit), never for a summary row
        # (those bypass this method), so a summary is never re-summarized.
        #
        # R3 (cross-profile contamination): the upsert above ran UNLOCKED, so a
        # concurrent set_profile could have snapshot+cleared the titles and
        # swapped _current_profile_id between the draft build (this profile_id)
        # and this locked append. Attribute the title ONLY while its profile is
        # still the active one; on mismatch skip silently — a bounded,
        # correctly-attributed loss (the departing summary misses one late
        # title) rather than leaking it into the arriving profile's summary.
        if title:
            with self._history_lock:
                if profile_id == self._current_profile_id:
                    self._session_memoria_titles.append(title)
                    if len(self._session_memoria_titles) > _eng._SESSION_MEMORIA_TITLES_CAP:
                        del self._session_memoria_titles[0]
        return True

    def _auto_capture_or_update_memoria_internal(
        self, profile_id: str, stable_key: str, title: str, content: str, signature: str = "",
        *, provenance: Optional[str] = None, lang: Optional[str] = None,
    ) -> bool:
        """Resolve turn language and upsert draft memoria with locale isolation."""
        return self._capture_memoria(
            profile_id, stable_key, title, content, signature=signature,
            provenance=provenance, lang=lang,
        )

    

    def _anchor_in_memory(self, profile_id: str, anchors) -> Optional[bool]:
        """True/False when any stored row (drafts included) mentions an anchor;
        None when the lookup failed. Never logs content."""
        try:
            return self._get_memoria_store().any_row_mentions(profile_id, anchors)
        except Exception:
            return None

    def _nomatch_skip_reason(self, profile_id, contexto, weak_dropped, session_topics_present):
        """Why the no-match note must be withheld, or None when it may fire."""
        if weak_dropped:
            return "weak_dropped"
        if session_topics_present:
            return "session_topics"
        if self._anchor_in_memory(profile_id, recall_topic_anchors(contexto)) is not False:
            return "anchor_in_memory"
        return None

    def _build_memorias_injection_block(
        self,
        profile_id: str,
        contexto: str,
        episodes=None,
        recall_stats: Optional[dict] = None,
        fact_query_vec=None,
        lexical_min_shared: Optional[int] = None,
        session_topics_present: bool = False,
        lang: Optional[str] = None,
    ) -> str:
        """Slice 5 (R9) — memorias retrieval + injection for the sources in
        _MEMORIA_INJECT_SOURCES (direct + ptt as of candidate 1).

        MUST be called AFTER _history_lock releases (store I/O, bounded by
        the store's own READ_TIMEOUT_SECONDS). Fail-open to "" on any error
        — a retrieval failure must never break a turn (mirrors
        _capture_memoria). Eligibility (private=0 AND inactive=0, capped) is
        enforced by MemoriaStore.list_injection_candidates; pinned policy A
        (max 2 oldest-pinned, 220-char clip, top-k floor) by
        build_injection_lines. Each line is re-sanitized at build time and
        wrapped in the i18n memorias_block_open/close delimiters.

        ADR-059: *episodes* (v5 RetrievedEpisodeContext items from a
        successful recall) are appended AFTER the fact lines as compact
        lines inside the same budget; facts are never cut for them. With
        episodes=None the output is byte-identical to the lexical-only path.
        *recall_stats* (optional dict) receives lexical/episodes line counts.

        ADR-060: *fact_query_vec* (the turn's query embedding) switches the
        automatic top-k to the hybrid semantic+lexical rank over rows with a
        valid fact vector; without it, or with no valid vector (cold), the
        lexical top-k runs byte-identically. Stale fact vectors are rebuilt
        by a background reconcile, never here. *recall_stats* then also
        receives ``semantic`` and ``fact_fallback``.

        T3: *lexical_min_shared* (set only when the semantic side was rejected
        or timed out) raises the lexical top-k gate from the legacy 2 shared
        tokens; when that withholds every line, ``recall_stats["inject_skip"]``
        becomes ``lexical_only_weak``. Pinned rows are never affected.

        Judge round 1: the no-match note is decided on the matched lines only
        (pinned rows are ignored and the note follows them) and is withheld
        when the strict path dropped a lexical match, when this turn already
        carries a session-topics block (*session_topics_present*), or when any
        stored row (unjudged drafts included) mentions an anchor.
        """
        try:
            # T4 per-layer controls, read live: facts off -> no fact rows at
            # all (store not read); semantic off -> lexical top-k and no
            # fact-index access (not even a background reconcile).
            facts_on = bool(_settings.MEMORY_FACTS_ENABLED)
            rows = (
                self._get_memoria_store().list_injection_candidates(profile_id, lang=lang)
                if facts_on else []
            )
            self._memorias_pin_counter = _eng.pinned_injection_counter(rows)
            if facts_on and _settings.MEMORY_SEMANTIC_ENABLED:
                semantic_scores, fact_code = self._fact_semantic_scores(
                    profile_id, rows, fact_query_vec, lang=lang
                )
            else:
                semantic_scores = None
                fact_code = episodic_merge.FACT_FALLBACK_DISABLED
            semantic_count = 0
            weak_dropped = False
            pinned_count = 0
            if recall_stats is not None:
                recall_stats["semantic"] = 0
                if fact_code is not None:
                    recall_stats["fact_fallback"] = fact_code
            # memory-grounding T4 (owner decision M5): only an explicit TOPICAL
            # recall question can earn the no-match note; it is decided here so
            # an empty store still answers it.
            note_topic = ""
            if facts_on and is_topical_recall_question(contexto):
                note_topic = " ".join(recall_topic_anchors(contexto)[:3])[:60]
            can_cross_lang = (
                facts_on
                and _settings.MEMORY_SEMANTIC_ENABLED
                and fact_query_vec is not None
                and lang is not None
            )
            meta_facts_fallback = facts_on and self._unanchored_meta_recall_facts_allowed(contexto)
            if not rows and not episodes and not note_topic and not can_cross_lang and not meta_facts_fallback:
                return ""
            # W2b/W4 (memoria_recall_20260718): a meta/temporal recall query
            # ("¿qué recordás de mí?", "de qué hablamos la sesión pasada") has
            # no topical anchor for select_top_k — route it to RECENCY (session
            # summaries first when enabled, durable non-summary facts when off).
            if rows and self._summary_recall_allowed(contexto, recall_stats):
                lines = _eng.build_recency_lines(rows)
                routing_branch = "meta-recall"
                if semantic_scores and recall_stats is not None:
                    recall_stats["fact_fallback"] = "skipped"
            elif meta_facts_fallback:
                lines, pinned_count = self._durable_meta_recall_lines(
                    profile_id, rows, lang=lang
                )
                routing_branch = "meta-recall-facts"
                if semantic_scores and recall_stats is not None:
                    recall_stats["fact_fallback"] = "skipped"
            elif not rows:
                lines = []
                routing_branch = "episodic"
                if can_cross_lang:
                    added = self._cross_lang_semantic_fallback(
                        profile_id, lines, rows, fact_query_vec
                    )
                    if added:
                        semantic_count += added
                        routing_branch = "cross-lang-semantic"
            elif semantic_scores:
                fact_stats: dict = {}
                fact_rows = [r for r in rows if not self._is_summary_row(r)]
                lines = _eng.build_injection_lines(
                    fact_rows, contexto, semantic_scores=semantic_scores, stats=fact_stats,
                )
                semantic_count = int(fact_stats.get("semantic", 0))
                pinned_count = int(fact_stats.get("pinned", 0))
                routing_branch = "hybrid"
                if can_cross_lang and len(lines) - pinned_count == 0:
                    added = self._cross_lang_semantic_fallback(
                        profile_id, lines, rows, fact_query_vec
                    )
                    if added:
                        semantic_count += added
                        routing_branch = "cross-lang-semantic"
            else:
                fact_rows = [r for r in rows if not self._is_summary_row(r)]
                fact_stats = {}
                if lexical_min_shared is None:
                    lines = _eng.build_injection_lines(fact_rows, contexto, stats=fact_stats)
                else:
                    lines = _eng.build_injection_lines(
                        fact_rows, contexto, min_shared=lexical_min_shared, stats=fact_stats,
                    )
                    weak_dropped = len(lines) < len(
                        _eng.build_injection_lines(fact_rows, contexto)
                    )
                pinned_count = int(fact_stats.get("pinned", 0))
                routing_branch = "lexical"
                if can_cross_lang and len(lines) - pinned_count == 0:
                    added = self._cross_lang_semantic_fallback(
                        profile_id, lines, rows, fact_query_vec
                    )
                    if added:
                        semantic_count += added
                        routing_branch = "cross-lang-semantic"
            lexical_count = len(lines)
            episode_count = 0
            if episodes:
                superseded_facts = (
                    self._get_memoria_store().list_superseded_contents(profile_id, limit=25)
                    if facts_on else ()
                )
                episode_count = episodic_merge.append_episode_lines(
                    lines,
                    episodes,
                    max_chars=_settings.MEMORIAS_MAX_INJECT_CHARS,
                    max_episodes=_settings.MEMORY_RECALL_MAX_EPISODES,
                    injected_fact_lines=list(lines),
                    superseded_facts=superseded_facts,
                )
            if recall_stats is not None:
                recall_stats["lexical"] = lexical_count
                recall_stats["episodes"] = episode_count
                recall_stats["semantic"] = semantic_count
                if weak_dropped and not lines:
                    recall_stats["inject_skip"] = "lexical_only_weak"
            if note_topic and len(lines) - pinned_count <= 0 and episode_count == 0:
                # Nothing matched: say so in the volatile block instead of
                # leaving a gap the model fills with invented continuity. The
                # note follows any pinned lines (they never count as a match).
                skip = self._nomatch_skip_reason(
                    profile_id, contexto, weak_dropped, session_topics_present,
                )
                if skip is None:
                    lines.extend([
                        _eng.i18n_active.memorias_no_match_line().format(topic=note_topic),
                        _eng.i18n_active.memorias_no_match_rule(),
                    ])
                    routing_branch = "no-match-note"
                    if recall_stats is not None:
                        recall_stats["note"] = "no_match"
                elif recall_stats is not None:
                    recall_stats["note_skip"] = skip
            if not lines:
                return ""
            # 4R correction round (R1): sanitize (control chars + truncation)
            # then STRIP marker phrases — scout-scrub parity; truncation alone
            # would leave an instruction-like marker verbatim in the wrapper.
            sanitized = [
                _eng._strip_injection_markers(self._sanitize_history_context(line))
                for line in lines
            ]
            block = (
                _eng.i18n_active.memorias_block_open() + "\n"
                + "\n".join(sanitized)
                + "\n" + _eng.i18n_active.memorias_block_close()
            )
            # F11 (runtime_findings_batch_20260807): the only success signal
            # for this path before this line was the answer content itself —
            # RC-8 (memoria_store.py:55-56) bars logging the block's text, so
            # this is counts/routing ONLY, never a line or a char of content.
            _eng.logger.debug(
                "memorias injection block built: lines=%d branch=%s chars=%d",
                len(sanitized), routing_branch, len(block),
            )
            return block
        except Exception as exc:
            _eng.logger.warning(
                "memoria retrieval failed (fail-open): %s profile_id=%s",
                type(exc).__name__, profile_id,
            )
            return ""
        
    def _fact_semantic_scores(
        self,
        profile_id: str,
        rows,
        query_vec,
        *,
        lang: Optional[str] = None,
        ignore_orphans: Optional[bool] = None,
        reconcile_rows=None,
    ):
        """ADR-060 fact ranking inputs for this turn -> (scores|None, code|None).

        Scores come only from VALID cached fact vectors (row id + revision +
        content hash); a stale or missing cache schedules a background
        reconcile and is never embedded here. code None keeps the code the
        pre-assembly recall step already chose. Never raises.
        """
        mem = getattr(self, "_memory", None)
        if mem is None:
            return None, None
        # Lazy: v5 OFF must not import the v5 package (OFF-zero contract);
        # with a live subsystem it is already loaded.
        from opencohost.core.memory_v5_shadow.fact_index import FactSemanticIndex

        index = getattr(mem, "fact_index", None)
        if not isinstance(index, FactSemanticIndex):
            return None, None
        orphan_flag = (lang is not None) if ignore_orphans is None else ignore_orphans
        if not rows:
            if not orphan_flag:
                try:
                    if index.cache_store.count_fact_embeddings(profile_id) > 0:
                        index.schedule_reconcile(profile_id, ())
                except Exception as exc:
                    _eng.logger.warning(
                        "memory_facts: empty reconcile failed (fail-open) exc=%s profile_id=%s",
                        type(exc).__name__, profile_id,
                    )
            return None, None
        try:
            view = index.view(profile_id, rows, query_vec, ignore_orphans=orphan_flag)
            if view.needs_reconcile:
                target_rows = reconcile_rows if reconcile_rows is not None else rows
                if reconcile_rows is None and lang is not None:
                    full_rows = self._get_memoria_store().list_injection_candidates(profile_id)
                    if full_rows:
                        target_rows = full_rows
                index.schedule_reconcile(profile_id, target_rows)
        except Exception as exc:
            _eng.logger.warning(
                "memory_facts: view failed (fail-open) exc=%s profile_id=%s",
                type(exc).__name__, profile_id,
            )
            return None, ("error" if query_vec is not None else None)
        if query_vec is None:
            return None, None
        if not view.scores:
            return None, "cold"
        return view.scores, "none"

    def _attach_fact_query(self, mem, query_text: str, profile_id: str, outcome, started: float, budget_ms: int):
        """Rank facts with the turn's query vector (ADR-060) and fold it into
        *outcome*. The episodic stage's embedding is reused when it exists
        (T2: one embed per turn, even when the episodic outcome was discarded
        by the budget); otherwise the query is embedded inside what is left of
        the recall budget. Never raises."""
        fn = getattr(mem, "fact_query_vector", None)
        if fn is None:
            return outcome
        remaining_s = (budget_ms - (time.monotonic() - started) * 1000.0) / 1000.0
        try:
            if outcome.query_vec is not None:
                vec, code = fn(query_text, profile_id, remaining_s, precomputed=outcome.query_vec)
            else:
                vec, code = fn(query_text, profile_id, remaining_s)
            vec = tuple(float(x) for x in vec) if vec is not None else None
            code = str(code)
        except Exception:
            vec, code = None, "error"
        return dataclasses.replace(
            outcome,
            fact_query_vec=vec,
            fact_fallback=code,
            ms=int((time.monotonic() - started) * 1000),
        )

    def _run_episodic_recall(self, mem, query_text: str, profile_id: str):
        """One bounded v5 recall attempt -> episodic_merge.RecallOutcome.

        Never raises. mem=None (v5 OFF / init failed) -> disabled. The
        subsystem caps the worker IPC wait at MEMORY_RECALL_BUDGET_MS; a
        recall that finished is used even if it overran (over_budget=1 telemetry).
        """
        if mem is None:
            return episodic_merge.RecallOutcome.disabled()
        # T4 per-layer controls, read live. Semantic off disables BOTH worker
        # uses (v5 episodic recall is embedding-only); a disabled layer never
        # spends a worker call.
        semantic_on = bool(_settings.MEMORY_SEMANTIC_ENABLED)
        episodes_on = semantic_on and bool(_settings.MEMORY_EPISODES_ENABLED)
        fact_query_on = semantic_on and bool(_settings.MEMORY_FACTS_ENABLED)
        budget_ms = int(_settings.MEMORY_RECALL_BUDGET_MS)
        started = time.monotonic()
        if not episodes_on:
            outcome = episodic_merge.RecallOutcome.disabled()
        else:
            try:
                decision = mem.recall_decision(
                    query_text, profile_id=profile_id, budget_ms=budget_ms
                )
            except Exception:
                outcome = episodic_merge.RecallOutcome.error(
                    int((time.monotonic() - started) * 1000)
                )
            else:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                outcome = episodic_merge.resolve_recall_outcome(
                    decision, elapsed_ms, budget_ms, _settings.MEMORY_RECALL_MAX_EPISODES
                )
        if not fact_query_on:
            return outcome
        # ADR-060: facts get the rest of the same budget (one worker, serial).
        return self._attach_fact_query(mem, query_text, profile_id, outcome, started, budget_ms)

    @staticmethod
    def _semantic_side_failed(recall) -> bool:
        """T3: True when the semantic recall was rejected or timed out, i.e.
        the lexical fact path is the only (and therefore weak) signal."""
        if recall is None:
            return False
        return (
            str(recall.decision).startswith("REJECTED")
            or recall.fallback == episodic_merge.FALLBACK_TIMEOUT
            or recall.fact_fallback == "timeout"
        )

    def _memorias_builder_with_recall(self, recall, skip_reason=None, lang: Optional[str] = None):
        """Builder handed to the prompt assembler for this turn, plus the
        stats dict it fills. Without recalled episodes the real builder is
        called with its legacy (profile_id, contexto) signature, so the
        lexical-only prompt stays byte-identical. *skip_reason* withholds the
        whole block (facts and episodes) and is logged in the telemetry line.
        When the semantic side was rejected or timed out the lexical gate is
        tightened to STRONG_LEXICAL_SHARED_TOKENS shared tokens."""
        stats: dict = {"called": False, "lexical": 0, "episodes": 0, "semantic": 0}
        base = getattr(self, "_build_memorias_injection_block", None)
        if base is None:
            return None, stats
        episodes = recall.episodes if recall is not None else ()
        fact_vec = recall.fact_query_vec if recall is not None else None

        strict = self._semantic_side_failed(recall)

        def _builder(profile_id, contexto, lang=lang):
            stats["called"] = True
            if recall is not None:
                stats["decision"] = str(recall.decision)
            if skip_reason:
                stats["inject_skip"] = skip_reason
                return ""
            # Judge round 1: the session-topics flag is only passed when set,
            # so every legacy call signature stays byte-identical.
            extra = {"session_topics_present": True} if stats.get("session_topics") else {}
            if lang is not None:
                extra["lang"] = lang
            if strict:
                return base(
                    profile_id, contexto, episodes=episodes or None,
                    recall_stats=stats, fact_query_vec=fact_vec,
                    lexical_min_shared=STRONG_LEXICAL_SHARED_TOKENS, **extra,
                )
            if fact_vec is not None:
                return base(
                    profile_id, contexto, episodes=episodes or None,
                    recall_stats=stats, fact_query_vec=fact_vec, **extra,
                )
            if episodes:
                # ADR-059 signature: no fact_query_vec kwarg when unused.
                return base(
                    profile_id, contexto, episodes=episodes, recall_stats=stats, **extra,
                )
            if _settings.MEMORY_SUMMARY_RECALL_ENABLED and recall is not None:
                # T6: the summary gate reads the v5 decision from recall_stats.
                block = base(profile_id, contexto, recall_stats=stats, **extra)
            else:
                block = base(profile_id, contexto, **extra)
            if block and _eng.i18n_active.memorias_no_match_rule() in block:
                # The legacy signature carries no recall_stats: read the
                # code-built no-match note off the block itself (T4).
                stats["note"] = "no_match"
                stats["lexical"] = 0
            else:
                stats["lexical"] = self._count_memorias_block_lines(block)
            return block

        return _builder, stats

    @staticmethod
    def _count_memorias_block_lines(block: str) -> int:
        if not block:
            return 0
        open_tag = _eng.i18n_active.memorias_block_open() + "\n"
        close_tag = "\n" + _eng.i18n_active.memorias_block_close()
        inner = block
        if inner.startswith(open_tag):
            inner = inner[len(open_tag):]
        if inner.endswith(close_tag):
            inner = inner[: -len(close_tag)]
        return len(inner.split("\n")) if inner else 0

    def _log_recall_telemetry(self, recall, stats: dict) -> None:
        """Exactly one INFO `memory_recall:` line per attempt, on the
        OpenCohost logger (the one routed to logs/opencohost_*.log).
        Numbers and codes only."""
        try:
            if recall is None:  # T3: a skipped turn still logs its reason
                recall = episodic_merge.RecallOutcome.disabled()
            outcome = recall
            fact_code = stats.get("fact_fallback")
            if not stats.get("called"):
                # Memorias injection disabled for this turn: nothing merged.
                fact_code = episodic_merge.FACT_FALLBACK_DISABLED
                if recall.episodes:
                    outcome = episodic_merge.RecallOutcome(
                        (), recall.decision, recall.scope, recall.top_score,
                        recall.ms, episodic_merge.FALLBACK_DISABLED,
                    )
            if stats.get("called"):
                turn_timing.add("recall_ms", recall.ms)  # the recall ms this line reports
            _eng.logger.info(
                episodic_merge.format_recall_telemetry(
                    outcome, stats.get("lexical", 0), stats.get("episodes", 0),
                    semantic=stats.get("semantic", 0), fact_fallback=fact_code,
                    inject_skip=stats.get("inject_skip"),
                    note=stats.get("note"),
                )
            )
        except Exception:
            pass

    @classmethod
    def _build_ledger_line(cls, user_text: str, asst_text: str) -> str:
        """Compact an evicted turn into one ledger line body (no [hace N] prefix).

        The "[hace N turno(s)]" prefix is rendered at build time by
        MemoryDigest.build_block() so the counter always reflects distance-from-now.

        Format: contexto: <first words> → Kira: <first sentence>

        G3: the history-wrapper frame comes off before the 8-word budget is
        spent. historial stores the WRAPPED turn on purpose (the frame is the
        model's provenance cue), but "El streamer escribió: " is 3 words and
        "El streamer dijo (PTT): " is 4 — enough to drop the operator's whole
        symptom from the digest block, while the "contexto: ... → Kira: ..."
        format already says who said what.
        """
        user_summary = cls._first_words(_eng.strip_history_wrapper(user_text))
        if len(user_summary) > 100:
            user_summary = user_summary[:100]
        kira_summary = cls._first_sentence(asst_text)
        ctx_label = _eng.i18n_active.ledger_context_label()
        kira_label = _eng.i18n_active.ledger_kira_label()
        return f"{ctx_label}: {user_summary} {kira_label}: {kira_summary}"

    # C1 (memoria_quality_20260717): closed leading-tic list stripped off the
    # Kira side before picking the first CONTENTFUL sentence. Kira's stock
    # openers ("Mirá vos," is 52% of legacy rows) carry zero memory signal and
    # would otherwise become the whole stored content. Comma-inclusive so we
    # only strip the opener, not a mid-sentence "Mirá".
    _MEMORIA_KIRA_LEADING_TICS = ("Mirá vos,", "Mirá,", "Che,", "Posta,")
    _MEMORIA_CONTENT_USER_WORDS = 36
    # Draft body cap bumped from 300 to 400 alongside the 36-word budget so
    # longer operator facts survive intact to the promotion judge while keeping
    # injection bounded by MEMORIAS_MAX_INJECT_CHARS and clip_to_remaining.
    _MEMORIA_CONTENT_MAX_CHARS = 400

    @classmethod
    def _contentful_kira_sentence(cls, asst_text: str) -> str:
        """First sentence of *asst_text* with >=3 significant tokens, after
        stripping a leading tic. Falls back to the first sentence when none
        qualify. Returns "" when the reply is empty OR collapses to nothing
        after tic-stripping (e.g. a tic-only reply like "Mirá vos,") — the
        caller (_build_memoria_content) then stores the user side alone, with
        no dangling Kira label."""
        stripped = (asst_text or "").lstrip()
        for tic in cls._MEMORIA_KIRA_LEADING_TICS:
            if stripped.startswith(tic):
                stripped = stripped[len(tic):].lstrip()
                break
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", stripped) if s.strip()]
        for sentence in sentences:
            if _eng.significant_token_count(sentence) >= 3:
                return sentence
        return sentences[0] if sentences else stripped.strip()

    @classmethod
    def _build_memoria_content(cls, user_text: str, asst_text: str) -> str:
        """C1 memoria body — richer than the 8-word digest ledger line.

        user side = first 24 words (digest keeps 8 — decoupled). Kira side =
        first CONTENTFUL sentence (see _contentful_kira_sentence). Capped at 300
        chars, preserving the pre-C1 content-length invariant.

        G3: the history-wrapper frame comes off here too, not only off the
        ledger line. It reads like useful provenance in the "Memoria de Kira"
        panel, but it is not: _DIGEST_CAPTURE_SOURCES is {"direct", "ptt"}, so
        every memoria's user side is the operator by construction (viewer chat
        is structurally barred), and "contexto:" already labels the side. All it
        actually does is spend 3-4 of the 24 words, then spend those same chars
        AGAIN inside build_injection_lines' recall budget when the row is
        re-injected. It also left the row self-inconsistent: stable_key, title
        and signature are all derived from STRIPPED text (memoria_store
        _significant_tokens), so only the body carried the tax. Modality
        (typed vs voice) belongs in a column if the operator ever needs it, not
        in a prose prefix that survives only by accident of which wrapper the
        caller happened to apply.
        """
        user_summary = cls._first_words(
            _eng.strip_history_wrapper(user_text), cls._MEMORIA_CONTENT_USER_WORDS
        )
        kira_summary = cls._contentful_kira_sentence(asst_text)
        ctx_label = _eng.i18n_active.ledger_context_label()
        # FIX3 (memoria_quality_20260717): append the "→ Kira: ..." segment ONLY
        # when Kira's side is non-empty. A tic-only reply collapses to "" — a
        # contentless Kira side simply isn't stored (no dangling label).
        if not kira_summary:
            return f"{ctx_label}: {user_summary}"[:cls._MEMORIA_CONTENT_MAX_CHARS]
        kira_label = _eng.i18n_active.ledger_kira_label()
        return f"{ctx_label}: {user_summary} {kira_label}: {kira_summary}"[:cls._MEMORIA_CONTENT_MAX_CHARS]

    def set_memorias_private(self, value: bool) -> None:
        """Session-scoped memorias capture-privacy switch (R2/R4).

        True pauses capture; False (default) resumes it. Forward-only: only
        affects pairs appended to historial AFTER this call — never retro-
        captures a paused window, never retro-hides an already-capturable
        one. UI wiring (the actual toggle control) lands in slice 7.
        """
        self._memorias_private = bool(value)

    @property
    def memorias_private(self) -> bool:
        """Current session-scoped capture-privacy switch state (slice 7 UI read)."""
        return self._memorias_private


    def _collect_flush_drafts(self) -> list[tuple[str, str, str, str, str]]:
        """F4 (slice 4) — snapshot the LIVE (un-evicted) historial window into
        memoria drafts. Pure, no-I/O. MUST be called while _history_lock is
        held: iterates historial in (user, assistant) pairs (historial only
        ever holds complete pairs — see _commit_history), running each pair
        through the exact same gate chain as eviction capture
        (_build_memoria_draft — R1-R4/RC-1). Reused by both close-flush
        (task 4.12) and profile-switch flush (task 4.14) — one snapshot
        routine, two callers.
        """
        drafts: list[tuple[str, str, str, str, str]] = []
        entries = list(self.historial)
        for i in range(0, len(entries) - 1, 2):
            user_entry, asst_entry = entries[i], entries[i + 1]
            # FIX2 (memoria_quality_20260717): a pair already captured at
            # commit-time (B1) is byte-identical on disk — skip the flush
            # re-upsert (revision churn / panel-order flapping). Unflagged
            # pairs (e.g. commit-capture failed, or appended directly) still
            # flush, preserving flush's belt-and-braces role.
            if user_entry.get("mem_captured", False):
                continue
            user_content = user_entry.get("content", "")
            asst_content = asst_entry.get("content", "")
            draft = self._build_memoria_draft(
                user_content, asst_content,
                source=user_entry.get("source"),
                private=user_entry.get("private"),
            )
            if draft is not None:
                drafts.append(draft)
        return drafts


    def _run_switch_flush(
        self,
        drafts: list[tuple[str, str, str, str, str]],
        summary_profile_id: str | None = None,
        summary_titles: list[str] | None = None,
        lang: Optional[str] = None,
    ) -> None:
        """Worker-thread body for _dispatch_switch_flush. Fail-open; partial
        failures log a COUNT only, never content (RC-8)."""
        flush_lang = self._resolve_turn_lang(lang)
        failed = 0
        for profile_id, stable_key, title, content, signature in drafts:
            try:
                self._get_memoria_store().upsert_draft(
                    profile_id, stable_key, title, content, signature=signature, lang=flush_lang
                )
            except Exception:
                failed += 1
        if failed:
            _eng.logger.warning(
                "memoria switch-flush partial failure (fail-open): failed=%d of %d",
                failed, len(drafts),
            )
        # W2a: departing profile's mechanical session summary, same worker.
        self._write_session_summary(summary_profile_id, summary_titles or [], lang=flush_lang)

    

    @staticmethod
    def _first_words(text: str, max_words: int = 8) -> str:
        """Return the first N words of text, stripped of leading/trailing whitespace."""
        words = text.split()
        return " ".join(words[:max_words])

    @staticmethod
    def _first_sentence(text: str) -> str:
        """Return the first sentence of text (split on . ! ?)."""
        return _eng._first_sentence(text)
