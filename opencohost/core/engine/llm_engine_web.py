"""Web lookup seam of MotorVocalIA (web-fetch-laya-20261009).

``_run_web_step`` runs once per owner turn, after the vision look and before
prompt assembly, and returns a ``WebLook``: the untrusted WEB RESULTS block,
or a short code-built note when the search failed or found nothing. With
the owner's switch off (the default), for any non-owner source, or when the
turn's ``web`` decision says ``no_search``, it returns ``NO_WEB`` and the
turn is the base turn.

  * Decision: the explicit-request rule (``core/web/trigger.py``), then the
    turn's Laya ``web`` decision (only asked while the switch is on), which
    may add a search but never cancels an explicit request.
  * Subject: the query is the owner's question with the command removed.
    A bare follow-up ("búscalo en internet") searches the previous owner
    turn instead (``subject=followup``); with none, the turn gets the empty
    note and nothing is fetched.
  * Budget: the lookup runs on its own thread and the turn waits at most
    ``settings.WEB_FETCH_BUDGET_S``; a late lookup is abandoned (its own
    deadline and timeouts end it) and the turn gets the failure note.
  * Speech: on a foreground turn with the speech router, a short bridge line
    ("Déjame buscarlo.") goes out on the owner band while the lookup runs; a
    bridge that never started is retired once the lookup is over.
  * Reuse: a lookup is kept for one minute per (source, payload), so the
    foreground turn after an interactive pregen, and the cloud->local retry,
    never search twice.
  * Link (runtime-fixes-20261110 T2): ONE URL typed in the current owner message
    is read only when the turn's origin is ``desktop`` (the Tauri app's own
    route), the web switch is on and ``settings.LINK_READ_ENABLED`` is on.
    Any other origin, including a missing one, never fetches; one
    ``[LINK_READ]`` line (origin, outcome code, bare host, counts) per turn
    that carries a URL.
  * Logs: one ``[WEB]`` line per owner turn while the switch is on, with
    counts, timings, closed codes (``reason=``, ``subject=``, ``engine=``)
    and the kept source domains only; never the user text, the query or a
    URL.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from typing import NamedTuple

from opencohost.config import settings
from opencohost.config import web_fetch as web_fetch_settings
from opencohost.core.decision import runtime as decision_runtime
from opencohost.core.decision.consumers import apply_web_decision
from opencohost.core import turn_origin
from opencohost.core.engine import turn_timing
from opencohost.core.web import freshness, link_read
from opencohost.core.web import rewrite as web_rewrite
from opencohost.core.web import sources as web_sources
from opencohost.core.web import prompt as web_prompt
from opencohost.core.web.fetch import SafeFetcher, default_accept_language
from opencohost.core.web.lookup import OUTCOME_EMPTY, OUTCOME_OK, OUTCOME_TIMEOUT, WebLookupResult, run_lookup
from opencohost.core.web.rank import _STOPWORDS as _RANK_STOPWORDS
from opencohost.core.web import subject as web_subject
from opencohost.core.web.trigger import (
    QUERY_MAX_CHARS, SEARCH, _FILLER, _fold, is_bare_search_command, search_query, web_rule,
)

logger = logging.getLogger("OpenCohost")

WEB_SOURCES = frozenset({"direct", "ptt"})
# Same owner-source prefix as the reply, so emergency stop and the owner
# sweeps reach the bridge too.
WEB_BRIDGE_SUFFIX = "-web-bridge"
WEB_PROGRESS_SUFFIX = "-web-progress"
WEB_BRIDGE_LINES = {"es": "Déjame buscarlo.", "en": "Let me look that up."}
WEB_PROGRESS_INTERVAL_S = 5.0
WEB_PROGRESS_LINES = {
    "es": ("Sigo buscando…", "Ya casi…", "Un momento más…"),
    "en": ("Still searching…", "Almost there…", "One more moment…"),
}
WEB_LOOK_TTL_S = 60.0
WEB_CARRY_TTL_S = 45.0
WEB_CARRY_MAX_CONTENT_WORDS = 3
# Closed values of the [WEB] line's subject= field.
SUBJECT_DIRECT = "direct"
SUBJECT_FOLLOWUP = "followup"
SUBJECT_CARRY = "carry"
SUBJECT_REFINE = "refine"
SUBJECT_REWRITE = "rewrite"
WEB_LEDGER_MAX = 6
WEB_QUERY_REWRITE_CLOUD_TIMEOUT_S = 2.5
WEB_QUERY_REWRITE_LOCAL_TIMEOUT_S = 3.5
WEB_QUERY_REWRITE_MAX_TOKENS = 48
# The lookup's own deadline ends a little before the hard cap, so a normal
# timeout is reported by the lookup itself rather than by the cap.
_DEADLINE_MARGIN_S = 0.3

_monotonic = time.monotonic
_DOMAIN_SAFE = re.compile(r"[^a-z0-9.\-]")
_CODE_SAFE = re.compile(r"[^a-z0-9_]")
_CARRY_STOPWORDS = frozenset(_FILLER | _RANK_STOPWORDS | {"a", "al", "un"})
# K2: a carry needs an anaphoric follow-up: a demonstrative ("eso", "esto") or
# a leading "y" + preposition ("y en México?"). A query with its own subject
# ("qué es LiveAudio") is fresh.
_ANAPHORA_WORDS = frozenset(
    "eso esto ese esa esos esas estos estas aquello ello "
    "it this that those these them".split()
)
_Y_FOLLOWUP = re.compile(
    r"^(?:(?:oye|hey|ok|okay|vale|kira|please|can\s+you|could\s+you)\b[\s,.:;!¡?¿]*)*"
    r"(?:y\s+(?:en|de|del|con|para|sobre)"
    r"|and\s+(?:in|on|at|for|from|of|about)"
    r"|(?:what|how)\s+about(?:\s+(?:in|on|at|for))?)\b"
)
_WEB_MENTION_WORDS = re.compile(
    r"\b(?:web|internet|google|online|browser|websearch|duckduckgo|bing|busqueda|busquedas|buscar)\b|\ben\s+linea\b",
    re.IGNORECASE,
)


_EXPLICIT_ES_BRIDGE_WORDS = re.compile(
    r"\b(?:busca|buscame|buscalo|buscala|buscarlo|investiga|investigalo|noticias|puedes|podrias|por favor|cual|quien|cuando|donde|en la web)\b"
)


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text) if _fold(w) not in _CARRY_STOPWORDS]


def _is_anaphoric(clean: str, query: str) -> bool:
    if any(_fold(w) in _ANAPHORA_WORDS for w in re.findall(r"\w+", query)):
        return True
    return bool(
        _Y_FOLLOWUP.match(_fold(clean).strip(" \t¿¡"))
        or _Y_FOLLOWUP.match(_fold(query).strip(" \t¿¡"))
    )


def _mentions_web(text: str) -> bool:
    return bool(_WEB_MENTION_WORDS.search(_fold(text)))


class WebLook(NamedTuple):
    block: str = ""
    note: str = ""
    searched: bool = False
    link_block: str = ""
    link_note: str = ""
    # (host, https url) of what went into the prompt: kept search results or the
    # read link (runtime-fixes-20261110 T5). Rides the reply, never the prompt.
    sources: tuple = ()

    def kwargs(self) -> dict:
        """Only the non-empty fields, so a base turn passes nothing new."""
        out: dict = {}
        if self.block:
            out["web_block"] = self.block
        elif self.note:
            out["web_note"] = self.note
        if self.link_block:
            out["link_block"] = self.link_block
        elif self.link_note:
            out["link_note"] = self.link_note
        return out


NO_WEB = WebLook()


class _WebProgressTicker:
    """Race-safe progress ticker matching Vision V1.28's _ProgressTicker."""

    def __init__(self, speak_fn, lines: tuple[str, ...], interval_s: float) -> None:
        self._speak = speak_fn
        self._lines = lines
        self._interval_s = max(0.01, float(interval_s))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.count = 0
        self._thread = threading.Thread(target=self._run, name="web-progress", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> int:
        with self._lock:
            self._stop.set()
            return self.count

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            with self._lock:
                if self._stop.is_set():
                    return
                line = self._lines[self.count % len(self._lines)]
                self.count += 1
                try:
                    self._speak(line)
                except Exception:
                    logger.debug("[WEB] progress tick failed", exc_info=False)


class WebTurnMixin:
    """All state lives on the motor."""

    _web_look_cache = None  # ((source, text, query), WebLook, at)
    _web_bridge_job = None
    _web_progress_job = None
    _web_progress_interval_s = WEB_PROGRESS_INTERVAL_S
    _web_fetcher_factory = SafeFetcher
    _last_web_search: tuple[str, float] | None = None  # (query, monotonic_time)
    _owner_web_turns = None  # deque[LedgerTurn]: owner turns, in memory only
    _active_topics = None  # ActiveTopicStack: session LRU topics, no 45s TTL amnesia

    def _active_topics_stack(self) -> web_subject.ActiveTopicStack:
        stack = self._active_topics
        if stack is None:
            stack = self._active_topics = web_subject.ActiveTopicStack(maxlen=5)
        return stack

    def _previous_search(self) -> tuple[str, float] | None:
        if self._last_web_search is not None:
            return self._last_web_search
        cached = self._web_look_cache
        # K2: only a lookup that produced a block may become a carry subject.
        if cached is not None and len(cached[0]) >= 3 and cached[0][2] and cached[1].block:
            return (cached[0][2], cached[2])
        return None

    def _web_session_lang(self) -> str:
        """Resolve active session language ('en' or 'es') from profile locale then active bundle."""
        code = getattr(self, "_current_profile_locale", None)
        if not code:
            try:
                from opencohost.i18n import active as i18n_active

                bundle = i18n_active.get_active_bundle()
                code = bundle.code if bundle else "es"
            except Exception:
                code = "es"
        subtag = str(code or "es").split("-", 1)[0].strip().lower()
        return "en" if subtag == "en" else "es"

    def _web_enabled(self) -> bool:
        try:
            return web_fetch_settings.is_enabled() is True
        except Exception:
            return False

    def _run_web_step(
        self, contexto, source: str, *, decisions=None, speak: bool = False, origin=None
    ) -> WebLook:
        """This owner turn's ``WebLook``. Never raises. ``origin`` is the door the
        turn came through (core/turn_origin.py); only ``desktop`` may read a link."""
        if source not in WEB_SOURCES:
            return NO_WEB
        look = NO_WEB
        if self._web_enabled():
            try:
                look = self._web_step(contexto, source, decisions, speak)
            except Exception as exc:
                logger.info("[WEB] decision=search outcome=error reason=%s", type(exc).__name__)
                look = NO_WEB
        try:
            return self._with_link(look, contexto, source, origin)
        except Exception as exc:
            logger.info("[LINK_READ] origin=- outcome=error host=- chars=0 ms=0 reason=%s", type(exc).__name__)
            return look

    def _with_link(self, look: WebLook, contexto, source, origin) -> WebLook:
        """``look`` plus the link step. Only the CURRENT message is inspected, at
        most one fetch, one attempt. The gate on ``origin`` comes first."""

        text = contexto if isinstance(contexto, str) else str(contexto or "")
        found = link_read.find_link(_strip_wrapper(text))
        if found is None:
            return look
        # Only the typed chat turn qualifies: source "direct" from the desktop door.
        if source != "direct" or not turn_origin.is_desktop(origin):
            known = turn_origin.normalize(origin)
            _link_log(known or "-", "refused_origin", "", 0, 0)
            if known == turn_origin.REMOTE:
                from opencohost.i18n import active as i18n_active

                return look._replace(link_note=i18n_active.link_remote_note())
            return look
        if not self._web_enabled() or not settings.LINK_READ_ENABLED:
            _link_log(turn_origin.DESKTOP, "disabled", "", 0, 0)
            return look
        result = link_read.read_page(
            found.url, self._web_fetcher_factory(), deadline=_monotonic() + link_read.LINK_BUDGET_S
        )
        turn_timing.add("web_ms", result.ms)
        _link_log(turn_origin.DESKTOP, result.outcome, result.host, result.chars, result.ms)
        if result.outcome == link_read.OUTCOME_OK:
            read = web_sources.make_source(found.url)
            return look._replace(
                link_block=link_read.build_block(result.host, result.text, found.extra),
                sources=web_sources.merge((read,) if read else (), look.sources),
            )
        from opencohost.i18n import active as i18n_active

        return look._replace(link_note=i18n_active.link_read_failed_note())

    def _web_step(self, contexto, source, decisions, speak) -> WebLook:
        lang = self._web_session_lang()

        text = contexto if isinstance(contexto, str) else str(contexto or "")
        # A URL is never part of a search query (it would leave the machine).
        clean = link_read.strip_links(_strip_wrapper(text)).strip()
        bare = is_bare_search_command(clean)
        topics = self._active_topics_stack()
        if not bare and apply_web_decision(decisions, web_rule(clean)) != SEARCH:
            _log("skip", WebLookupResult("skipped"))
            self._web_ledger_add(search_query(clean), search_intent=False)
            topics.observe(clean)
            if _mentions_web(clean):
                return WebLook(note=web_prompt.not_searched_note(lang))
            return NO_WEB
        # A bare command names no topic: whatever trigger.search_query leaves
        # ("de nuevo", "otra vez") is junk, so the subject is always resolved.
        question, query, subject = clean, "" if bare else search_query(clean), SUBJECT_DIRECT
        if bare:
            # A bare "búscalo" / "búscalas": subject comes from the owner's recent
            # turns or the session LRU active topic stack.
            subject = SUBJECT_FOLLOWUP
            question = query = self._web_followup_subject() or topics.current()
            if not query:
                rescued = self._maybe_rewrite_web_query(clean, "")
                if rescued:
                    question = query = rescued
                    subject = SUBJECT_REWRITE
                else:
                    _log("search", WebLookupResult(OUTCOME_EMPTY), subject=subject)
                    return WebLook(note=web_prompt.no_subject_note(lang))
            self._web_ledger_add(query, search_intent=True)
        elif not query:
            # A rule-search with nothing but trigger words left.
            subject = SUBJECT_FOLLOWUP
            question = query = self._web_followup_subject() or topics.current()
            if not query:
                rescued = self._maybe_rewrite_web_query(clean, "")
                if rescued:
                    question = query = rescued
                    subject = SUBJECT_REWRITE
                else:
                    _log("search", WebLookupResult(OUTCOME_EMPTY), subject=subject)
                    return WebLook(note=web_prompt.no_subject_note(lang))
            self._web_ledger_add(query, search_intent=True)
        elif web_subject.is_clarification(query) and topics.current():
            # "al lenguaje vuelvelo a buscar ahora": refines the active top topic
            # (e.g. "rust" -> "rust lenguaje") instead of searching bare "al lenguaje".
            topics.observe(query)
            subject = SUBJECT_FOLLOWUP
            question = query = topics.current()
            self._web_ledger_add(query, search_intent=True)
        elif web_subject.is_refinement(query) and (base := self._web_followup_subject(strict=True)):
            # "opciones más recientes este año": narrows the previous request.
            self._web_ledger_add(query, search_intent=True)
            subject = SUBJECT_REFINE
            question = query = web_subject.merge_refinement(base, query)
        elif web_subject.needs_topic_carry(query):
            # "busca en la web de que trata" / "que juegos usan ese lenguaje":
            # attach the recent owner topic deterministically (0 ms fallback
            # even if the cloud query rewriter times out or the 45s TTL expired).
            base = self._web_followup_subject() or topics.current()
            if base:
                subject = SUBJECT_FOLLOWUP
                question = query = web_subject.merge_followup_query(base, query)
                self._web_ledger_add(query, search_intent=True)
            elif web_subject.is_subjectless_followup(query):
                rescued = self._maybe_rewrite_web_query(clean, "")
                if rescued:
                    question = query = rescued
                    subject = SUBJECT_REWRITE
                    self._web_ledger_add(query, search_intent=True)
                else:
                    _log("search", WebLookupResult(OUTCOME_EMPTY), subject=SUBJECT_FOLLOWUP)
                    return WebLook(note=web_prompt.no_subject_note(lang))
            else:
                self._web_ledger_add(query, search_intent=True)
        else:
            self._web_ledger_add(query, search_intent=True)
            topics.observe(query)
            prev = self._previous_search()
            if prev is not None:
                prev_q, prev_at = prev
                if _monotonic() - prev_at <= WEB_CARRY_TTL_S:
                    cwords = _content_words(query)
                    anaphoric = _is_anaphoric(clean, query)
                    short = len(cwords) <= WEB_CARRY_MAX_CONTENT_WORDS  # 0 is fine: "mas sobre eso"
                    logger.info(
                        "[WEB] carry decision=%s qwords=%d pwords=%d",
                        "carry" if anaphoric and short else "fresh",
                        len(cwords), len(_content_words(prev_q)),
                    )
                    if anaphoric and short:
                        seen = {w.lower() for w in re.findall(r"\w+", query)}
                        prev_cwords = []
                        for w in _content_words(prev_q):
                            wl = w.lower()
                            if wl not in seen:
                                seen.add(wl)
                                prev_cwords.append(w)
                        if prev_cwords:
                            query = f"{query} {' '.join(prev_cwords)}"[:QUERY_MAX_CHARS].strip()
                            subject = SUBJECT_CARRY
        cached = self._web_look_cache
        if cached is not None and cached[0][:2] == (source, text) and _monotonic() - cached[2] <= WEB_LOOK_TTL_S:
            return cached[1]
        ticker = None
        if speak:
            self._web_speak_bridge(clean, source)
            ticker = self._start_web_progress(clean, source)
        try:
            if subject != SUBJECT_REWRITE:
                rewritten = self._maybe_rewrite_web_query(clean, query)
                if rewritten:
                    query = rewritten
                    question = rewritten
                    subject = SUBJECT_REWRITE
                    self._web_ledger_add(query, search_intent=True)
            key = (source, text, query)
            # Freshness: a time-sensitive query carries the year in its text.
            result = self._web_lookup(question, freshness.with_year(query))
        finally:
            if ticker is not None:
                ticker.stop()
            retire = getattr(self, "_retire_vision_job", None)
            bridge_job, self._web_bridge_job = self._web_bridge_job, None
            prog_job, self._web_progress_job = self._web_progress_job, None
            if callable(retire):
                if bridge_job is not None:
                    retire(bridge_job)
                if prog_job is not None:
                    retire(prog_job)
        _log("search", result, subject=subject)
        turn_timing.add("web_ms", result.fetch_ms + result.filter_ms)
        if result.outcome == OUTCOME_OK and result.block:
            look = WebLook(block=result.block, searched=True, sources=tuple(getattr(result, "sources", ()) or ()))
        else:
            # Every failure outcome carries a note (T4); an unknown one fails closed.
            look = WebLook(note=web_prompt.note_for(result.outcome, result.reason, lang=lang) or web_prompt.WEB_FAILED_NOTE, searched=True)
        now = _monotonic()
        if look.block:  # K2: an empty/failed lookup is never a carry subject
            self._last_web_search = (query, now)
        self._web_look_cache = (key, look, now)
        return look

    def _web_ledger_add(self, query: str, *, search_intent: bool) -> None:
        """Remember this owner turn's subject, in memory only (never logged,
        never persisted). A repeat of the newest entry (pregen then the
        foreground turn) is not stored twice."""
        ledger = self._owner_web_turns
        if ledger is None:
            ledger = self._owner_web_turns = deque(maxlen=WEB_LEDGER_MAX)
        now = _monotonic()
        refinement = search_intent and web_subject.is_refinement(query)
        if ledger:
            last = ledger[-1]
            if (last.query, last.search_intent, last.refinement) == (query, search_intent, refinement)                     and now - last.ts <= WEB_LOOK_TTL_S:
                return
        ledger.append(web_subject.LedgerTurn(now, query, search_intent, refinement))

    def _web_followup_subject(self, *, strict: bool = False) -> str:
        """The query a bare command or refinement builds on, from the in-memory
        owner ledger only (a search chain lives ``settings.WEB_SUBJECT_TTL_S``,
        a plain owner turn 45 s). ``strict`` (refinements) accepts a search
        chain only. History is never read: it has no ages."""
        ledger = self._owner_web_turns
        if not ledger:
            return ""
        return web_subject.resolve_followup_subject(
            list(ledger), now=_monotonic(), ttl=float(settings.WEB_SUBJECT_TTL_S), year=None, strict=strict
        )

    def _web_rewrite_enabled(self) -> bool:
        raw = os.environ.get("OPENCOHOST_WEB_QUERY_REWRITE", "1").strip().lower()
        return raw not in ("0", "false", "off", "no")

    def _snapshot_recent_host_history(self) -> list[tuple[str, str]]:
        history = getattr(self, "historial", None)
        if not history:
            return []
        lock = getattr(self, "_history_lock", None)
        try:
            if lock is not None:
                with lock:
                    snap = list(history)
            else:
                snap = list(history)
        except Exception:
            return []
        return web_rewrite.recent_host_context(snap)

    def _invoke_web_rewrite_llm(self, messages: list[dict], *, timeout: float) -> str:
        """Run one short non-streaming rewrite call on the active LLM (cloud or resident local)."""
        is_local = getattr(self, "_is_local", True)
        started = _monotonic()
        response = None
        model_for_usage = None
        if not is_local:
            cfg = getattr(self, "_provider_config", None)
            if not cfg:
                return ""
            from opencohost.core.engine.llm_inference_service import get_inference_service

            response = get_inference_service(self).cloud_chat(
                provider_cfg=cfg,
                messages=messages,
                options={"num_predict": WEB_QUERY_REWRITE_MAX_TOKENS, "temperature": 0.0},
                timeout=timeout,
            )
            model_for_usage = getattr(self, "current_model", None)
        else:
            loaded = getattr(self, "_loaded_model", None)
            if not loaded:
                return ""
            chat_fn = getattr(self, "_ollama_scout_chat", None)
            if not callable(chat_fn):
                ollama_client = getattr(self, "ollama", None)
                chat_fn = getattr(ollama_client, "chat", None) if ollama_client is not None else None
            if not callable(chat_fn):
                return ""
            options: dict = {"num_predict": WEB_QUERY_REWRITE_MAX_TOKENS, "temperature": 0.0}
            with_ctx = getattr(self, "_with_num_ctx", None)
            resolve_ctx = getattr(self, "_resolve_local_num_ctx", None)
            if callable(with_ctx) and callable(resolve_ctx):
                options = with_ctx(options, resolve_ctx(loaded))
            response = chat_fn(
                model=loaded,
                messages=messages,
                options=options,
                keep_alive=settings.LLM_KEEP_ALIVE,
                think=False,
            )
            model_for_usage = loaded
        usage_record = getattr(self, "_usage_record", None)
        if response is not None and callable(usage_record):
            usage_record(
                response,
                kind="web_query_rewrite",
                model=model_for_usage,
                is_local=bool(is_local),
                started=started,
            )
        if isinstance(response, dict):
            msg = response.get("message")
            return str(msg.get("content") or "") if isinstance(msg, dict) else ""
        msg = getattr(response, "message", None)
        return str(getattr(msg, "content", "") or "") if msg is not None else ""

    def _maybe_rewrite_web_query(self, clean: str, fallback_query: str) -> str:
        """Contextually rewrite *clean* using recent host history. Never raises; returns '' to keep fallback."""
        if not self._web_rewrite_enabled():
            return ""
        recent = self._snapshot_recent_host_history()
        if not recent:
            return ""
        messages = web_rewrite.build_rewrite_messages(clean, recent)
        t0 = _monotonic()
        is_local = getattr(self, "_is_local", True)
        transport = "local" if is_local else "cloud"
        timeout = WEB_QUERY_REWRITE_LOCAL_TIMEOUT_S if is_local else WEB_QUERY_REWRITE_CLOUD_TIMEOUT_S
        try:
            raw = self._invoke_web_rewrite_llm(messages, timeout=timeout)
        except TimeoutError:
            logger.info("[WEB_REWRITE] outcome=timeout ms=%d transport=%s", int((_monotonic() - t0) * 1000), transport)
            return ""
        except Exception as exc:
            logger.info(
                "[WEB_REWRITE] outcome=error reason=%s ms=%d transport=%s",
                _closed(type(exc).__name__), int((_monotonic() - t0) * 1000), transport,
            )
            return ""
        ms = int((_monotonic() - t0) * 1000)
        if not raw or not raw.strip():
            logger.info("[WEB_REWRITE] outcome=empty ms=%d transport=%s", ms, transport)
            return ""
        rewritten = web_rewrite.parse_rewritten_query(raw, fallback=fallback_query)
        if not rewritten or rewritten == fallback_query:
            logger.info("[WEB_REWRITE] outcome=fallback ms=%d transport=%s", ms, transport)
            return ""
        turn_timing.add("web_ms", ms)
        logger.info("[WEB_REWRITE] outcome=ok ms=%d transport=%s", ms, transport)
        return rewritten

    def _web_lookup(self, question: str, query: str) -> WebLookupResult:
        """Run the lookup on its own thread, bounded by the hard budget."""
        budget = max(0.1, float(settings.WEB_FETCH_BUDGET_S))
        deadline = _monotonic() + max(0.05, budget - _DEADLINE_MARGIN_S)
        backend = getattr(decision_runtime.get_bus(), "backend", None)
        fetcher = (
            SafeFetcher(accept_language=default_accept_language(self._web_session_lang()))
            if self._web_fetcher_factory is SafeFetcher
            else self._web_fetcher_factory()
        )
        box: list = []
        phase: list = []

        def _work():
            box.append(run_lookup(question, query, fetcher=fetcher, backend=backend, deadline=deadline, phase=phase))

        worker = threading.Thread(target=_work, name="web-lookup", daemon=True)
        worker.start()
        worker.join(budget)
        # Usage ledger: a real execution counts (a TTL cache hit returns earlier
        # and never reaches here). Calls only: no query, no tokens.
        try:
            from opencohost.core.usage import ledger

            ledger.record_web_search(profile_id=getattr(self, "_current_profile_id", None) or "")
        except Exception:
            logger.debug("usage ledger web hook failed", exc_info=True)
        if not box:
            return WebLookupResult(OUTCOME_TIMEOUT, fetch_ms=int(budget * 1000), phase=phase[0] if phase else "")
        return box[0]

    def _resolve_web_speech_lang(self, clean: str) -> str:
        from opencohost.core.vision.turn_step import detect_language

        session_lang = self._web_session_lang()
        if (
            session_lang == "en"
            and not _EXPLICIT_ES_BRIDGE_WORDS.search(_fold(clean))
            and not re.search(r"[¿¡ñáéíóú]", clean, re.IGNORECASE)
        ):
            return "en"
        return detect_language(clean, default=session_lang)

    def _web_speak_bridge(self, clean: str, source: str) -> None:
        if not getattr(self, "_speech_router_enabled", False):
            return
        try:
            from opencohost.core.speech.router import PRIORITY_OWNER

            bridge_lang = self._resolve_web_speech_lang(clean)
            line = WEB_BRIDGE_LINES.get(bridge_lang, WEB_BRIDGE_LINES[self._web_session_lang()])
            self._web_bridge_job = self._ensure_router().submit(
                line, f"{source}{WEB_BRIDGE_SUFFIX}", PRIORITY_OWNER,
                releases_arrival_hold=False,
            )
        except Exception:
            logger.debug("[WEB] bridge submit failed", exc_info=False)

    def _web_speak_progress(self, line: str, source: str) -> None:
        """Speak one progress line while a web lookup runs long (never piles up)."""
        if not getattr(self, "_speech_router_enabled", False):
            return
        try:
            from opencohost.core.speech.router import PRIORITY_OWNER, SpeechJobState

            busy = (SpeechJobState.QUEUED, SpeechJobState.ACTIVE, SpeechJobState.SUSPENDED)
            for job in (self._web_bridge_job, self._web_progress_job):
                if job is not None and getattr(job, "state", None) in busy:
                    return
            self._web_progress_job = self._ensure_router().submit(
                line, f"{source}{WEB_PROGRESS_SUFFIX}", PRIORITY_OWNER,
                releases_arrival_hold=False,
            )
        except Exception:
            logger.debug("[WEB] progress line submit failed", exc_info=False)

    def _start_web_progress(self, clean: str, source: str):
        if not getattr(self, "_speech_router_enabled", False):
            return None
        try:
            lang = self._resolve_web_speech_lang(clean)
            lines = WEB_PROGRESS_LINES.get(lang, WEB_PROGRESS_LINES["es"])
            interval = getattr(self, "_web_progress_interval_s", WEB_PROGRESS_INTERVAL_S)
            ticker = _WebProgressTicker(
                lambda line: self._web_speak_progress(line, source),
                lines,
                interval,
            )
            ticker.start()
            return ticker
        except Exception:
            logger.debug("[WEB] progress ticker failed to start", exc_info=False)
            return None


def _strip_wrapper(text: str) -> str:
    try:
        from opencohost.core.memory.memoria_store import strip_history_wrapper

        return strip_history_wrapper(text) or text
    except Exception:
        return text


def _closed(value) -> str:
    """A closed log code (letters, digits, underscore) or "-"."""
    return _CODE_SAFE.sub("", str(value or "").lower())[:24] or "-"


def _link_log(origin: str, outcome: str, host: str, chars: int, ms: int) -> None:
    """One content-free line: closed codes, the bare host and counts. Never a URL."""
    logger.info(
        "[LINK_READ] origin=%s outcome=%s host=%s chars=%d ms=%d",
        _closed(origin), _closed(outcome), _DOMAIN_SAFE.sub("", host or "")[:80] or "-", chars, ms,
    )


def _log(decision: str, result: WebLookupResult, *, subject: str = "-") -> None:
    api_part = f" api_reason={_closed(result.api_reason)}" if getattr(result, "api_reason", "") else ""
    if getattr(result, "phase", ""):
        api_part += f" phase={_closed(result.phase)}"
    logger.info(
        "[WEB] decision=%s results=%d kept_links=%d chunks=%d kept_chunks=%d fetch_ms=%d filter_ms=%d "
        "outcome=%s reason=%s subject=%s engine=%s domains=%s%s",
        decision, result.results, result.kept_links, result.chunks, result.kept_chunks,
        result.fetch_ms, result.filter_ms, result.outcome,
        _closed(result.reason), subject, _closed(result.engine),
        ",".join(_DOMAIN_SAFE.sub("", d) for d in result.domains) or "-",
        api_part,
    )
