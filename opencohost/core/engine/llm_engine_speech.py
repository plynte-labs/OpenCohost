"""
opencohost/core/engine/llm_engine_speech.py

TTS synthesis, playback, the speech router seam, and the PTT cue of
MotorVocalIA. Mixin extracted from llm_engine.py; all runtime state stays on
MotorVocalIA.
"""
import os
import queue
import re
import requests
import threading
import time
import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # never imported at runtime
    from opencohost.core.speech.router import SpeechRouter

# `Optional` is imported directly, not via `_eng`, because it appears where the
# CLASS BODY evaluates (annotations), which runs during llm_engine's PARTIAL
# import -- there `_eng.X` resolves only for names bound above its mixin-import
# line. Annotations want a direct import; runtime values want `_eng`.
#
# `SpeechRouter` is the exception that proves the rule, and it is the THIRD option
# the rule does not mention: `_ensure_router` annotates `-> "SpeechRouter"` as a
# STRING, and a string annotation is never evaluated at all. So it needs no import
# to be correct -- making it a bare name would be strictly WORSE, because that
# would start evaluating at class-body time where the string does not. The
# TYPE_CHECKING import above exists only so editors and type checkers can resolve
# the name; it does not run. Construction stays on `_eng.SpeechRouter`.
#
# NOT imported here, deliberately: `winsound`. llm_engine.py:21-24 wraps it in a
# try/except because it is Windows-only stdlib, and `play_ptt_cue` reads
# `winsound is None` to no-op off-Windows -- a bare `import winsound` here would
# raise on those hosts instead, and the None branch would be dead code on every
# host. It is also REBOUND by the suite (`monkeypatch.setattr(llm, "winsound",
# spy)`, test_ptt_cue.py:131 and :189), so a local import would keep the real
# module and drive a real audio device. Both reasons point the same way: `_eng`.
from opencohost.core import llm_engine as _eng


