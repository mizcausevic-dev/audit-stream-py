"""
In-memory audit store with hash-chaining + async fanout to SSE listeners.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .models import EventKind, GovernanceEvent, PublishRequest

GENESIS_HASH = "0" * 64


@dataclass
class ChainVerificationResult:
    """Outcome of `AuditStore.verify_chain`."""

    valid: bool
    checked: int
    first_break_at: int | None
    reason: str | None


class AuditStore:
    """
    Thread-safe ish: serialised behind a single asyncio.Lock so the chain
    stays consistent. Each append:

        1. Compute prev_hash from the last event's `hash` (or genesis).
        2. Build the event envelope without `hash`.
        3. Compute `hash` over the canonical JSON of that envelope.
        4. Append + fan out to subscribers.
    """

    def __init__(self) -> None:
        self._events: list[GovernanceEvent] = []
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[GovernanceEvent]] = set()

    # ---- writes ---------------------------------------------------------

    async def append(self, req: PublishRequest) -> GovernanceEvent:
        async with self._lock:
            event_id = len(self._events) + 1
            prev_hash = self._events[-1].hash if self._events else GENESIS_HASH
            timestamp = req.timestamp or _now_iso()

            body_without_hash: dict[str, Any] = {
                "event_id": event_id,
                "timestamp": timestamp,
                "kind": req.kind,
                "source": req.source,
                "payload": req.payload,
                "prev_hash": prev_hash,
            }
            this_hash = _canonical_hash(body_without_hash)
            event = GovernanceEvent(**body_without_hash, hash=this_hash)
            self._events.append(event)

            # Best-effort fanout: a slow subscriber must not block writers.
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop the subscriber's view; better than blocking the chain.
                    self._subscribers.discard(queue)

        return event

    # ---- reads ----------------------------------------------------------

    async def all(self) -> list[GovernanceEvent]:
        async with self._lock:
            return list(self._events)

    async def by_kind(self, kind: EventKind) -> list[GovernanceEvent]:
        async with self._lock:
            return [e for e in self._events if e.kind == kind]

    async def by_source(self, source: str) -> list[GovernanceEvent]:
        async with self._lock:
            return [e for e in self._events if e.source == source]

    async def get(self, event_id: int) -> GovernanceEvent | None:
        async with self._lock:
            if 1 <= event_id <= len(self._events):
                return self._events[event_id - 1]
            return None

    async def latest(self) -> GovernanceEvent | None:
        async with self._lock:
            return self._events[-1] if self._events else None

    async def count(self) -> int:
        async with self._lock:
            return len(self._events)

    # ---- streaming ------------------------------------------------------

    async def subscribe(self, *, max_queue: int = 256) -> AsyncIterator[GovernanceEvent]:
        """Yield events as they arrive. Caller responsible for backpressure."""
        queue: asyncio.Queue[GovernanceEvent] = asyncio.Queue(maxsize=max_queue)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    # ---- verification ---------------------------------------------------

    async def verify_chain(self) -> ChainVerificationResult:
        """
        Re-derive `hash` and check `prev_hash` linkage for every event.
        Returns the first break we find.
        """
        async with self._lock:
            events = list(self._events)

        expected_prev = GENESIS_HASH
        for i, e in enumerate(events, start=1):
            if e.event_id != i:
                return ChainVerificationResult(
                    valid=False,
                    checked=i - 1,
                    first_break_at=i,
                    reason=f"event_id should be {i}, got {e.event_id}",
                )
            if e.prev_hash != expected_prev:
                return ChainVerificationResult(
                    valid=False,
                    checked=i - 1,
                    first_break_at=i,
                    reason=f"prev_hash mismatch at event #{i}",
                )
            body = {
                "event_id": e.event_id,
                "timestamp": e.timestamp,
                "kind": e.kind,
                "source": e.source,
                "payload": e.payload,
                "prev_hash": e.prev_hash,
            }
            recomputed = _canonical_hash(body)
            if recomputed != e.hash:
                return ChainVerificationResult(
                    valid=False,
                    checked=i - 1,
                    first_break_at=i,
                    reason=f"hash mismatch at event #{i}",
                )
            expected_prev = e.hash

        return ChainVerificationResult(valid=True, checked=len(events), first_break_at=None, reason=None)


def _canonical_hash(body: dict[str, Any]) -> str:
    """SHA-256 over canonical JSON (sorted keys, no whitespace)."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
