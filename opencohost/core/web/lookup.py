"""One web lookup: search, Laya link filter, fetch, chunk, Laya chunk filter
(web-fetch-laya-20261009).

``run_lookup`` never raises and returns a ``WebLookupResult`` holding the
prompt block (or "") plus counts, timings, the kept source domains and an
outcome: ``ok``, ``timeout``, ``blocked``, ``error`` or ``empty``. A failed
lookup also carries a closed ``reason`` (``http_<status>``, ``anti_bot``,
``parse``, ``dns``, ``blocked``, ``tls`` or ``other``) and ``engine`` names
the search endpoint that gave the final answer (``html`` or ``lite``).

  1. Search the query only (DuckDuckGo html), keep at most 5 results. On an
     error or an empty page, ask the lite endpoint exactly once, through the
     same fetcher and inside the same deadline (web-lookup-polish-20261010
     T3). A timeout or a blocked address never falls back. With both tried,
     the lite outcome is final, but a failure logs the first reason.
  2. A lexical prefilter on the query keeps 3 results (T6), then Laya keeps
     the 2 most useful (fail-open: the first 2).
  3. Fetch both pages in parallel behind the SSRF guard; up to 3 chunks per
     page, in kept-link order.
  4. When no page gave text, the kept results' snippets are the chunks.
  5. The lexical prefilter keeps 3 candidate chunks and Laya keeps the 3
     most useful above its floor (fail-open: all 3, in order). The block
     holds the kept results (title + snippet, unless they already are the
     chunks) and the kept chunks, capped to ``WEB_TEXT_MAX_CHARS``.

``deadline`` (a ``clock()`` instant) bounds every fetch and every Laya call;
the engine adds a hard wall-clock cap on top.
"""
from __future__ import annotations

import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from opencohost.config import settings
from opencohost.core.web import (
    engine_health,
    extract,
    rank,
    search,
    search_api,
    search_api_runtime,
    search_usage,
)
from opencohost.core.web import sources as web_sources
from opencohost.core.web.fetch import WebFetchError
from opencohost.core.web.prompt import build_block

OUTCOME_OK = "ok"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_BLOCKED = "blocked"
OUTCOME_ERROR = "error"
OUTCOME_EMPTY = "empty"
REASON_OTHER = "other"
# A keyed-API failure worth one more try: a slow or flaky call, not a refusal.
# auth, quota, rate_limit, local_cap, parse and blocked are never retried.
_TRANSIENT_API_REASONS = frozenset({search_api.REASON_TIMEOUT, search_api.REASON_OTHER})
_HTTP_5XX = re.compile(r"http_5\d\d")


def _is_transient(reason: str) -> bool:
    return reason in _TRANSIENT_API_REASONS or bool(_HTTP_5XX.fullmatch(reason))

# (engine, url builder, parser), in the order they are asked.
_ENGINES = (
    (search.ENGINE_HTML, search.search_url, search.parse_results),
    (search.ENGINE_LITE, search.lite_search_url, search.parse_lite_results),
)


@dataclass(frozen=True)
class WebLookupResult:
    outcome: str
    block: str = ""
    domains: tuple = ()
    results: int = 0
    kept_links: int = 0
    chunks: int = 0
    kept_chunks: int = 0
    fetch_ms: int = 0
    filter_ms: int = 0
    reason: str = ""
    engine: str = ""
    api_reason: str = ""
    phase: str = ""  # only set on a hard-cap timeout: the stage that stalled
    # (host, https url) of the kept results whose text went into the block, in
    # block order, at most 3 (runtime-fixes-20261110 T5). In memory only.
    sources: tuple = ()


def _outcome(code: str) -> str:
    if code == "timeout":
        return OUTCOME_TIMEOUT
    if code == "blocked":
        return OUTCOME_BLOCKED
    return OUTCOME_ERROR


def _reason(outcome: str, exc: Optional[WebFetchError]) -> str:
    """The closed reason for an error or blocked outcome, else ""."""
    if outcome not in (OUTCOME_ERROR, OUTCOME_BLOCKED):
        return ""
    return exc.reason if exc is not None else REASON_OTHER


def _ms(seconds: float) -> int:
    return max(0, int(seconds * 1000))


def _fetch_chunks(fetcher, url: str, deadline):
    """``(chunks, error)`` for one page; ``error`` is a ``WebFetchError``."""
    try:
        page = fetcher.fetch(url, deadline=deadline)
    except WebFetchError as exc:
        return [], exc
    except Exception:
        return [], WebFetchError("error")
    return [(page.domain, c) for c in extract.html_to_chunks(page.html)], None