class SpeechPipelineMixin:
    @property
    def _speech_active(self) -> bool:
        """ACTIVE ∨ INCOMING ∨ STACK, plus the raw `_speaking` flag (design
        §11 B2).

        The INCOMING clause is the load-bearing one: without it the
        submit->pick window is open ON THE SUBMITTING THREAD — submit returns,
        `_complete_processing_cycle` runs, `_process_priority_queue` tests its
        gate against False and pops a second item mid-speech.

        `_speaking` stays in the predicate so the legacy direct-`_hablar` path
        (kill switch OFF, and CTK, which never arms the router) keeps exactly
        today's semantics. It is never NARROWER than before, only wider.
        """
        with self._lock:
            speaking = self._speaking
        if speaking:
            return True
        router = self._speech_router
        # Read outside `_lock`: `_sched_lock` is a leaf and must never be
        # taken under another engine lock (design §4 I10).
        return router is not None and router.has_work()
    
    def interrupt_speaking(self) -> None:
        """Public interrupt: stop the in-flight speech consumer immediately.

        Sets the speaking flag False under the engine lock so the _hablar
        consumer loop exits without draining the pre-filled audio queue. This
        is the supported way for the UI to interrupt speech — callers must NOT
        reach into _lock/_speaking directly (ADR-AUD-005 Demeter fix, FR1).

        NOTE: _lock is a plain threading.Lock (not reentrant). Call this method
        only from outside any code path that already holds self._lock.
        """
        with self._lock:
            self._speaking = False

    def speech_remaining_estimate(self) -> Optional[float]:
        """WU4 4c seam (design-fase2.md §3): best-effort remaining-playback
        estimate, in seconds.

        Derived from the REAL `_hablar_impl` consumer loop's own progress
        counters (`_speech_progress`: total fragments / fragments played so
        far / wall-clock start / `first_play`) — the actual seam the TTS
        pipeline already tracks, not a separate timer. Formula:
        `remaining_fragments * mean_seconds_per_played_fragment`. Returns
        None while not speaking, or before the first fragment has finished
        playing (no rate to extrapolate from yet).

        T2(b) [v5]: the mean is measured from `first_play` (monotonic, set
        the moment the FIRST fragment's playback actually started) when
        available, not from `start` (wall-clock, set when synthesis of the
        WHOLE utterance began). Using `start` inflates the mean by the
        synthesis wait before playback ever began — a slow-to-synthesize
        first fragment would otherwise make every later fragment look far
        slower than its real playback rate. Falls back to the legacy
        `start`-based measurement when `first_play` is absent (e.g. a
        test-constructed progress dict), so behavior is unchanged there.

        DELIBERATELY still on `_speaking`, not `_speech_active` (design §11):
        this predicate's consumers need "audio playing RIGHT NOW". A queued or
        suspended job has no rate to extrapolate from, and the connector
        upgrade would spawn an Ollama call on a false GPU-free premise.
        """
        with self._lock:
            if not self._speaking:
                return None
            progress = self._speech_progress
        if not progress:
            return None
        played = progress["played"]
        # Judge closure (2026-08-05): `played`/`total` are TURN-absolute
        # (they include a resume's `cursor_base`), but the rate must stay
        # local to THIS invocation — `first_play`/`start` belong to the
        # resumed slice, and fragments played before the hold carry no
        # information about the post-hold playback rate. `base` is 0 on any
        # non-resume invocation and absent on a test-constructed dict, so
        # legacy behavior is bit-identical there.
        played_here = played - progress.get("base", 0)
        if played_here <= 0:
            return None
        first_play = progress.get("first_play")
        if first_play is not None:
            elapsed = time.monotonic() - first_play
        else:
            elapsed = time.time() - progress["start"]
        mean_per_fragment = elapsed / played_here
        remaining_fragments = max(0, progress["total"] - played)
        return remaining_fragments * mean_per_fragment

    # ── WU5 D2/D3: frozen-stash detour return + connector (ADR-038; the D1
    # position cut was retired at router step 5) ──

    

    def mark_audio_suspect(self) -> None:
        """Flag the pygame mixer as possibly zombied so the NEXT _hablar()
        call quits+re-inits it before playing its first chunk.

        Thread-safe (bool + lock, cheap latch): called from the PTT WS thread
        on session teardown (opencohost/api/ptt_session.py) and from the
        playback consumer itself after a chunk raises. The actual
        pygame.mixer.quit()/init() only ever runs on this engine's own
        thread, inside _hablar — external callers never touch pygame.
        """
        with self._lock:
            self._audio_reinit_needed = True

    def play_ptt_cue(self) -> None:
        """Play a short, low-volume blip the instant a PTT hold starts listening.

        A nicety so the operator knows the mic is live while gaming with the
        app window unfocused/minimized: the frontend pauses its polls in the
        background, so a UI-side sound would not fire reliably. Wired from
        ``PttController.start`` (opencohost/api/ptt_session.py) via the
        ``on_listening`` hook, on the HTTP handler thread.

        Plays through ``winsound`` (a separate Windows audio path with ZERO
        pygame/SDL interaction), so it is safe to fire from any thread — it
        shares no state with the engine's pygame mixer, which the voice-death
        reinit quits+re-inits on its own thread after every PTT close. The cue
        is fire-and-forget (``SND_ASYNC``) and fail-open: ``PTT_CUE_ENABLED``
        gates it off entirely, a non-Windows host (no ``winsound``) no-ops, and
        every error is swallowed — a cue that fails must never break PTT start.

        Bug fix (2026-08-09): this asked for ``SND_MEMORY | SND_ASYNC``, which
        winsound rejects outright ("Cannot play asynchronously from memory"),
        so the cue had never played once — the fail-open except swallowed it
        and every test mocks winsound. Async is the load-bearing half (this
        runs on the HTTP handler thread, mid PTT start), so the bytes go to a
        cached temp file and play with ``SND_FILENAME`` instead.
        """
        if not _eng.PTT_CUE_ENABLED or _eng.winsound is None:
            return
        try:
            _eng.winsound.PlaySound(
                self._ptt_cue_wav_file(),
                _eng.winsound.SND_FILENAME | _eng.winsound.SND_ASYNC | _eng.winsound.SND_NODEFAULT,
            )
        except Exception:
            _eng.logger.debug("PTT cue playback skipped (fail-open)", exc_info=True)

    def _ptt_cue_wav_file(self) -> str:
        """Path to the cue WAV on disk, written once from ``_ptt_cue_wav()``.

        winsound can play asynchronously from a FILE or synchronously from
        MEMORY, never asynchronously from memory — and a synchronous cue would
        block PTT start for the blip's whole duration. So the cached bytes are
        spilled to one temp file per process and replayed from there forever.
        """
        if self._ptt_cue_wav_path is not None:
            return self._ptt_cue_wav_path
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="ptt_cue_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self._ptt_cue_wav())
        except Exception:
            os.unlink(path)
            raise
        # ponytail: never unlinked -- one small WAV per process, reclaimed by
        # the OS temp sweep. A finalizer would race the async playback.
        self._ptt_cue_wav_path = path
        return path

    def _ptt_cue_wav(self) -> bytes:
        """Lazily build and cache the PTT blip as an in-memory WAV file.

        Pure stdlib (no numpy): a soft ~120 ms 880 Hz sine, 44100 Hz mono
        16-bit, with a short fade in/out to kill click artifacts.
        ``PTT_CUE_VOLUME`` is baked into the sample amplitude because winsound
        has no volume control. The bytes are immutable and independent of the
        pygame mixer, so they are built once and cached forever.
        """
        if self._ptt_cue_wav_bytes is not None:
            return self._ptt_cue_wav_bytes
        import io
        import math
        import struct
        import wave

        rate = 44100
        n = max(1, int(rate * 0.12))
        fade = max(1, n // 8)
        amp = max(0.0, min(1.0, _eng.PTT_CUE_VOLUME)) * 32767.0
        frames = bytearray()
        for i in range(n):
            if i < fade:
                env = i / fade
            elif i >= n - fade:
                env = (n - i) / fade
            else:
                env = 1.0
            sample = int(math.sin(2.0 * math.pi * 880.0 * i / rate) * env * amp)
            frames += struct.pack("<h", sample)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(bytes(frames))
        self._ptt_cue_wav_bytes = buf.getvalue()
        return self._ptt_cue_wav_bytes

    def cancel_speech_for_sources(self, prefixes: tuple[str, ...]) -> None:
        """Refuse to START any upcoming _hablar() whose source matches a prefix.

        Called by the emergency paths BEFORE interrupt_speaking() and
        drop_pending_sources() so no window exists where a turn already popped
        from the priority queue (Bug B straggler) slips into _hablar between
        interrupt and drop. Scoped to ("kira-agenda",) so a concurrent
        direct/PTT reply is not silenced. Cleared only on agenda enable.
        """
        with self._lock:
            self._cancelled_speech_prefixes = tuple(prefixes)
        # Step 3 (design §4 sweeps): a stack entry at any depth matching one
        # of these prefixes is discarded too — the emergency stop must not
        # leave a `kira-agenda*` beat resumable after the token is set.
        router = self._speech_router
        if router is not None:
            router.sweep_sources(prefixes)

    def clear_speech_cancel(self) -> None:
        """Clear the speech-cancellation token. Called only on agenda enable."""
        with self._lock:
            self._cancelled_speech_prefixes = ()

    def _maybe_notify_piper_locale_mismatch(self) -> None:
        """One-shot ui_callback notice fired the first time Piper actually
        engages as the TTS fallback while its voice language disagrees with
        the active locale (honest degrade — never silent Spanish audio under
        locale=en). Per-process, not per-utterance: fires once, then no-ops.
        """
        if self._piper_locale_mismatch_notified:
            return
        voice_lang = _eng.PIPER_VOICES.get(self._piper_voice_key, {}).get("lang", "")
        locale_lang = _eng.i18n_coherence.primary_subtag(_eng.i18n_active.get_active_bundle().code)
        if voice_lang and locale_lang and voice_lang != locale_lang:
            self._piper_locale_mismatch_notified = True
            self._log(
                "Piper fallback engaged with a voice/locale language mismatch "
                f"(voice={voice_lang!r} locale={locale_lang!r}); offline audio "
                "will not match the active locale.",
                level="warning",
            )
            self.ui_callback(_eng.i18n_coherence.PIPER_VOICE_LOCALE_MISMATCH)
            
    @staticmethod
    def _sanitize_tts_text_for_playback(text: str) -> str:
        """Strip Markdown emphasis markers and non-Latin script glyphs, without
        deleting otherwise-speakable text.

        Screen/speech split: this runs inside _hablar_impl, AFTER _emit_dialogue
        already forwarded the original (unfiltered) string to the screen sink —
        the screen keeps CJK/etc glyphs, only the TTS-bound copy is filtered.
        """
        return _eng._sanitize_tts_text_for_playback(text)

    @staticmethod
    def _split_for_tts(texto_a_generar: str) -> list:
        """Sanitize `texto_a_generar`, then fragment it into TTS-ready chunks.

        Two distinct stages, not one expression -- `_sanitize_for_tts` then
        `_fragment_for_tts` -- because the streaming design needs to run
        output_guard BETWEEN them, at sentence granularity:

          - guarding the RAW (pre-sanitize) text reproduces the
            markdown-evasion bug fixed in f07b360 (asterisk-wrapped
            violations survive the guard, then the sanitizer strips the
            asterisks and speaks the violation clean);
          - guarding the final TTS FRAGMENT (post comma-sub-split) is also
            wrong -- the sub-split lands inside R4's `listo,\\s+ya\\s+lo\\s+hice`
            pattern and between R3's discourse marker and its outcome, so
            both rules miss at fragment granularity and hit at sentence
            granularity (measured).

        So the correct order is sanitize -> guard(sentence) -> fragment.
        This method only makes that order expressible; it does not call the
        guard itself.
        """
        return SpeechPipelineMixin._fragment_for_tts(
            SpeechPipelineMixin._sanitize_for_tts(texto_a_generar)
        )

    @staticmethod
    def _sanitize_for_tts(texto_a_generar: str) -> list:
        """Stage 1: strip markdown/quotes/newlines, then split into
        sentences on terminator+whitespace (B_ws -- the same boundary
        production TTS ships today, `re.split(r'(?<=[.!?])\\s+', ...)`).

        Returns the sentence list: the granularity the output_guard is
        meant to run at, once that seam is wired.
        """
        texto_limpio = SpeechPipelineMixin._sanitize_tts_text_for_playback(texto_a_generar)
        texto_limpio = texto_limpio.replace('"', '').replace('\n', ' ')
        return re.split(r'(?<=[.!?])\s+', texto_limpio)

    @staticmethod
    def _fragment_for_tts(oraciones_saneadas: list) -> list:
        """Stage 2: sub-split any sentence over 25 words on internal
        commas/semicolons, regrouping at >=8 words, producing the final
        TTS-synthesizable chunks."""
        oraciones = []

        MIN_PALABRAS_POR_CHUNK = 8
        MAX_PALABRAS_POR_CHUNK = 25

        for frag in oraciones_saneadas:
            frag = frag.strip()
            if not frag: continue

            if len(frag.split()) > MAX_PALABRAS_POR_CHUNK:
                sub_frags = re.split(r'(?<=[,;])\s+', frag)
                temp_chunk = ""
                for sub in sub_frags:
                    temp_chunk += sub + " "
                    if len(temp_chunk.split()) >= MIN_PALABRAS_POR_CHUNK:
                        oraciones.append(temp_chunk.strip())
                        temp_chunk = ""
                if temp_chunk.strip():
                    oraciones.append(temp_chunk.strip())
            else:
                oraciones.append(frag)

        return [o for o in oraciones if len(o) > 3]

    def _sanitize_agenda_output(self, text: str) -> str:
        """Last line of defense for autonomous agenda speech."""
        clean = " ".join((text or "").strip().split())
        if not clean:
            return ""
        lowered = clean.lower()
        # Active locale's banned-closure phrases; None means the locale ships
        # none (guardrails domain, no cross-locale fallback). enable()'s
        # fail-closed gate now also requires agenda_banned_closures() is not
        # None, so in production this branch is unreachable while agenda
        # mode is enabled — kept as a defensive no-op since this method is
        # also exercised directly (e.g. in tests) outside that gate.
        banned = _eng.i18n_active.agenda_banned_closures()
        if banned and any(term in lowered for term in banned):
            self._log("Agenda: salida con cierre artificial detectada; usando fallback natural.", level="warning")
            return _eng.i18n_active.agenda_sanitizer_fallback()
        return clean

    def _log_clause_sanitizer(self, result, source: str, *, stage: str = "generate") -> None:
        """Metadata-only telemetry for the clause sanitizer.

        Owner rule: never log previews, raw dialogue, or the removed content —
        not even at DEBUG. Counts, ratios, source, stage and verdict only. These
        records are the evidence input for the threshold tuning pass and for any
        later decision to arm a non-agenda source.
        """
        _eng.logger.debug(
            "[CLAUSE_SANITIZER] stage=%s source=%s verdict=%s removed=%d distinct=%d "
            "max_occ=%d orig_len=%d remaining_len=%d removed_pct=%.3f",
            stage, source, result.verdict, result.removed_fragments,
            result.distinct_looping, result.max_occurrences,
            result.original_len, result.remaining_len, result.removed_pct,
        )

    def _speak_or_submit(self, dialogo, source: str = "direct", priority: Optional[int] = None) -> None:
        """The ONE seam every spoken line goes through (design §8 step 2).

        Router armed: hand the text to the router thread and return
        immediately — playback no longer occupies the calling thread.
        Kill switch OFF: the legacy direct, BLOCKING `_hablar`, byte-identical
        to what each of the four converted call sites did before.

        `priority` defaults to the source's band; a caller passes it
        explicitly when the source tag would lie about the content's urgency
        (see the cloud-fallback notice).
        """
        if not self._speech_router_enabled:
            self._hablar(dialogo, source=source)
            # Measure-first telemetry seam: a chat turn actually played to
            # completion — advance the spoken clock so secs_since_last_spoken
            # stays honest vs a should_call that may later expire via TTL.
            # Chat-only; no-op when unset. Intentionally NOT in a finally: if
            # `_hablar` raises (TTS failure) Kira did NOT speak, so the spoken
            # gap should keep growing — that growing gap is the very signal
            # that surfaces silent TTS failures to the operator. The router
            # reproduces exactly this rule at job completion (FINISHED only).
            if source == "chat" and self.on_chat_turn_spoken is not None:
                try:
                    self.on_chat_turn_spoken()
                except Exception:
                    pass
            return
        router = self._ensure_router()
        router.submit(
            dialogo,
            source,
            _eng.priority_for_source(source) if priority is None else priority,
        )

    def _ensure_router(self) -> "SpeechRouter":
        """Lazy-build-then-start, extracted from `_speak_or_submit` (step 3)
        so `pause_speech_for_ptt` can arm the router on a press that lands
        BEFORE any speech this process ever submitted — a hold must block
        `_pick()` even when nothing is playing yet. Idempotent; copies the
        step-3 kill switch onto the router at build time.
        """
        router = self._speech_router
        if router is None:
            with self._lock:
                if self._speech_router is None:
                    # No flag copy here (judge closure 2026-08-05): the
                    # router reads `_speech_interrupt_enabled` LIVE via its
                    # `interrupt_enabled` property, so the switch can be
                    # disarmed in-process and the host's two flag writes
                    # have no ordering requirement.
                    self._speech_router = _eng.SpeechRouter(self)
                router = self._speech_router
        router.start()  # idempotent
        return router

    def pause_speech_for_ptt(self) -> None:
        """PTT_DOWN entrypoint (design §0 row 1, §5.1): fail-open, a no-op
        unless BOTH switches are armed. Judge closure (2026-08-05, MAJOR):
        step 3 requires the router to BE the playback path — with only
        `_speech_interrupt_enabled` on (the documented router-only revert
        flips `_speech_router_enabled` alone), a press built a router no
        job would ever reach, and the closure-B1 re-check then silently
        deleted every LEGACY turn spoken during the hold (no stack to
        suspend to, no discard log). Router-off strictly dominates. The
        hold and the victim's request publish in ONE `_sched_lock`
        acquisition (`hold_and_pause`) — a press landing before this
        process ever spoke still blocks `_pick()`."""
        if not (self._speech_interrupt_enabled and self._speech_router_enabled):
            return
        self._ensure_router().hold_and_pause("ptt")

    def resume_speech_after_ptt(self) -> None:
        """PTT_UP entrypoint (design §5.1's `on_release` funnel): fail-open,
        a no-op unless BOTH switches are armed (same conjunction as the
        press — router-off dominates). Idempotent — every PTT exit path may
        call this, including a double-clear."""
        if not (self._speech_interrupt_enabled and self._speech_router_enabled):
            return
        router = self._ensure_router()
        router.set_ptt_held(False)

    def _speech_cancelled(self, source: str) -> bool:
        """True when an emergency path cancelled this source's speech.

        Extracted from `_hablar_impl`'s entry guard (Bug B straggler) so the
        router can refuse a job BEFORE any boundary event exists for consumers
        to react to (design §2 reconcile / §11 B1).
        """
        with self._lock:
            cancelled = self._cancelled_speech_prefixes
        return bool(cancelled) and any(source.startswith(p) for p in cancelled)

    def _speech_pause_pending(self) -> bool:
        """Closure B1 (2026-08-05): a pause/hold requested for the ACTIVE
        router job. `_hablar_impl` consults this right after (re)arming
        `_speaking` — an interrupt that landed in the router's pick→arm gap
        was stomped by that assignment, and this is what un-stomps it.
        Always False on the legacy path: the conjunction (judge closure
        2026-08-05) keeps this from cutting a LEGACY `_hablar_impl` call to
        silence when only the interrupt flag is armed — on the legacy path
        there is no job, no stack and no resume, so a True here deleted the
        turn outright."""
        if not (self._speech_interrupt_enabled and self._speech_router_enabled):
            return False
        router = self._speech_router
        if router is None:
            return False
        return router.pause_pending_for_active()

    def _speech_boundary_start(self, source: str) -> None:
        """Open a speech boundary: arm the cut flag, publish the source, emit
        `speaking_start`.

        Step 2 (design §11 B1): this LEFT `_hablar_impl`. A job is one boundary
        pair no matter how many `_hablar` invocations it takes (a retry today, a
        resume at step 3), and a zero-fragment job no longer emits a second
        `speaking_end` from inside the invocation. The emitter is the ROUTER
        when it is armed, and `_hablar` itself on the legacy direct path.

        A raising `speaking_start` consumer must not strand `_speaking` /
        `_current_speech_source` (pinned semantic, test_llm_engine_timeouts) —
        both are cleared here and the exception keeps propagating.
        """
        with self._lock:
            self._speaking = True
            self._current_speech_source = source
            # T1(a) [v5]: monotonic start of THIS turn's speech, paired with
            # `_speech_boundary_end` to record the previous turn's own speech
            # duration for the `speech_ms=` boundary telemetry field.
            self._speaking_start_monotonic = time.monotonic()
        try:
            self.ui_callback("speaking_start")
        except Exception:
            with self._lock:
                self._speaking = False
                self._current_speech_source = None
                self._speaking_start_monotonic = None
            _eng.logger.exception("UI callback failed during speaking_start")
            raise

    def _speech_boundary_end(self) -> None:
        """Close a speech boundary: disarm, clear the source, emit
        `speaking_end`. Counterpart of `_speech_boundary_start` (design §11 B1).

        `_current_speech_source` is cleared HERE, not in `_hablar_impl`'s tail:
        `agenda_driver._has_non_agenda_audio_work` reads `is_speaking` and
        `current_speech_source` together, and clearing the tag while the job is
        still ACTIVE would read as "non-agenda audio work".
        """
        with self._lock:
            self._speaking = False
            self._current_speech_source = None
            now = time.monotonic()
            self._last_speaking_end_monotonic = now
            # T1(a) [v5]: this turn's own speech duration, recorded as the
            # "previous turn" value the NEXT boundary's speech_ms reads.
            if self._speaking_start_monotonic is not None:
                self._last_speech_duration_ms = int((now - self._speaking_start_monotonic) * 1000)
            self._speaking_start_monotonic = None
        self.ui_callback("speaking_end")

    def _hablar(self, texto_a_generar, source: str = "direct", *, emit_boundary: bool = True,
                pre_split: Optional[list] = None, cursor_base: int = 0):
        """WU2b belt lock (design-fase2.md §2.5): serialize every _hablar caller.

        Thin wrapper around _hablar_impl. After WU2 the engine worker is the ONLY
        caller in the API host, so the non-blocking acquire always succeeds there
        and contention is never logged (a contention log is a bypass regression).
        In the CTK legacy path (play_prefetched_agenda's speaker thread) this lock
        serializes that thread against the worker — the log then = the lock
        working. Both call sites are terminal/non-recursive (neither _hablar_impl
        nor its callbacks re-enter _hablar), so the non-reentrant Lock never
        self-deadlocks; it is released in finally on every exit path.

        Step 2: `emit_boundary=False` is the ROUTER's call — it owns one
        `speaking_start`/`speaking_end` pair per JOB and would otherwise get a
        second pair per invocation (design §11 B1). Every other caller keeps the
        invocation-scoped pair, which is exactly today's behavior.

        Step 3: `pre_split`, when not None, is forwarded to `_hablar_impl`
        verbatim — a resume replays the router's own owed slice, never a
        re-chunked rejoin (design §1 resolution 1).
        """
        if self._speech_cancelled(source):
            # Bug B fix: refuse a turn whose source was cancelled during its
            # generation phase (already popped from the priority queue, so
            # drop_pending_sources can't reach it). Checked BEFORE the boundary
            # opens so an emergency-stopped straggler emits no event pair at all.
            self._log(f"Habla suprimida (cancelada): source={source}", level="warning")
            return _eng.SpeechOutcome(chunks=[], cursor=0, spoken=[], skipped=[], interrupted=False, error=None)
        # The boundary pair lives INSIDE the belt-lock critical section — the
        # same discipline `_hablar_impl` had when it emitted the pair itself.
        # Emitting `speaking_start` before the acquire (or `speaking_end`
        # after the release) lets two concurrent legacy callers (CTK's
        # `play_prefetched_agenda` speaker, `CloudFallbackWarm`) interleave
        # nested pairs, clobber `_current_speech_source` mid-playback, and cut
        # the lock holder's utterance via the unlocked `_speaking = False`.
        if not self._hablar_lock.acquire(blocking=False):
            self._log("hablar contention (serialized)")
            self._hablar_lock.acquire()
        try:
            if emit_boundary:
                self._speech_boundary_start(source)
            try:
                # Self-caught: forward `pre_split`/`cursor_base` ONLY when
                # actually resuming (non-None). tests/test_pregen_pop_cache.py
                # stubs `_hablar_impl` with the pre-step-3 2-arg signature;
                # always forwarding `pre_split=None` broke that pre-existing,
                # unrelated stub with a TypeError. The router is the only
                # caller that ever passes a non-None slice.
                if pre_split is not None:
                    return self._hablar_impl(
                        texto_a_generar, source=source,
                        pre_split=pre_split, cursor_base=cursor_base,
                    )
                return self._hablar_impl(texto_a_generar, source=source)
            finally:
                if emit_boundary:
                    self._speech_boundary_end()
        finally:
            self._hablar_lock.release()

    def _hablar_impl(self, texto_a_generar, source: str = "direct", *,
                     pre_split: Optional[list] = None, cursor_base: int = 0):
        # Bug B fix (second line of defence): the token can still be set in the
        # window between the caller's own check and this frame. Mid-playback
        # truncation is handled separately by the _speaking guard in the
        # consumer loop via interrupt_speaking().
        with self._lock:
            cancelled = self._cancelled_speech_prefixes
        if cancelled and any(source.startswith(p) for p in cancelled):
            self._log(f"Habla suprimida (cancelada): source={source}", level="warning")
            return _eng.SpeechOutcome(chunks=[], cursor=0, spoken=[], skipped=[], interrupted=False, error=None)

        with self._lock:
            # `_speaking` SURVIVES as this method's cut mechanism (design §11):
            # the consumer loop and interrupt_speaking() both key off it. It is
            # not renamed and not replaced by `_speech_active`.
            self._speaking = True
            self._current_speech_source = source

        # Closure B1 (2026-08-05): a pause/hold published between the
        # router's window check and the re-arm above had its
        # interrupt_speaking() STOMPED by that assignment — the whole
        # utterance would play into a live mic. Re-honor it here, before any
        # fragment synthesises; the producer/consumer loop only ever
        # re-reads `_speaking`, and nothing re-arms it after this point.
        if self._speech_pause_pending():
            with self._lock:
                self._speaking = False

        # WU5 D3 (design-fase2.md §3 WU5): while an interruption ANSWER's TTS
        # plays (GPU free — generation already finished), opportunistically
        # generate the return connector. Placed HERE, at playback start, so it
        # never races the turn's own generation (single-Ollama rule). No-op
        # unless a frozen return is pending and this is a non-agenda turn.
        if not source.startswith("kira-agenda"):
            self._maybe_generate_connector_upgrade()

        ruta_absoluta_ref = os.path.abspath(self.voz_referencia) if self.voz_referencia else ""

        if pre_split is not None:
            # Step 3 (design §1 resolution 1): a RESUME. The chunker is NOT a
            # fixpoint — re-splitting `' '.join(pre_split)` can silently
            # re-merge comma-separated fragments across the join (a >25-word
            # comma-heavy fragment shrinks under the cap once its head is
            # dropped, so the splitter stops sub-splitting it and merges two
            # owed chunks into one). The router already knows the exact owed
            # slice; replay it verbatim, never re-derive it. Skips the
            # sanitizer/quote-newline scrub/splitter entirely.
            oraciones = list(pre_split)
        else:
            oraciones = self._split_for_tts(texto_a_generar)

        if not oraciones:
            self._log("⚠️ No se generaron oraciones válidas para sintetizar.", level="warning")
            # Step 2 (design §11 B1): the THIRD speaking_end site. It used to
            # fire from in here, so a zero-fragment job completed the agenda
            # turn (and idled the avatar) while the router still held the job
            # ACTIVE, then emitted a second end at FINISHED. The boundary owner
            # emits the single pair now.
            with self._lock:
                self._speaking = False
            return _eng.SpeechOutcome(chunks=[], cursor=0, spoken=[], skipped=[], interrupted=False, error=None)

        self._log(f"Sintetizando {len(oraciones)} fragmento(s) con pipeline...")
        start_tts = time.time()
        # WU4 4c seam (design-fase2.md §3): live progress counters for
        # speech_remaining_estimate() — updated as each fragment finishes
        # playing (below), cleared at speaking_end. `first_play` (T2(b)
        # [v5]) is set at the FIRST fragment's actual playback start, below.
        with self._lock:
            # Judge closure (2026-08-05, convergent): TURN-relative, never
            # slice-relative. On a resume `oraciones` is only the owed tail;
            # without `cursor_base` every played/total consumer (the WU5
            # zone gate, the would-fire telemetry) read a nearly-finished
            # resumed turn as barely started and un-earned its LATE-zone
            # defer protection. `base` anchors the slice so
            # `speech_remaining_estimate`'s per-fragment rate stays local
            # to THIS invocation (a pre-hold rate is stale after a hold of
            # unknown length).
            self._speech_progress = {
                "total": cursor_base + len(oraciones), "played": cursor_base,
                "start": start_tts, "first_play": None, "base": cursor_base,
            }

        cola_audios = queue.Queue(maxsize=3)
        error_count = 0
        # Step 1 ledgers (design §3/§6 I1): every fragment index ends in
        # exactly one of spoken/skipped/pending. `skipped` is mutated from
        # BOTH threads (the producer's _drop below, and the consumer's
        # playback-exception handler further down) -- safe without an extra
        # lock because list.append() is a single atomic bytecode op in
        # CPython, same tolerance the existing unsynchronized `error_count`
        # counter already relies on.
        skipped: list = []

        def productor():
            nonlocal error_count

            def _drop(i: int, why: str, exc: Optional[Exception] = None) -> None:
                # Step 0 (design §5.3, §8 step 0): the single collapse point
                # for every synthesis-side chunk loss. PRIVACY: `why` is a
                # closed set of short reason tags, never fragment/chat text.
                # `exc`, when given, restores the traceback for unknown/heavy
                # failures (design §12 observability) via exc_info -- passing
                # None is a no-op for logging, so this needs no branch.
                nonlocal error_count
                _eng.logger.warning(f"[SPEECH_LOST] idx={i} reason={why} source={source}", exc_info=exc)
                error_count += 1
                skipped.append(i)
                cola_audios.put(None)

            # Snapshot tts_local_only ONCE at utterance start so a mid-utterance
            # toggle cannot send remaining chunks to Edge-TTS.  The toggle takes
            # effect from the NEXT utterance only.
            local_only = self.tts_local_only
            # Same snapshot contract for the Edge-TTS rate: a mid-utterance
            # speed change applies from the next utterance only.
            edge_rate = _eng.edge_rate_for_length_scale(self._tts_length_scale)

            # Determine effective motor for this request
            effective_motor = self.motor_tts
            fallback_reason = ""

            # Health-based auto-fallback: check before heavy TTS
            # Missing-reference auto-fallback: must run before the health gate
            # so its "effective=pesado" log only fires when heavy TTS will
            # actually be used.
            if effective_motor == "pesado" and not self.voz_referencia:
                effective_motor = "ligero"
                fallback_reason = "missing_reference"
                self._log(
                    "Auto-fallback to Edge-TTS: "
                    f"requested=pesado effective=ligero reason={fallback_reason}"
                )

            if effective_motor == "pesado":
                hm = getattr(self, "health_monitor", None)
                if hm is not None:
                    block_reason = None
                    if hasattr(hm, "heavy_tts_block_reason"):
                        block_reason = hm.heavy_tts_block_reason(
                            auto_fallback_enabled=True,
                            manual_motor=effective_motor,
                        )
                    elif not hm.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor=effective_motor):
                        effective_motor = "ligero"
                        block_reason = "health_gate"
                    if block_reason:
                        fallback_reason = block_reason
                        effective_motor = "ligero"
                        self._log(
                            "Auto-fallback to Edge-TTS: "
                            f"requested=pesado effective=ligero reason={fallback_reason}"
                        )
                    else:
                        self._log("TTS efectivo: requested=pesado effective=pesado")

            for i, oracion in enumerate(oraciones):
                if not self._speaking:
                    break

                # Privacy fast-path: local_only is a snapshot taken at utterance
                # start (see top of productor()).  Using the snapshot ensures a
                # mid-utterance toggle cannot redirect remaining chunks to Edge-TTS;
                # the toggle applies from the next utterance only.
                # If Piper is unavailable, drop the chunk (degraded) rather than
                # silently re-enabling Edge-TTS (which would betray the privacy promise).
                if effective_motor == "ligero" and local_only:
                    self._maybe_notify_piper_locale_mismatch()
                    archivo_chunk_wav = os.path.join(
                        _eng.TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
                    )
                    if self._piper.is_available():
                        if self._piper.synthesize(oracion, archivo_chunk_wav):
                            cola_audios.put((archivo_chunk_wav, i, oracion))
                        else:
                            _drop(i, "piper_synth_failed")
                    else:
                        _drop(i, "piper_unavailable")
                    continue

                # Fast-path: Edge-TTS is known offline for this session (or the
                # package is not installed) — go straight to Piper without
                # attempting a network call.
                if effective_motor == "ligero" and (self._edge_tts_offline or _eng.edge_tts is None):
                    self._maybe_notify_piper_locale_mismatch()
                    archivo_chunk_wav = os.path.join(
                        _eng.TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
                    )
                    if self._piper.is_available():
                        if self._piper.synthesize(oracion, archivo_chunk_wav):
                            cola_audios.put((archivo_chunk_wav, i, oracion))
                        else:
                            _drop(i, "piper_synth_failed")
                    else:
                        # Piper gone / never loaded.
                        _drop(i, "piper_unavailable")
                    continue

                ext = ".mp3" if effective_motor == "ligero" else ".wav"
                archivo_chunk = os.path.join(_eng.TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}{ext}")
                try:
                    if effective_motor == "ligero":
                        async def generar_edge():
                            communicate = _eng.edge_tts.Communicate(
                                oracion, _eng.i18n_active.edge_voice(), rate=edge_rate
                            )
                            await communicate.save(archivo_chunk)

                        _eng.asyncio.run(_eng.asyncio.wait_for(generar_edge(), timeout=_eng.TTS_LIGHT_TIMEOUT))
                        cola_audios.put((archivo_chunk, i, oracion))
                    else:
                        # Heavy TTS path — measure generation time for RTF
                        gen_start = time.time()
                        respuesta = requests.post(
                            _eng.TTS_SERVER_URL,
                            json={
                                "texto": oracion,
                                "referencia": ruta_absoluta_ref,
                                "motor": effective_motor
                            },
                            timeout=_eng.TTS_HEAVY_TIMEOUT
                        )
                        gen_elapsed = time.time() - gen_start
                        if respuesta.status_code == 200:
                            with open(archivo_chunk, 'wb') as f:
                                f.write(respuesta.content)
                            cola_audios.put((archivo_chunk, i, oracion))

                            # Record RTF measurement
                            hm = getattr(self, "health_monitor", None)
                            if hm is not None:
                                try:
                                    # Estimate audio duration: ~15 chars/sec for Spanish
                                    estimated_duration = len(oracion) / 15.0
                                    hm.record_ttf_measurement(gen_elapsed, estimated_duration)
                                except Exception:
                                    pass  # Never break TTS for measurement failure
                        else:
                            _drop(i, "heavy_http_error")

                except requests.exceptions.ConnectionError:
                    self._log("ERROR: Servidor Qwen3-TTS no disponible.", level="error")
                    _drop(i, "heavy_connection_error")
                    continue

                except requests.exceptions.Timeout:
                    _drop(i, "heavy_timeout")

                except Exception as e:
                    # Connection-error detection: only network-offline errors trigger
                    # Piper fallback. asyncio.TimeoutError and other errors do NOT.
                    if effective_motor == "ligero" and _eng._is_connection_error(e):
                        self._edge_tts_offline = True
                        self._maybe_notify_piper_locale_mismatch()
                        if self._piper.is_available():
                            self._log(
                                "Edge-TTS sin conexion; usando TTS local (Piper) "
                                "por el resto de la sesion."
                            )
                            archivo_chunk_wav = os.path.join(
                                _eng.TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}.wav"
                            )
                            if self._piper.synthesize(oracion, archivo_chunk_wav):
                                cola_audios.put((archivo_chunk_wav, i, oracion))
                            else:
                                _drop(i, "edge_fallback_piper_synth_failed")
                        else:
                            self._log(
                                "TTS local no disponible: instala piper-tts y "
                                "configura TTS_LOCAL_MODEL_PATH",
                                level="warning",
                            )
                            _drop(i, "edge_fallback_piper_unavailable")
                        continue

                    if effective_motor == "ligero":
                        self._log("ERROR: Edge-TTS requiere internet. Si estas offline usa Pesado (Qwen3-TTS).", level="error")
                        _drop(i, "edge_ligero_failed")
                        continue
                    _drop(i, "heavy_unexpected_error", e)

            cola_audios.put("FIN")

        hilo_productor = threading.Thread(target=productor, daemon=True)
        hilo_productor.start()

        # Recovery: consume the suspect flag once, before the first chunk of
        # this turn plays. Gated on the flag (never unconditional per turn —
        # see the _audio_reinit_needed comment in __init__) because the CTk
        # app's AudioBedEngine shares this same mixer for background music;
        # PTT (the only setter of this flag besides a playback exception)
        # does not exist in the CTk app, so CTk never pays this reinit.
        with self._lock:
            reinit_needed = self._audio_reinit_needed
            self._audio_reinit_needed = False
        if reinit_needed:
            try:
                self.pygame.mixer.quit()
                self.pygame.mixer.init()
                self._log("Audio device re-inicializado (recovery)")
            except Exception as e:
                _eng.logger.warning(f"No se pudo re-inicializar pygame.mixer: {e}")

        chunks_played = 0
        # Step 1 ledgers (design §3 SpeechOutcome, §6 I1/I4). `cursor` defaults
        # to clean completion (== len(oraciones)) and is overwritten ONLY at an
        # actual cut point below -- never derived from `chunks_played`, which
        # increments before the interrupted break and never advances past a
        # failed synthesis (the CURSOR TRAP, design §3). `last_idx` lets the
        # rare pre-dequeue guard (no item in hand yet) still report a sane
        # best-effort cursor.
        spoken: list = []
        cursor = len(oraciones)
        last_idx = -1
        was_interrupted = False
        outcome_error = None
        try:
            while True:
                # Bug 4 fix: check _speaking before dequeuing the next chunk.
                # emergency_stop() sets _speaking=False externally; without this
                # guard the consumer drains the entire pre-filled queue even after
                # teardown is requested.
                with self._lock:
                    if not self._speaking:
                        was_interrupted = True
                        # ponytail: best-effort cursor -- no item is in hand at
                        # this guard, so the exact next idx is unknowable
                        # without dequeuing. Assumes no skip landed exactly in
                        # this narrow inter-fragment window; the router resumes
                        # from this cursor, so an off-by-one here replays or
                        # drops one fragment. Tighten only if that shows up.
                        cursor = last_idx + 1
                        _eng.logger.info(
                            f"[SPEECH_STACK] cut source={source} cursor={cursor} at=pre_dequeue"
                        )
                        break

                item = cola_audios.get(timeout=_eng.TTS_AUDIO_QUEUE_TIMEOUT)

                if item == "FIN":
                    break
                if item is None:
                    continue

                # Second _speaking check after dequeue — emergency_stop may have
                # fired while we were blocked on cola_audios.get().
                with self._lock:
                    if not self._speaking:
                        was_interrupted = True
                        cursor = item[1]  # the fragment about to play, never started
                        _eng.logger.info(
                            f"[SPEECH_STACK] cut source={source} cursor={cursor} at=pre_play"
                        )
                        try:
                            if os.path.exists(item[0]):
                                os.remove(item[0])
                        except (OSError, TypeError):
                            pass
                        break

                archivo_chunk, idx, oracion_texto = item
                last_idx = idx

                try:
                    if chunks_played == 0:
                        elapsed_first = time.time() - start_tts
                        self._log(f"🔊 Primer fragmento listo en {elapsed_first:.2f}s. Reproduciendo...")
                        # T2(b) [v5]: mark the FIRST fragment's real playback
                        # start (monotonic) — speech_remaining_estimate uses
                        # this as its baseline instead of `start` (set before
                        # synthesis even began) so a slow-to-synthesize first
                        # fragment never inflates the per-fragment mean.
                        with self._lock:
                            if self._speech_progress is not None:
                                self._speech_progress["first_play"] = time.monotonic()

                    self.pygame.mixer.music.load(archivo_chunk)
                    self.pygame.mixer.music.play()

                    while self.pygame.mixer.music.get_busy():
                        # Bug 4 fix: honour external _speaking=False inside the
                        # busy-wait so a playing chunk is stopped promptly on
                        # emergency teardown instead of draining to completion.
                        with self._lock:
                            if not self._speaking:
                                break
                        time.sleep(0.05)

                    # Bug 4 fix: if _speaking was cleared externally, stop the
                    # mixer explicitly before unload() — pygame keeps playing
                    # until stop() or end-of-track; unload() alone does not stop.
                    with self._lock:
                        interrupted = not self._speaking
                    if interrupted:
                        self.pygame.mixer.music.stop()

                    self.pygame.mixer.music.unload()
                    chunks_played += 1
                    with self._lock:
                        if self._speech_progress is not None:
                            # Turn-absolute (judge closure 2026-08-05):
                            # invocation-local count + the resume offset.
                            self._speech_progress["played"] = cursor_base + chunks_played

                    if interrupted:
                        # CURSOR TRAP (design §3): this is the cut MID-AUDIO --
                        # `idx` (never chunks_played) is the fragment owed a
                        # full replay, so it counts as CUT, NOT spoken.
                        was_interrupted = True
                        cursor = idx
                        _eng.logger.info(f"[SPEECH_STACK] cut source={source} cursor={idx}")
                        break
                    spoken.append(idx)

                except Exception as e:
                    # Bug fix (2026-07-15 PTT voice-death): a playback
                    # exception must count as a failed fragment, not just a
                    # log line — otherwise the mixer can be zombied (e.g. a
                    # migrated WASAPI stream) while every turn still reports
                    # "completado" and speaking_end fires as if nothing
                    # happened. error_count is _hablar's own local, not a
                    # nested closure, so no `nonlocal` is needed here.
                    error_count += 1
                    _eng.logger.warning(f"Error reproduciendo chunk {idx}: {e}")
                    self.mark_audio_suspect()
                    # Step 1 (design §5.3): a playback exception loses this ONE
                    # fragment (skipped), never the rest of the job -- no
                    # control-flow change, the loop continues to the next item.
                    skipped.append(idx)
                finally:
                    try:
                        if os.path.exists(archivo_chunk):
                            os.remove(archivo_chunk)
                    except OSError:
                        pass

        except queue.Empty:
            self._log("⚠️ Timeout esperando chunks de audio.", level="warning")
            outcome_error = "queue_empty_timeout"
            # Design §12: cursor must stay honest on this exit too -- a
            # healthy heavy-TTS server dribbling a chunk past the timeout, or
            # a dead producer, must never report the clean-completion
            # signature (cursor == len(oraciones)).
            cursor = last_idx + 1
        except Exception as e:
            self._log(f"ERROR en reproducción: {e}", level="error")
            _eng.logger.exception("Error en consumidor de audio")
            outcome_error = str(e)
            # Design §12: same honesty requirement as the queue.Empty exit.
            cursor = last_idx + 1
        finally:
            total_elapsed = time.time() - start_tts
            # Join the producer FIRST so it can no longer enqueue a chunk, THEN
            # drain.  Draining before the join races: when the producer is still
            # synthesizing at interrupt time, it writes+enqueues that chunk AFTER
            # get_nowait() already emptied the queue, leaking the temp file
            # permanently (reproducible under loaded-CI interleaving).  After the
            # consumer's dequeue freed a queue slot, the producer always makes
            # progress and breaks on its next _speaking check, so this join
            # returns well within its timeout.
            hilo_productor.join(timeout=2.0)
            # Fix 1: drain any remaining items left in cola_audios by the producer
            # after an early break (pre-dequeue guard, post-dequeue guard, or
            # interrupted-chunk break).  Without this, 1-3 temp .wav files leak
            # per emergency stop because the producer thread may have already
            # enqueued chunks that the consumer never got to consume.
            # Sentinels ("FIN" / None) are skipped — only real chunk tuples have
            # a temp file path at index 0.
            while True:
                try:
                    _leftover = cola_audios.get_nowait()
                    if isinstance(_leftover, tuple):
                        try:
                            if os.path.exists(_leftover[0]):
                                os.remove(_leftover[0])
                        except (OSError, TypeError):
                            pass
                except queue.Empty:
                    break
        self._log(f"✅ Pipeline TTS completado: {chunks_played}/{len(oraciones)} fragmentos en {total_elapsed:.2f}s")
        if error_count > 0:
            self._log(f"⚠️ {error_count} fragmento(s) fallaron.", level="warning")
        with self._lock:
            self._speaking = False
            self._speech_progress = None
        # Step 2 (design §11 B1): `speaking_end`, the speech-source clear and
        # the boundary telemetry all moved to `_speech_boundary_end`, which the
        # JOB owner calls — once per job, after this invocation returns.
        # Step 1 (design §3/§8 step 1): every exit path returns a
        # SpeechOutcome. capture-and-discard only -- `_hablar` propagates this
        # verbatim and every existing caller ignores it (verified by grep),
        # so nothing is stored and nothing resumes yet.
        # Design §12 (defect 2): the producer can run up to ~4 indices ahead
        # of the consumer (queue maxsize=3 + in-hand); a drop at an index the
        # cut never reached must return to PENDING, not double-count as both
        # skipped AND pending. Snapshot-copy + filter at this single return
        # site so every exit path (clean/cut/error) gets it. On clean
        # completion cursor == len(chunks), so every skipped index is
        # already < cursor and this is a no-op.
        skipped_final = sorted(i for i in set(skipped) if i < cursor)
        return _eng.SpeechOutcome(
            chunks=oraciones,
            cursor=cursor,
            spoken=spoken,
            skipped=skipped_final,
            interrupted=was_interrupted,
            error=outcome_error,
        )
