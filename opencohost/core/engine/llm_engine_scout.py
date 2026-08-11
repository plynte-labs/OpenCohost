""" opencohost/core/engine/llm_engine_scout.py
Scout and memory-promotion methods of MotorVocalIA.
Mixin extracted from llm_engine.py.
All runtime state stays on MotorVocalIA.
"""
import hashlib
import re
import sqlite3
import time
from collections import Counter

# Module object, not names off it: `_eng.X` resolves at CALL time, so the suite's
# monkeypatches on llm_engine are seen. Safe at module level in THIS direction --
# llm_engine is already in sys.modules when it imports us, and nothing below
# dereferences `_eng` until a method actually runs.
#
# Never evaluate `_eng.X` at module or class-body scope (default args, decorator
# args, class attributes). Those run during llm_engine's PARTIAL import, so they
# see only the names bound above its `import ScoutPromotionMixin` line: measured
# 2026-08-11, `_eng.SYSTEM_PROMPT` (llm_engine.py:30) resolves while
# `_eng.TTS_AUDIO_QUEUE_TIMEOUT` (llm_engine.py:105) raises
# "AttributeError: partially initialized module". Which side of that line a name
# falls on is not something a mixin can track -- and the line moves -- so the rule
# is scope, not the name. `mixin_freevar_audit.py` enforces it.
from opencohost.core import llm_engine as _eng

