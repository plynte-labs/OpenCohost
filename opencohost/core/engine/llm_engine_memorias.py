"""
opencohost/core/engine/llm_engine_memorias.py
""" 
import re
import threading
from typing import Optional

from opencohost.core.memory.memoria_store import MemoriaStore

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

class MemoriaCaptureMixin:
    
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
        _build_memoria_content (24-word user budget + first contentful Kira
        sentence), NOT the 8-word digest ledger line.
        """
        if source not in _eng._DIGEST_CAPTURE_SOURCES:
            return None
        if user_content.startswith("[agenda segura"):
            return None
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
        signature_text = f"{user_content} {asst_content}"
        stable_key = _eng.derive_stable_key(profile_id, signature_text)
        if stable_key is None:
            return None
        return (
            profile_id, stable_key, _eng.build_title(signature_text),
            self._build_memoria_content(user_content, asst_content),
            _eng.build_signature(signature_text),
        )
        
    def _dispatch_switch_flush(
        self,
        drafts: list[tuple[str, str, str, str, str]],
        summary_profile_id: str | None = None,
        summary_titles: list[str] | None = None,
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
        summary_titles = summary_titles or []
        if not drafts and len(summary_titles) < _eng.MEMORIAS_SUMMARY_MIN_TITLES:
            return
        threading.Thread(
            target=self._run_switch_flush,
            args=(drafts, summary_profile_id, summary_titles),
            daemon=True,
        ).start()
        
    def _write_session_summary(self, profile_id: str | None, titles: list[str]) -> None:
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
        try:
            self._get_memoria_store().insert_summary(
                profile_id, title, content, signature=_eng.build_signature(content),
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

        if source.startswith("kira-agenda"):
            safe_context = "[agenda segura: prompt interno omitido]"
        else:
            # agenda_ptt_commit_raw_text: when a caller supplies the honest
            # turn text (PTT path), store THAT instead of the raw contexto
            # (which, for agenda-driven turns, is the full prompt template).
            # Sanitizer pipeline is unchanged — it applies to whatever text
            # ends up in safe_context.
            safe_context = self._sanitize_history_context(history_text if history_text else contexto)

        # T3 — staged memorias draft (pure strings, no I/O). Built while
        # _history_lock is held below; upsert_draft is called AFTER the lock
        # releases (upsert_draft must never run with _history_lock held).
        pending_memoria_capture: Optional[tuple[str, str, str, str, str]] = None
        committed_memoria_capture: Optional[tuple[str, str, str, str, str]] = None

        # Hold _history_lock around the eviction-capture + both appends so
        # concurrent callers (worker loop and agenda speaker daemon) cannot
        # interleave a read of historial[0]/[1] with an append from another thread.
        with self._history_lock:
            # D1 — eviction capture: before appending the new turn, check whether
            # the deque is at maxlen. If so, the oldest pair (user+assistant at
            # indices 0 and 1) will be auto-evicted. Capture them as one ledger line.
            # We only capture non-agenda turns; agenda turns already masked to
            # "[agenda segura…]" so their "eviction" produces no useful ledger line.
            maxlen = self.historial.maxlen  # always HISTORY_MAX_TURNS * 2
            if maxlen is not None and len(self.historial) >= maxlen and not source.startswith("kira-agenda"):
                # historial is guaranteed to hold pairs (user, assistant).
                evicted_user_content = self.historial[0].get("content", "")
                evicted_asst_content = self.historial[1].get("content", "")
                # Skip capture when the evicted pair is agenda-origin (user slot is
                # the sentinel), to prevent real agenda replies from leaking into
                # the digest via the eviction path.
                evicted_is_agenda = evicted_user_content.startswith("[agenda segura")
                # D1 — source allowlist gate (fail-closed): only capture when the
                # evicted pair's own source tag is in _DIGEST_CAPTURE_SOURCES.
                # Missing or unknown source values are treated as not-capturable.
                evicted_source = self.historial[0].get("source")
                if evicted_source not in _eng._DIGEST_CAPTURE_SOURCES:
                    _eng.logger.debug(
                        "Eviction capture skipped: source=%r not in digest allowlist",
                        evicted_source,
                    )
                elif not evicted_is_agenda:
                    ledger_line = self._build_ledger_line(
                        evicted_user_content,
                        evicted_asst_content,
                    )
                    self._memory_digest.append(ledger_line)

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
                    if not self.historial[0].get("mem_captured", False):
                        pending_memoria_capture = self._build_memoria_draft(
                            evicted_user_content, evicted_asst_content,
                            source=evicted_source,
                            private=self.historial[0].get("private"),
                        )

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
            committed_user_entry = {
                'role': 'user', 'content': safe_context, 'source': source,
                'private': priv,
            }
            self.historial.append(committed_user_entry)
            self.historial.append({
                'role': 'assistant', 'content': dialogo, 'source': source,
                'private': priv,
            })

            # B1 (memoria_quality_20260717) — capture-at-commit: the pair that
            # just ENTERED historial becomes a memoria immediately, not only
            # when it is later evicted/flushed. This is the durability fix (F2):
            # a hard kill before eviction/close no longer loses the exchange.
            # Built under the lock (same discipline as eviction capture);
            # upserted AFTER the lock releases. Belt-and-braces with eviction +
            # flush: the stable_key upsert (ON CONFLICT(profile_id,stable_key)
            # ... WHERE status='draft') is idempotent, so a pair captured here
            # and again later is a no-op revision bump, never a duplicate row.
            committed_memoria_capture = self._build_memoria_draft(
                safe_context, dialogo, source=source, private=priv,
            )

        if pending_memoria_capture is not None:
            self._capture_memoria(*pending_memoria_capture)
        if committed_memoria_capture is not None:
            # FIX2 (memoria_quality_20260717): flag the just-committed pair ONLY
            # when the upsert actually persisted, so eviction/flush skip the
            # byte-identical re-upsert (revision churn / panel-order flapping).
            # On the fail-open path the flag stays unset → eviction/flush retain
            # their retry role (belt-and-braces preserved for the failure case).
            if self._capture_memoria(*committed_memoria_capture):
                committed_user_entry["mem_captured"] = True
                
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
        try:
            with self._history_lock:
                drafts = self._collect_flush_drafts()
                # W2a: snapshot this session's titles + profile under the same
                # lock, then clear (a double close never writes two summaries).
                summary_profile_id = self._current_profile_id
                summary_titles = list(self._session_memoria_titles)
                self._session_memoria_titles.clear()
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
                        profile_id, stable_key, title, content, signature=signature
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
            self._write_session_summary(summary_profile_id, summary_titles)
        else:
            _eng.logger.debug(
                "memoria close-flush budget spent, skipping session summary (profile_id=%s)",
                summary_profile_id,
            )
            
    def _capture_memoria(
        self, profile_id: str, stable_key: str, title: str, content: str, signature: str = ""
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
        try:
            self._get_memoria_store().upsert_draft(
                profile_id, stable_key, title, content, signature=signature,
            )
        except Exception as exc:
            _eng.logger.warning(
                "memoria capture failed (fail-open): %s profile_id=%s stable_key=%s",
                type(exc).__name__, profile_id, stable_key,
            )
            return False
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

    

    def _build_memorias_injection_block(self, profile_id: str, contexto: str) -> str:
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
        """
        try:
            rows = self._get_memoria_store().list_injection_candidates(profile_id)
            self._memorias_pin_counter = _eng.pinned_injection_counter(rows)
            if not rows:
                return ""
            # W2b/W4 (memoria_recall_20260718): a meta/temporal recall query
            # ("¿qué recordás de mí?", "de qué hablamos la sesión pasada") has
            # no topical anchor for select_top_k — route it to RECENCY (session
            # summaries first). W4 gating is structural: recency fires ONLY on a
            # detected meta query; a topical 0-match keeps injecting nothing
            # (owner mitigation — the existing select_top_k path, untouched).
            if _eng.is_meta_recall_query(contexto):
                lines = _eng.build_recency_lines(rows)
                routing_branch = "meta-recall"
            else:
                lines = _eng.build_injection_lines(rows, contexto)
                routing_branch = "lexical"
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
    _MEMORIA_CONTENT_USER_WORDS = 24

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
            return f"{ctx_label}: {user_summary}"[:300]
        kira_label = _eng.i18n_active.ledger_kira_label()
        return f"{ctx_label}: {user_summary} {kira_label}: {kira_summary}"[:300]

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
    ) -> None:
        """Worker-thread body for _dispatch_switch_flush. Fail-open; partial
        failures log a COUNT only, never content (RC-8)."""
        failed = 0
        for profile_id, stable_key, title, content, signature in drafts:
            try:
                self._get_memoria_store().upsert_draft(
                    profile_id, stable_key, title, content, signature=signature
                )
            except Exception:
                failed += 1
        if failed:
            _eng.logger.warning(
                "memoria switch-flush partial failure (fail-open): failed=%d of %d",
                failed, len(drafts),
            )
        # W2a: departing profile's mechanical session summary, same worker.
        self._write_session_summary(summary_profile_id, summary_titles or [])

    

    @staticmethod
    def _first_words(text: str, max_words: int = 8) -> str:
        """Return the first N words of text, stripped of leading/trailing whitespace."""
        words = text.split()
        return " ".join(words[:max_words])

    @staticmethod
    def _first_sentence(text: str) -> str:
        """Return the first sentence of text (split on . ! ?)."""
        return _eng._first_sentence(text)
