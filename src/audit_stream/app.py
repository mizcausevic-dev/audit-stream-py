"""
FastAPI app — six endpoints.

  GET  /                  service info + endpoint list
  GET  /healthz           liveness probe
  POST /events            append one governance event
  GET  /events            query — by kind / source / limit
  GET  /events/{id}       fetch one event
  GET  /stream            live tail via Server-Sent Events
  GET  /verify            verify the hash chain end-to-end
  GET  /stats             { count, last_event_id, latest_hash }
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

from . import __version__
from .models import EventKind, GovernanceEvent, PublishRequest
from .store import AuditStore


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.store = AuditStore()
    try:
        yield
    finally:
        pass


app = FastAPI(
    title="audit-stream",
    version=__version__,
    description=(
        "Append-only governance event stream for the Kinetic Gain portfolio. "
        "Hash-chained for tamper-evidence; SSE for live tailing."
    ),
    lifespan=_lifespan,
)


def _store() -> AuditStore:
    return cast(AuditStore, app.state.store)


@app.get("/", tags=["meta"])
async def root() -> dict[str, Any]:
    return {
        "name": "audit-stream",
        "version": __version__,
        "description": (
            "Append-only governance events for the Kinetic Gain portfolio. Hash-chained, SSE-tailed."
        ),
        "endpoints": {
            "GET  /": "this page",
            "GET  /healthz": "liveness probe",
            "POST /events": "append one event",
            "GET  /events": "query events (kind / source / limit)",
            "GET  /events/{id}": "fetch one event",
            "GET  /stream": "live tail via Server-Sent Events",
            "GET  /verify": "verify the hash chain end-to-end",
            "GET  /stats": "summary stats",
        },
    }


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/events", tags=["producer"], status_code=201)
async def append_event(req: PublishRequest) -> GovernanceEvent:
    return await _store().append(req)


@app.get("/events", tags=["consumer"])
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


@app.get("/events/{event_id}", tags=["consumer"])
async def get_event(event_id: int) -> GovernanceEvent:
    event = await _store().get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"unknown event_id: {event_id}")
    return event


@app.get("/stream", tags=["consumer"])
async def stream_events() -> EventSourceResponse:
    """Live tail of every event after the moment of subscription."""

    async def generator() -> AsyncIterator[dict[str, Any]]:
        async for event in _store().subscribe():
            yield {
                "event": event.kind,
                "id": str(event.event_id),
                "data": json.dumps(event.model_dump(mode="json")),
            }

    return EventSourceResponse(generator())


@app.get("/verify", tags=["consumer"])
async def verify_chain() -> dict[str, Any]:
    result = await _store().verify_chain()
    return {
        "valid": result.valid,
        "checked": result.checked,
        "first_break_at": result.first_break_at,
        "reason": result.reason,
    }


@app.get("/stats", tags=["consumer"])
async def stats() -> dict[str, Any]:
    latest = await _store().latest()
    return {
        "count": await _store().count(),
        "last_event_id": latest.event_id if latest else 0,
        "latest_hash": latest.hash if latest else None,
    }