class ScoutPromotionMixin:
     # ── Topic Scout (topic_scout_llm_20260629) ──────────────────────────────
    def _scout_render_history(self, history_snapshot: list) -> list[str]:
        """Compact, sanitized rendering of the most-recent LIVE turns.

        Inherits the direct-path gate (``_sanitize_history_context``); the scout
        needs topic words only, so usernames and injection phrases are scrubbed.
        """
        # Host-only (history_source_tag_20260629 Task C/D): filter the FULL
        # snapshot to genuine HOST turns (direct/ptt) FIRST, then take the last N,
        # so the scout sees the last N real host turns — not N mixed turns thinned
        # to however few host turns happen to survive. Untagged/viewer/agenda
        # entries (source absent or not in the set) are excluded by `.get`.

        host_only = [
            msg for msg in history_snapshot
            if isinstance(msg, dict) and msg.get("source") in {"direct", "ptt"}
        ]
        recent = host_only[-_eng.LLM_SCOUT_HISTORY_MSGS:]
        lines: list[str] = []
        for msg in recent:
            if not isinstance(msg, dict):
                continue
            content = self._scout_scrub_text(
                self._sanitize_history_context(str(msg.get("content", "")))
            )
            if not content:
                continue
            speaker = "Host" if msg.get("role") == "user" else "Kira"
            lines.append(f"{speaker}: {content}")
        return lines

    @staticmethod
    def _scout_scrub_text(text: str) -> str:
        """Strip @mentions and injection-marker phrases from a render line.

        The scout only needs topic words — never usernames or injected
        instructions — so it scrubs harder than the verbatim direct path.
        """
        text = re.sub(r"@\w+", "", text)
        return _eng._strip_injection_markers(text)
    
    
    def _scout_extract_text(self, response, *, field: str = "content") -> str:
        """Pull the assistant content out of a chat response (dict or object).

        *field* selects which message field. The memoria judge's empty-content
        self-heal needs ``thinking``, and this already handles both response
        shapes — a second extractor would only duplicate that.
        """
        if response is None:
            return ""
        msg = response["message"] if isinstance(response, dict) else getattr(response, "message", None)
        if msg is None:
            return ""
        content = msg.get(field) if isinstance(msg, dict) else getattr(msg, field, "")
        return content or ""

    def _scout_parse_titles(self, text: str, input_block: str) -> list[dict]:
        """Parse adjacent topic titles: filter preamble/echo, sanitize, dedupe, cap 3."""
        if not text:
            return []
        from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

        input_cf = input_block.casefold()
        results: list[dict] = []
        seen: set[str] = set()
        for raw_line in text.splitlines():
            line = raw_line.strip().strip("-•*\t\"'").strip()
            if not line:
                continue
            # Preamble filter: conversational lead-ins ("Claro, acá van:") and
            # over-long lines are not titles.
            if line.endswith(":"):
                continue
            if len(line.split()) > _eng.SCOUT_TITLE_MAX_WORDS:
                continue
            # Echo filter: a title that merely repeats a seed term is not adjacent.
            if line.casefold() in input_cf:
                continue
            # Title gate (emoji/code/length) — drop silently on rejection.
            try:
                title = KiraAgendaController.sanitize_topic_text(line, field="title")
            except ValueError:
                continue
            slug = title.casefold()
            if slug in seen:
                continue
            seen.add(slug)
            results.append({"title": title, "source": "scout", "confidence": "LOW"})
            if len(results) >= 3:
                break
        return results

    def scout_digest(self) -> list[dict]:
        """Topic Scout: idle-time adjacent-topic suggester.

        Best-effort and FULLY self-contained: every internal failure returns []
        so it can never break the rule-based ``generate_suggestions`` it runs
        alongside. Returns DRAFTED-ready dicts; NEVER speaks or persists.
        """
        try:
            if not _eng.SCOUT_ENABLED:
                return []
            # F4 Pregen Cloud Gate (multi_provider_llm_20260723): the scout is
            # speculative generation and pregen-OFF covers ALL speculative spend,
            # so skip the dispatch on cloud unless explicitly opted in. Local
            # short-circuits (byte-identical). Same idiom as the pregenerate gate.
            if not self._is_local and not self._provider_config.get("pregen_enabled", False):
                return []
            # Gate 3: no model resident, or a switch pending/in-flight -> skip
            # (never cold-load; use the RESIDENT model, not the desired one).
            if self._loaded_model is None:
                return []
            if self._pending_model_switch or self._awaiting_first_success_after_switch:
                return []
            # Gate 1: re-read idle state immediately before the call.
            if self.is_processing or self.is_speaking:
                return []
            # Gate 2: real work queued but not yet started would serialize behind
            # an in-flight scout on the single runner — abort on ANY pending item.
            if self.has_pending_priority_before(_eng.SCOUT_QUEUE_FLOOR):
                return []
            # Value gate (efficiency, not safety): a thinking-capable model burns
            # the 64-token budget on <think> and returns nothing useful.
            if self._check_capabilities_reasoning(self._loaded_model):
                return []
            with self._history_lock:
                history_snapshot = list(self.historial)
            lines = self._scout_render_history(history_snapshot)
            if len(lines) < _eng.LLM_SCOUT_MIN_DIGEST_LINES:
                return []
            input_block = "\n".join(lines)
            # Fresh-input gate: hash the LIVE snapshot; skip an identical input.
            # Cached on EVERY attempt that reaches here (even when the call returns
            # []), so a no-op scout does not re-fire until the conversation moves.
            input_hash = hashlib.sha256(input_block.encode("utf-8")).hexdigest()
            if input_hash == self._scout_last_input_hash:
                return []
            self._scout_last_input_hash = input_hash
            if self._ollama_scout_client is None:
                self._ollama_scout_client = self._create_ollama_scout_client(self.ollama)
            prompt = _eng.i18n_active.scout_prompt().format(digest_block=input_block)
            response = self._ollama_chat_with_watchdog(
                timeout=_eng.LLM_SCOUT_TIMEOUT + 2,
                chat_callable=self._ollama_scout_chat,
                model=self._loaded_model,
                messages=[{"role": "user", "content": prompt}],
                options={
                    "num_predict": _eng.LLM_SCOUT_NUM_PREDICT,
                    "temperature": _eng.LLM_SCOUT_TEMPERATURE,
                },
                keep_alive=_eng.LLM_KEEP_ALIVE,
            )
            text = self._scout_extract_text(response)
            return self._scout_parse_titles(text, input_block)
        except Exception:
            # Total internal isolation — a scout failure must never escape.
            return []

    # ── memoria draft promotion (memory_promotion_20260725) ─────────────────
    def _judge_model(self) -> str:
        """The LOCAL model the judge will run on — pinned regardless of the
        active provider (owner decision 2026-08-08, F16).

        The once-per-launch promotion sweep sends up to 40 draft contents
        (excerpts of the owner's own conversation) to whatever model this
        resolves to. Memory content stays on the machine; the judge is the
        only place in the memoria pipeline that ever crosses the network, so
        it must never follow a cloud provider — never the active profile's
        model, never ``_cloud_chat``.

        Prefers the already-resident model (``_loaded_model``, no cold-load)
        when Ollama already has one loaded; otherwise falls back to the SAME
        local-posture resolution the cloud-fallback retry already uses
        (``llm_engine.py`` F1: ``_last_known_good_model or current_model``) —
        reused here rather than inventing a second one. Always a str:
        ``_resolve_reasoning_classification`` lowercases it, so a None would
        raise straight into the sweep's catch-all and silently disable the
        feature.
        """
        return self._loaded_model or self._last_known_good_model or self.current_model or ""

    def _judge_timeout_seconds(self) -> float:
        """The judge's adaptive time budget (owner decision 5).

        Reuses ``_pregen_last_gen_duration`` — the SAME measurement
        ``_pregen_retry_gate_seconds`` already ships — instead of introducing a
        second latency-measurement scheme or a fixed constant (a constant would
        be a per-model assumption in disguise, contradicting the model-agnostic
        decision 2).

        Stated honestly: at the STARTUP trigger no generation has happened yet,
        so ``last`` is None and this returns the cold-start value — the identical
        contract ``_pregen_retry_gate_seconds`` already has. The reasoning branch
        rides the capability probe D2 needs anyway (cached ``ollama.show``), so it
        is a per-model MEASUREMENT, not a per-model assumption.

        Always the LOCAL adaptive budget now (owner decision 2026-08-08, F16):
        the judge transport (``_ollama_judge_chat``) is pinned local, so there is
        no cloud socket for ``CLOUD_CHAT_TIMEOUT`` to bound anymore — deriving
        the budget from local generation latency is correct in every case, not
        just when the active provider happens to be local too.
        """
        last = self._pregen_last_gen_duration
        if last is None:
            base = _eng.RETRY_MIN_REMAINING_SECONDS * (
                _eng._JUDGE_REASONING_COLD_FACTOR
                if self._resolve_reasoning_classification(self._judge_model()) else 1.0
            )
        else:
            base = last * _eng._JUDGE_BUDGET_FACTOR
        return max(_eng._JUDGE_BUDGET_FLOOR_SECONDS, min(_eng._JUDGE_BUDGET_CEILING_SECONDS, base))

    def _run_promotion_judge(self, batch: list, *, chat_callable=None) -> list[tuple]:
        """ONE chat completion over *batch*, parsed. Raises on transport failure.

        The reasoning branch (D2): ``scout_digest``'s sixth gate skips thinking
        models entirely because they burn a 64-token budget inside ``<think>``
        and return empty ``content``. Adopting it here would mean ZERO promotions
        forever on the owner's Qwen3-class models, so this reaches the same
        conclusion ``_generar_dialogo``'s self-heal already does instead: OMIT
        ``num_predict`` on a reasoning-capable model. The capability answer is
        cached, so this costs no extra RPC.

        Detection goes through ``_resolve_reasoning_classification`` — the SAME
        resolver ``_generar_dialogo`` already trusts — not the narrow
        ``_check_capabilities_reasoning`` probe alone. That probe degrades to
        False on any Ollama build whose ``show`` response lacks ``capabilities``
        (and returns {} outright on cloud), which would leave the cap on a
        Qwen3-class model, burn it inside <think>, return empty content, and
        re-pay the identical inference every launch with zero promotions.
        """
        draft_block = "\n".join(
            f"{i}. {row['content']}" for i, row in enumerate(batch, start=1)
        )
        # str.replace, not str.format: the prompt's JSON example is full of
        # literal braces and doubling every one of them is a corruption trap.
        prompt = _eng._PROMOTION_JUDGE_PROMPT.replace("{draft_block}", draft_block)
        budget = self._judge_timeout_seconds()
        # Reuses the established auxiliary-generation temperature rather than
        # inventing a second knob; a format-drifted reply is already handled
        # (the parser returns [] and nothing is marked judged).
        options = {"temperature": _eng.LLM_SCOUT_TEMPERATURE}
        judge_model = self._judge_model()
        if not self._resolve_reasoning_classification(judge_model):
            # Always the LOCAL-sized cap now (owner decision 2026-08-08, F16):
            # the judge transport is pinned local, so the CLOUD_MAX_TOKENS branch
            # this used to take when the ACTIVE PROVIDER was cloud would size the
            # cap for a model this call never reaches. A 40-draft batch that keeps
            # ~20 needs ~1700 output tokens (a keep is ~60-67: the index, the
            # flags and up to 220 chars of rewritten text), so a flat 1200 can
            # still truncate a big local batch -- accepted cost of the pin, same
            # as any other local judge run; the next launch simply retries.
            options["num_predict"] = _eng._PROMOTION_NUM_PREDICT
        if chat_callable is None:
            # Rebuilt per sweep because the budget is adaptive; the sweep runs
            # once per launch, so this costs nothing. Unconditional now: the
            # judge transport (`_ollama_judge_chat`) is pinned local regardless
            # of the active provider, so it always needs this client.
            self._ollama_judge_client = self._create_ollama_scout_client(
                self.ollama, timeout=budget,
            )
        call = chat_callable or self._ollama_judge_chat
        messages = [{"role": "user", "content": prompt}]
        deadline = time.monotonic() + budget + 2
        response = self._ollama_chat_with_watchdog(
            timeout=budget + 2,  # the socket abort must fire first (scout precedent)
            chat_callable=call,
            model=judge_model,
            messages=messages,
            options=options,
            keep_alive=_eng.LLM_KEEP_ALIVE,
        )
        text = self._scout_extract_text(response)
        # `_generar_dialogo`'s Layer-2 self-heal, verbatim in shape: empty visible
        # content + internal thinking + a cap in the options means the model spent
        # the budget inside <think>. Drop the cap and re-issue ONCE.
        #
        # This is the only thing that makes the judge work on a thinking-capable
        # CLOUD model. Detection cannot: the name heuristic knows a handful of
        # markers and `_fetch_show` returns {} outright when `not self._is_local`,
        # so glm-5.2 is classified "not reasoning", gets num_predict (sent as
        # max_tokens), returns empty content — and the sweep promotes nothing,
        # every launch, forever, paying a full cloud inference each time. The
        # response IS the evidence, so no detection is involved and this covers
        # cloud and local, known and unknown model names alike.
        #
        # One shot, never a loop, and inside the FIRST call's deadline: the retry
        # gets what is LEFT of it, so the self-heal cannot double the operator's
        # worst-case wait. A non-positive remainder is already handled — the
        # watchdog floors its wait at 0.1s and raises, which the sweep's isolation
        # turns into "nothing written, next launch retries".
        if not text.strip() and self._scout_extract_text(response, field="thinking") \
                and "num_predict" in options:
            options.pop("num_predict", None)
            _eng.logger.warning(
                "memoria judge returned empty content with internal thinking; "
                "dropping the token cap and retrying once",
            )
            response = self._ollama_chat_with_watchdog(
                timeout=deadline - time.monotonic(),
                chat_callable=call,
                model=judge_model,
                messages=messages,
                options=options,
                keep_alive=_eng.LLM_KEEP_ALIVE,
            )
            text = self._scout_extract_text(response)
        return _eng._parse_promotion_decisions(text, len(batch))

    def _promotion_gate(self) -> str:
        """Name of the gate blocking a sweep right now, or "" when clear.

        No longer branches on the active provider (owner decision 2026-08-08,
        F16 superseding the earlier "whatever provider is active" decision 2):
        the judge is pinned local always, so this gates only when NO local
        model name can be resolved at all (``_judge_model()`` empty) — a fresh
        install with no local model ever configured. An actually-unreachable
        Ollama, or a model name that IS resolved but not actually installed,
        cannot be known without a live probe here; both surface later as an
        ordinary transport failure inside ``_run_promotion_judge``, caught by
        ``promote_pending_drafts``'s own fail-open ``except Exception`` — the
        SAME contract every other internal judge failure already gets, so the
        drafts stay unjudged and the next launch simply retries.
        """
        if not _eng.MEMORIAS_ENABLED:
            return "memorias_disabled"
        if not self._judge_model():
            return "no_local_model"
        if self._pending_model_switch or self._awaiting_first_success_after_switch:
            return "model_switch_pending"
        if self._current_profile_id is None:
            return "no_profile"
        return ""

    def promote_pending_drafts(self, *, chat_callable=None) -> dict:
        """ONE LLM call: judge, rewrite and promote this profile's oldest
        unjudged drafts (memory_promotion_20260725).

        Per-turn capture is cheap, permissive and LLM-free; nothing ever filtered
        afterward, so the store fills with vague half-memories Kira then recites.
        This is the missing step.

        Synchronous, on the engine thread, and FULLY isolated (``scout_digest``
        precedent): every internal failure — timeout, malformed JSON, provider
        offline, sqlite lock — returns counts-only and leaves every draft
        untouched and UNJUDGED, so the next launch simply retries. A failed
        promotion can never lose a memory: this method never deletes and never
        demotes.

        *chat_callable* is the test seam, threaded straight into
        ``_ollama_chat_with_watchdog`` (the same boundary the Topic Scout tests
        fake). Returns counts ONLY — RC-8: no memory text ever reaches a log.

        ``counts["skipped"]`` names the gate that blocked a NOT-ATTEMPTED sweep
        and is "" whenever the sweep genuinely reached the store. The one-shot
        latch in ``run()`` reads it: arming on the attempt instead of the outcome
        meant a single transient not-ready first idle tick (the profile is seeded
        AFTER ``motor.start()`` on the API surface; Ollama may not be up yet on
        the GUI one) disabled promotion for the whole process, silently.
        """
        reasons: Counter = Counter()
        counts = {
            "considered": 0, "kept": 0, "rejected": 0, "stale": 0,
            "unjudged_remaining": 0, "reasons": reasons, "skipped": "",
        }
        try:
            # scout_digest's gates MINUS its reasoning value gate (see D2).
            gate = self._promotion_gate()
            if gate:
                counts["skipped"] = gate
                # Once per CHANGED gate state, so a permanently inert sweep is
                # observable (owner decision 8) without a log line every second.
                if gate != self._promotion_last_gate:
                    self._promotion_last_gate = gate
                    _eng.logger.info("memoria promotion sweep gated: %s", gate)
                return counts
            self._promotion_last_gate = ""
            profile_id = self._current_profile_id

            store = self._get_memoria_store()
            drafts = store.list_unjudged_drafts(profile_id, limit=_eng._PROMOTION_DRAFT_BATCH)
            if not drafts:
                return counts  # the common no-op: zero tokens, no call at all
            counts["considered"] = len(drafts)

            # No dedup step, deliberately: the design's "drop any draft whose
            # stable_key is already durable" can never fire. UNIQUE(profile_id,
            # stable_key) is a TABLE constraint, so that state is unreachable —
            # a re-capture of an already-durable exchange resolves upsert_draft
            # to a no-op and inserts nothing. Re-judging a promoted row is
            # equally impossible (list_unjudged_drafts filters status='draft'
            # AND judged_at=''). Both would have been dead code behind a test
            # that only passes on a hand-forged database.
            batch = drafts

            if batch:
                kept: list[tuple[str, int]] = []
                rejected: list[tuple[str, int]] = []
                for index, judged_text, uncertain, reason in self._run_promotion_judge(
                    batch, chat_callable=chat_callable,
                ):
                    row = batch[index - 1]
                    if judged_text is None:
                        reasons[reason] += 1
                        rejected.append((row["id"], row["revision"]))
                        continue
                    # Two optimistic-concurrency tokens, because the operator has
                    # two ways to touch a row while the model is thinking.
                    # `if_revision` catches a re-CAPTURE (upsert_draft bumps it);
                    # `if_status` catches a hand EDIT, which goes through
                    # update_row and sets status='curated' WITHOUT bumping
                    # revision — without it the judge's rewrite of the now-stale
                    # text would overwrite the operator's own words and demote
                    # curated (operator intent) to promoted (machine).
                    # `touch_updated_at=False` keeps the row's place in the
                    # recency ranking build_recency_lines uses; the judgment
                    # rewrites text about an OLD conversation.
                    # An UNCERTAIN keep gets its rewritten text but stays `draft`
                    # (owner decision 4: keep and MARK) — the shipped `draft`
                    # badge already means "provisional, not operator-confirmed".
                    # `status=` must be passed explicitly: update_row's default
                    # is 'curated' (the F5 operator-edit contract), a provenance
                    # lie either way.
                    try:
                        written = store.update_row(
                            row["id"],
                            title=_eng.build_title(judged_text),
                            content=judged_text,
                            signature=_eng.build_signature(judged_text),
                            if_revision=row["revision"],
                            if_status="draft",
                            status="draft" if uncertain else "promoted",
                            touch_updated_at=False,
                            raising=True,
                        )
                    except sqlite3.Error:
                        # raising=True is what makes a plain False mean ONLY
                        # rowcount==0, so `stale` keeps its documented meaning
                        # ("the operator was speaking on this topic") instead of
                        # silently absorbing every locked-database write.
                        reasons["write_failed"] += 1
                        continue
                    if not written:
                        reasons["stale"] += 1
                        counts["stale"] += 1
                        continue
                    if uncertain:
                        reasons["uncertain_entity"] += 1
                    kept.append((row["id"], row["revision"]))

                # mark_judged, never set_flags: set_flags bumps updated_at, and
                # _prune_profile keeps the newest drafts by updated_at — routing
                # a rejection through it would evict a newer unjudged draft.
                # Keeps are counted from the writes that ALREADY landed on disk:
                # a failed bookkeeping stamp must never report kept=0 on a launch
                # that genuinely promoted rows (owner decision 8).
                if kept:
                    counts["kept"] = len(kept)
                    try:
                        stamped = store.mark_judged(kept, raising=True)
                    except sqlite3.Error:
                        reasons["write_failed"] += len(kept)
                    else:
                        # The text is already on disk; only the stamp is missing,
                        # so the row is simply re-judged next launch.
                        if stamped < len(kept):
                            reasons["unstamped"] += len(kept) - stamped
                if rejected:
                    try:
                        # `if_status="draft"` ONLY here. A reject performs no
                        # earlier guarded write, so this call carries BOTH of its
                        # race checks: `revision` catches a re-CAPTURE, `status`
                        # catches an operator EDIT or PIN (which write 'curated'
                        # WITHOUT bumping revision) — otherwise a memory he just
                        # curated is hidden on a judgment of the text he replaced,
                        # permanently, since upsert_draft's un-hiding CASE only
                        # fires on rows still in draft. It must NOT be passed on
                        # the keep call above: `update_row` has already written
                        # 'promoted' by then, so the same guard would leave every
                        # confident keep unstamped and re-judged every launch.
                        stamped = store.mark_judged(
                            rejected, inactive=True, if_status="draft", raising=True,
                        )
                    except sqlite3.Error:
                        reasons["write_failed"] += len(rejected)
                    else:
                        counts["rejected"] = stamped
                        lost = len(rejected) - stamped
                        if lost:
                            reasons["stale"] += lost
                            counts["stale"] += lost

            counts["unjudged_remaining"] = len(
                store.list_unjudged_drafts(profile_id, limit=_eng.MEMORIAS_PROFILE_CAP)
            )
            _eng.logger.info(
                "memoria promotion sweep: considered=%d kept=%d rejected=%d stale=%d "
                "remaining=%d reasons=%s",
                counts["considered"], counts["kept"], counts["rejected"],
                counts["stale"], counts["unjudged_remaining"], dict(reasons),
            )
        except Exception as exc:
            # Total isolation: nothing was marked judged, so the next launch
            # retries. Type only — never a message that could carry row text.
            _eng.logger.warning("memoria promotion sweep failed (fail-open): %s", type(exc).__name__)
        return counts

    def memory_inspector_snapshot(self) -> dict:
        """Read-only, privacy-gated snapshot of session memory for the
        "Memoria de Kira" UI inspector (cards_memory_readonly_panels_20260701).

        Snapshot-then-release (precedent: scout_digest): copies historial
        entries + digest stats to plain dicts under _history_lock, releases
        the lock, then formats. Never mutates historial or the digest.

        Content policy (fail-closed, no cross-module string heuristics):
          - user-slot entries: 'content' key present ONLY when source == "direct"
          - assistant-slot entries: 'content' key present when source is in
            _DIGEST_CAPTURE_SOURCES ({"direct", "ptt"}) — Kira's own on-air
            words are safe to show even for a ptt-sourced turn.
          - everything else (chat/accumulated/kira-agenda*/unknown/missing
            source): NO 'content' key at all.

        Returns:
            {
              "entries": [{"turn_index", "role", "source", "content_chars", ["content"]}],
              "source_breakdown": collections.Counter over entry sources,
              "digest": {"line_count", "total_chars", "max_chars"} — stats
                  only, never digest line text (compacted lines are
                  unattributable and can carry ptt-template junk pre-T1.1).
            }
        """
        with self._history_lock:
            raw_entries = list(self.historial)
            digest_lines = list(self._memory_digest.lines)
            digest_max_chars = self._memory_digest._max_chars

        entries: list[dict] = []
        for idx, raw_entry in enumerate(raw_entries):
            role = raw_entry.get("role")
            source = raw_entry.get("source")
            content = raw_entry.get("content", "") or ""
            entry = {
                "turn_index": idx,
                "role": role,
                "source": source,
                "content_chars": len(content),
            }
            show_content = (
                (role == "user" and source == "direct")
                or (role == "assistant" and source in _eng._DIGEST_CAPTURE_SOURCES)
            )
            if show_content:
                entry["content"] = content
            entries.append(entry)

        return {
            "entries": entries,
            "source_breakdown": Counter(e["source"] for e in entries),
            "digest": {
                "line_count": len(digest_lines),
                "total_chars": sum(len(line) for line in digest_lines),
                "max_chars": digest_max_chars,
            },
        }