"""
FastAPI app — event and local verification endpoints.

  GET  /                  service info + endpoint list
  GET  /healthz           liveness probe
  POST /events            append one governance event
  GET  /events            query — by kind / source / limit
  GET  /events/{id}       fetch one event
  GET  /stream            live tail via Server-Sent Events
  GET  /verify            verify the hash chain end-to-end
  GET  /stats             { count, last_event_id, latest_hash }
  GET  /checkpoint        export current head for external custody
  POST /verify/checkpoint compare against an externally kept head
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from . import __version__
from .boundary import LocalRateLimiter, Principal, RequestBoundary, access_config, authorize
from .models import Checkpoint, EventKind, GovernanceEvent, PublishRequest
from .sqlite_store import SqliteAuditStore
from .store import AuditStore


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    db_path = os.environ.get("AUDIT_STREAM_DB_PATH", "").strip()
    if os.environ.get("AUDIT_STREAM_AUTH_MODE", "").strip() == "scoped" and not db_path:
        raise RuntimeError("scoped audit access requires AUDIT_STREAM_DB_PATH")
    app.state.store = SqliteAuditStore(Path(db_path)) if db_path else AuditStore()
    _limiter.reset()
    try:
        yield
    finally:
        pass


app = FastAPI(
    title="audit-stream",
    version=__version__,
    description="Hash-chained local governance event stream for the Kinetic Gain portfolio.",
    lifespan=_lifespan,
)
_limiter = LocalRateLimiter()
app.add_middleware(RequestBoundary, limiter=_limiter)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, error: RequestValidationError) -> Response:
    # FastAPI's default error body includes rejected input. NaN/Infinity in a
    # payload can make that response itself fail JSON serialization, and audit
    # payloads may contain data that should not be echoed to callers.
    if request.method == "POST" and request.url.path == "/events":
        return JSONResponse(status_code=422, content={"detail": "invalid event"})
    return await request_validation_exception_handler(request, error)


def _store() -> AuditStore:
    return cast(AuditStore, app.state.store)


def _require_reader(request: Request) -> None:
    authorize(request.scope, "reader")


def _require_producer(request: Request) -> Principal:
    principal = getattr(request.state, "audit_principal", None)
    if not isinstance(principal, Principal) or principal.role != "producer":
        raise HTTPException(status_code=401, detail="producer token required")
    return principal


@app.get("/", tags=["meta"])
async def root() -> dict[str, Any]:
    return {
        "name": "audit-stream",
        "version": __version__,
        "description": (
            "Hash-chained local governance events for the Kinetic Gain portfolio. "
            "Scoped mode has no SSE stream."
        ),
        "endpoints": {
            "GET  /": "this page",
            "GET  /healthz": "liveness probe",
            "POST /events": "append one event",
            "GET  /events": "query events (kind / source / limit)",
            "GET  /events/{id}": "fetch one event",
            "GET  /stream": "legacy prototype live tail; unavailable in scoped mode",
            "GET  /verify": "verify the hash chain end-to-end",
            "GET  /stats": "summary stats",
            "GET  /checkpoint": "export current chain head for external anchoring",
            "POST /verify/checkpoint": "compare chain against an external checkpoint",
        },
    }


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/events", tags=["producer"], status_code=201)
async def append_event(
    req: PublishRequest, principal: Annotated[Principal, Depends(_require_producer)]
) -> GovernanceEvent:
    if principal.source is not None and req.source != principal.source:
        raise HTTPException(status_code=403, detail="source is not authorized")
    if principal.source is not None and req.timestamp is not None:
        raise HTTPException(status_code=422, detail="producer timestamp is not accepted")
    return await _store().append(req)


@app.get("/events", tags=["consumer"], dependencies=[Depends(_require_reader)])
async def query_events(
    kind: EventKind | None = None,
    source: str | None = None,
    limit: int = 1000,
) -> list[GovernanceEvent]:
    if limit < 1 or limit > 100_000:
        raise HTTPException(status_code=400, detail="limit must be in 1..100000")
    if kind is not None:
        events = await _store().by_kind(kind)
    elif source is not None:
        events = await _store().by_source(source)
    else:
        events = await _store().all()
    return events[-limit:]


@app.get("/events/{event_id}", tags=["consumer"], dependencies=[Depends(_require_reader)])
async def get_event(event_id: int) -> GovernanceEvent:
    event = await _store().get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"unknown event_id: {event_id}")
    return event


@app.get("/stream", tags=["consumer"], dependencies=[Depends(_require_reader)])
async def stream_events() -> EventSourceResponse:
    """Live tail of every event after the moment of subscription."""

    config = access_config()
    if config is None:
        raise HTTPException(status_code=503, detail="audit access is not configured")
    if config.mode == "scoped":
        raise HTTPException(status_code=501, detail="SSE streaming is unavailable in scoped mode")

    async def generator() -> AsyncIterator[dict[str, Any]]:
        async for event in _store().subscribe():
            yield {
                "event": event.kind,
                "id": str(event.event_id),
                "data": json.dumps(event.model_dump(mode="json")),
            }

    return EventSourceResponse(generator())


@app.get("/verify", tags=["consumer"], dependencies=[Depends(_require_reader)])
async def verify_chain() -> dict[str, Any]:
    result = await _store().verify_chain()
    return {
        "valid": result.valid,
        "checked": result.checked,
        "first_break_at": result.first_break_at,
        "reason": result.reason,
    }


@app.get("/stats", tags=["consumer"], dependencies=[Depends(_require_reader)])
async def stats() -> dict[str, Any]:
    latest = await _store().latest()
    return {
        "count": await _store().count(),
        "last_event_id": latest.event_id if latest else 0,
        "latest_hash": latest.hash if latest else None,
    }


@app.get("/checkpoint", tags=["consumer"], dependencies=[Depends(_require_reader)])
async def export_checkpoint() -> Checkpoint:
    """Return a candidate anchor; the operator must store it independently."""
    latest = await _store().latest()
    if latest is None:
        raise HTTPException(status_code=404, detail="no event to checkpoint")
    return Checkpoint(event_id=latest.event_id, hash=latest.hash)


@app.post("/verify/checkpoint", tags=["consumer"], dependencies=[Depends(_require_reader)])
async def verify_checkpoint(checkpoint: Checkpoint) -> dict[str, Any]:
    """Compare the current chain with an operator-supplied trusted anchor."""
    chain = await _store().verify_chain()
    if not chain.valid:
        return {"valid": False, "checked": chain.checked, "reason": chain.reason}
    event = await _store().get(checkpoint.event_id)
    if event is None:
        return {"valid": False, "checked": chain.checked, "reason": "checkpoint event is missing"}
    if event.hash != checkpoint.hash:
        return {"valid": False, "checked": chain.checked, "reason": "checkpoint hash mismatch"}
    return {"valid": True, "checked": chain.checked, "reason": None}
