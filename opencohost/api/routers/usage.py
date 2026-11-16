"""GET /api/usage/summary, GET /api/usage/export, POST /api/usage/purge
(usage-activity-ledger-20261109 T4).

The local usage ledger (``opencohost/core/usage/ledger.py``) holds only counts,
tokens, compute time, fixed enum labels, model names and profile ids. Never chat
text. The data never leaves the machine unless the owner exports it here.

``/api/usage/`` is a sensitive prefix (``api/auth.py``): GETs need the operator
token too, and POST/DELETE are already operator-only.

Errors keep a stable code, never FastAPI's validation list:
422 ``invalid_range`` (bad date, ``from`` after ``to``, range over 1,100 days),
422 ``invalid_format`` (export format), 422 ``confirm_required`` (purge).
"""
import csv
import io
import json
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from opencohost.core.usage import ledger

MAX_RANGE_DAYS = 1100
DEFAULT_RANGE_DAYS = 365
PURGE_CONFIRM = "DELETE_USAGE"

_CSV_COLUMNS = (
    "table", "date", "model", "provider", "kind", "source", "profile_id", "calls",
    "prompt_tokens", "completion_tokens", "compute_ms", "cache_est_reused", "failures",
    "decision_id", "mode", "acted_by", "status", "count", "reused_tokens", "full_reprocess",
)


class _UsageRoute(APIRoute):
    """Maps request validation failures to the stable 422 codes."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def _handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                code = "confirm_required" if request.url.path.endswith("/purge") else "invalid_range"
                return JSONResponse(status_code=422, content={"detail": code})

        return _handler


router = APIRouter(route_class=_UsageRoute)


class UsageDay(BaseModel):
    date: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    compute_ms: int


class _Totals(BaseModel):
    calls: int
    prompt_tokens: int
    completion_tokens: int
    compute_ms: int


class UsageByModel(_Totals):
    model_config = ConfigDict(protected_namespaces=())
    model: str
    provider: str


class UsageByKind(_Totals):
    kind: str


class UsageBySource(_Totals):
    source: str


class UsageByProfile(_Totals):
    profile_id: str


class UsageCache(BaseModel):
    estimated_reused_tokens: int
    real_available: bool
    real_prompt_tokens: Optional[int] = None
    real_reused_tokens: Optional[int] = None


class UsageDecision(BaseModel):
    decision_id: str
    asked: int
    laya_acted: int
    rule_acted: int
    by_status: dict[str, int]


class UsageSummaryResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    enabled: bool
    first_recorded: Optional[str] = None
    days: list[UsageDay]
    by_model: list[UsageByModel]
    by_kind: list[UsageByKind]
    by_source: list[UsageBySource]
    by_profile: list[UsageByProfile]
    cache: UsageCache
    decisions: list[UsageDecision]


class UsagePurgeRequest(BaseModel):
    confirm: Optional[str] = None


class UsagePurgeResponse(BaseModel):
    deleted: bool


def _invalid_range() -> HTTPException:
    return HTTPException(status_code=422, detail="invalid_range")


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise _invalid_range() from None


@router.get("/api/usage/summary", response_model=UsageSummaryResponse, response_model_by_alias=True)
def get_usage_summary(
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
) -> dict:
    today = date.today()
    d_to = _parse_day(date_to) if date_to else today
    d_from = _parse_day(date_from) if date_from else d_to - timedelta(days=DEFAULT_RANGE_DAYS)
    if d_from > d_to or (d_to - d_from).days > MAX_RANGE_DAYS:
        raise _invalid_range()
    payload = ledger.summary(date_from=d_from.isoformat(), date_to=d_to.isoformat())
    payload["from_"] = payload.pop("from")
    return payload


@router.get("/api/usage/export")
def export_usage(format: str = Query("json")) -> Response:
    if format not in ("json", "csv"):
        raise HTTPException(status_code=422, detail="invalid_format")
    rows = ledger.export_rows()
    stamp = date.today().strftime("%Y%m%d")
    headers = {"Content-Disposition": f'attachment; filename="opencohost-usage-{stamp}.{format}"'}
    if format == "json":
        body = json.dumps({"version": 1, "rows": rows}, ensure_ascii=False, indent=2)
        return Response(content=body, media_type="application/json", headers=headers)
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)


@router.post("/api/usage/purge", response_model=UsagePurgeResponse)
def purge_usage(body: Optional[UsagePurgeRequest] = Body(None)) -> Response:
    if body is None or body.confirm != PURGE_CONFIRM:
        return JSONResponse(status_code=422, content={"detail": "confirm_required"})
    if not ledger.purge():
        return JSONResponse(status_code=503, content={"detail": "usage_purge_failed"})
    return JSONResponse(content={"deleted": True})