def _search_once(engine, build_url, parse, query, fetcher, deadline):
    """``(results, outcome, reason)`` for one endpoint."""
    try:
        page = fetcher.fetch(build_url(query), deadline=deadline)
    except WebFetchError as exc:
        outcome = _outcome(exc.code)
        if outcome == OUTCOME_ERROR and exc.code == "http_status" and exc.status == 202:
            # DuckDuckGo answers its bot challenge with a 202.
            return [], outcome, search.REASON_ANTI_BOT
        return [], outcome, _reason(outcome, exc)
    reason = search.page_reason(page.html, engine)
    if reason == search.REASON_ANTI_BOT:
        return [], OUTCOME_ERROR, reason
    results = parse(page.html)
    if results:
        return results, OUTCOME_OK, ""
    if reason:
        return [], OUTCOME_ERROR, reason
    return [], OUTCOME_EMPTY, ""


def _get_api_key(provider: str) -> str:
    """Resolve Tavily through the shared secure service; Brave remains settings-only."""
    if provider == "tavily":
        from opencohost.security.search_keys import get_tavily_key

        return get_tavily_key()
    if provider == "brave":
        return getattr(settings, "BRAVE_API_KEY", "") or ""
    return ""


def _has_configured_api_keys() -> bool:
    providers = getattr(settings, "SEARCH_FALLBACK", None)
    if providers is None:
        providers = settings._resolve_search_fallback()
    if not providers:
        return False
    return any(bool(_get_api_key(p)) for p in providers)


def _mark(phase: Optional[list], code: str) -> None:
    """Record the stage the lookup reached, so a hard-cap timeout can say
    where it stalled (closed codes only)."""
    if phase is not None:
        phase[:] = [code]


def _release_runtime_slot(runtime, request_id: Optional[str] = None) -> None:
    runtime.release_slot(request_id)


def _search_api(
    query: str,
    providers: Iterable[str],
    deadline: Optional[float],
    clock: Callable[[], float],
    counts: dict,
    *,
    health: engine_health.EngineHealth,
    usage: search_usage.SearchUsageTracker,
    runtime: search_api_runtime.SearchApiRuntime,
) -> tuple[list[search.SearchResult], str, str, str, str]:
    """Execute Phase 4 fallback across configured API providers with transactional ledger and runtime.

    Each attempt is capped at ``SEARCH_API_ATTEMPT_S`` so one slow call cannot
    eat the whole deadline. With exactly one keyed provider that provider gets
    one sequential retry after a transient failure (the runtime has one slot,
    so there is no parallel hedge); every attempt still reserves, dispatches
    and counts one credit, and sends the same query."""
    min_budget = getattr(settings, "SEARCH_API_MIN_BUDGET_S", 1.5)
    attempt_s = float(getattr(settings, "SEARCH_API_ATTEMPT_S", 3.5))
    last_api_reason = ""

    keyed = [p for p in providers if _get_api_key(p)]
    attempts = [(p, False) for p in keyed]
    if len(keyed) == 1:
        attempts.append((keyed[0], True))
    retry_ok = False

    for provider, is_retry in attempts:
        if is_retry and not retry_ok:
            break
        retry_ok = False
        key = _get_api_key(provider)

        req_id = uuid.uuid4().hex

        # 1. Acquire slot from runtime; every path below releases it.
        slot_res = runtime.acquire_slot(deadline, request_id=req_id)
        if not slot_res:
            last_api_reason = getattr(slot_res, "reason", "") or search_usage.REASON_BUSY
            break

        reservation_id: Optional[str] = None
        dispatched_ok = False
        try:
            # 2. Check health breaker for provider
            avail, breaker_reason = health.is_provider_available(provider)
            if not avail:
                last_api_reason = breaker_reason
                continue

            if deadline is not None and (deadline - clock()) < min_budget:
                last_api_reason = search_api.REASON_TIMEOUT
                break

            # 3. Reserve call in transactional ledger
            budget_ms = max(0.0, deadline - clock()) * 1000.0 if deadline is not None else None
            cap = getattr(settings, f"{provider.upper()}_MONTHLY_CAP", None)
            reserve_res = usage.reserve(provider, cap=cap, request_id=req_id, budget_ms=budget_ms)
            if not reserve_res:
                last_api_reason = getattr(reserve_res, "reason", "") or search_usage.REASON_LOCAL_CAP
                continue
            reservation_id = getattr(reserve_res, "request_id", req_id) or req_id

            # 4. PREPARE (no HTTP yet)
            started = clock()
            try:
                prepare_ok, prepare_reason = runtime.prepare(
                    provider, query, key, deadline=deadline, request_id=reservation_id
                )
            finally:
                counts["fetch_ms"] += _ms(clock() - started)
            if not prepare_ok:
                health.record_provider_failure(provider, prepare_reason)
                last_api_reason = prepare_reason
                continue

            # 5. Recheck the deadline BEFORE the durable mark (A5): no quota
            #    is consumed unless a send can still happen.
            if deadline is not None and (deadline - clock()) < min_budget:
                health.record_provider_failure(provider, search_api.REASON_TIMEOUT)
                last_api_reason = search_api.REASON_TIMEOUT
                break

            # 6. A failed durable mark never authorizes SEND.
            if usage.mark_dispatched(reservation_id) is not True:
                last_api_reason = search_usage.REASON_USAGE_STATE
                break
            dispatched_ok = True

            # Only SEND is capped per attempt: PREPARE (worker spawn, DNS) runs
            # under the overall deadline.
            attempt_deadline = deadline
            if attempt_s > 0:
                capped = clock() + attempt_s
                attempt_deadline = capped if deadline is None else min(deadline, capped)
            started = clock()
            try:
                api_results, api_reason = runtime.dispatch(
                    provider, query, key, deadline=attempt_deadline, request_id=reservation_id
                )
            finally:
                counts["fetch_ms"] += _ms(clock() - started)
        finally:
            _release_runtime_slot(runtime, req_id)
            if reservation_id is not None and not dispatched_ok:
                try:
                    usage.release(reservation_id)
                except Exception:
                    pass

        # 7. If results returned: return results, OUTCOME_OK, "", provider, ""
        if api_results:
            return api_results, OUTCOME_OK, "", provider, ""

        # 8. If failure reason returned: record health failure, set last_api_reason
        if api_reason:
            health.record_provider_failure(provider, api_reason)
            last_api_reason = api_reason
            retry_ok = _is_transient(api_reason)
        else:
            last_api_reason = "empty"

    return [], OUTCOME_ERROR, "", "", last_api_reason


def _search(
    query,
    fetcher,
    deadline,
    clock,
    counts,
    *,
    health: Optional[engine_health.EngineHealth] = None,
    usage: Optional[search_usage.SearchUsageTracker] = None,
    runtime: Optional[search_api_runtime.SearchApiRuntime] = None,
    phase: Optional[list] = None,
):
    """``(results, outcome, reason, engine, api_reason)``: html first, lite at most once,
    followed by keyed search API fallback (Tavily, Brave) if DDG fails or is in cooldown."""
    health = health or engine_health.get_health()
    has_keys = _has_configured_api_keys()
    skip_ddg = health.is_ddg_cooled_down(has_api_keys=has_keys)

    answer = ([], OUTCOME_EMPTY, "", "")
    first_reason = ""

    if skip_ddg:
        first_reason = search.REASON_ANTI_BOT
        answer = ([], OUTCOME_ERROR, search.REASON_ANTI_BOT, search.ENGINE_LITE)
    else:
        _mark(phase, "search_ddg")
        for engine, build_url, parse in _ENGINES:
            started = clock()
            try:
                results, outcome, reason = _search_once(engine, build_url, parse, query, fetcher, deadline)
            finally:
                counts["fetch_ms"] += _ms(clock() - started)
            health.record_ddg_attempt(engine, reason)
            first_reason = first_reason or reason
            answer = (results, outcome, reason, engine)
            if outcome not in (OUTCOME_ERROR, OUTCOME_EMPTY):
                break

    results, outcome, reason, engine = answer
    if outcome in (OUTCOME_ERROR, OUTCOME_BLOCKED):
        reason = first_reason or reason

    # DDG succeeded, or blocked by SSRF
    if outcome == OUTCOME_OK and results:
        return results, OUTCOME_OK, "", engine, ""
    if outcome == OUTCOME_BLOCKED:
        return [], OUTCOME_BLOCKED, reason, engine, ""

    # Check timeout deadline remaining budget
    min_budget = getattr(settings, "SEARCH_API_MIN_BUDGET_S", 1.5)
    if outcome == OUTCOME_TIMEOUT:
        if deadline is not None and (deadline - clock()) < min_budget:
            return [], OUTCOME_TIMEOUT, reason, engine, ""

    # API fallback
    providers = getattr(settings, "SEARCH_FALLBACK", None)
    if providers is None:
        providers = settings._resolve_search_fallback()

    if not providers or not has_keys:
        return results, outcome, reason, engine, ""

    usage = usage or search_usage.get_tracker()
    runtime = runtime or search_api_runtime.get_runtime()
    _mark(phase, "search_api")

    api_results, api_outcome, api_err, api_engine, last_api_reason = _search_api(
        query, providers, deadline, clock, counts, health=health, usage=usage, runtime=runtime
    )

    if api_results:
        return api_results, OUTCOME_OK, "", api_engine, ""

    # All API providers failed
    return results, outcome, reason, engine, last_api_reason


def run_lookup(
    question: str,
    query: str,
    *,
    fetcher,
    backend,
    deadline: Optional[float],
    clock: Callable[[], float] = time.monotonic,
    health: Optional[engine_health.EngineHealth] = None,
    usage: Optional[search_usage.SearchUsageTracker] = None,
    runtime: Optional[search_api_runtime.SearchApiRuntime] = None,
    phase: Optional[list] = None,
) -> WebLookupResult:
    counts = {"fetch_ms": 0, "filter_ms": 0}
    try:
        return _run(
            question,
            query,
            fetcher,
            backend,
            deadline,
            clock,
            counts,
            health=health,
            usage=usage,
            runtime=runtime,
            phase=phase,
        )
    except Exception:
        return WebLookupResult(OUTCOME_ERROR, reason=REASON_OTHER, **counts)


def _run(
    question,
    query,
    fetcher,
    backend,
    deadline,
    clock,
    counts,
    *,
    health: Optional[engine_health.EngineHealth] = None,
    usage: Optional[search_usage.SearchUsageTracker] = None,
    runtime: Optional[search_api_runtime.SearchApiRuntime] = None,
    phase: Optional[list] = None,
) -> WebLookupResult:
    results, outcome, reason, engine, api_reason = _search(
        query,
        fetcher,
        deadline,
        clock,
        counts,
        health=health,
        usage=usage,
        runtime=runtime,
        phase=phase,
    )
    if not results:
        return WebLookupResult(outcome, reason=reason, engine=engine, api_reason=api_reason, **counts)
    meta = {"engine": engine, "api_reason": api_reason}

    _mark(phase, "rank_links")
    started = clock()
    candidates = rank.lexical_prefilter(
        query, results, [f"{r.title} {r.snippet}" for r in results], keep=rank.LINK_CANDIDATES
    )
    ranked_links = rank.rank_links(backend, question, candidates, deadline=deadline, clock=clock)
    links = ranked_links.kept
    if not links and candidates:
        links = rank.rescue_links(query, candidates, ranked_links.scores, keep=rank.KEEP_LINKS)
    counts["filter_ms"] += _ms(clock() - started)
    tally = {"results": len(results), "kept_links": len(links)}
    if not links:
        return WebLookupResult(OUTCOME_EMPTY, **tally, **counts, **meta)

    _mark(phase, "fetch_pages")
    started = clock()
    with ThreadPoolExecutor(max_workers=len(links), thread_name_prefix="web-fetch") as pool:
        pages = list(pool.map(lambda r: _fetch_chunks(fetcher, r.url, deadline), links))
    counts["fetch_ms"] += _ms(clock() - started)
    chunks = [c for page_chunks, _ in pages for c in page_chunks]
    errors = [exc for _, exc in pages if exc is not None]
    shown = links  # T5: the kept results' title + snippet go in the block too
    if not chunks:
        chunks = [(r.domain, r.snippet) for r in links if r.snippet]
        shown = ()  # the snippets already are the chunks
    tally["chunks"] = len(chunks)
    if not chunks:
        if not errors:
            return WebLookupResult(OUTCOME_EMPTY, **tally, **counts, **meta)
        outcome = _outcome(errors[0].code)
        return WebLookupResult(outcome, reason=_reason(outcome, errors[0]), **tally, **counts, **meta)

    _mark(phase, "rank_chunks")
    started = clock()
    candidates = rank.lexical_prefilter(query, chunks, [text for _, text in chunks], keep=rank.CHUNK_CANDIDATES)
    kept = rank.rank_chunks(backend, question, candidates, deadline=deadline, clock=clock).kept
    counts["filter_ms"] += _ms(clock() - started)
    tally["kept_chunks"] = len(kept)
    block = build_block(kept, shown)
    if not block:
        return WebLookupResult(OUTCOME_EMPTY, **tally, **counts, **meta)
    domains = tuple(dict.fromkeys([domain for domain, _ in kept] + [r.domain for r in shown]))
    return WebLookupResult(
        OUTCOME_OK, block, domains, sources=_prompt_sources(links, shown, kept), **tally, **counts, **meta
    )


def _prompt_sources(links, shown, kept) -> tuple:
    """``(host, https url)`` of the kept results whose text is in the block, in
    block order (the result lines first, then the page excerpts), one per host,
    at most ``web_sources.MAX_SOURCES``. A chunk whose host matches no kept
    result (a redirect target) has no URL here and is left out."""
    url_by_host: dict = {}
    for result in links:
        url_by_host.setdefault(web_sources.bare_host(result.url), result.url)
    hosts = [r.domain for r in shown if (r.title or r.snippet)] + [d for d, _ in kept]
    urls = [url_by_host.get(web_sources.bare_host(host)) for host in hosts]
    return web_sources.collect(url for url in urls if url)
