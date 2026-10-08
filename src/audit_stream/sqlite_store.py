"""Opt-in SQLite-backed event chain for local single-worker durability drills."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from .models import EventKind, GovernanceEvent, PublishRequest
from .store import (
    GENESIS_HASH,
    AuditStore,
    ChainVerificationResult,
    _canonical_hash,
    _now_iso,
    verify_events,
)


class AuditStoreCorrupt(RuntimeError):
    """The configured database cannot be trusted as a valid event chain."""


class SqliteAuditStore(AuditStore):
    """Transactional append and verified reopen; one process for SSE fanout."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or not path.parent.is_dir():
            raise ValueError("AUDIT_STREAM_DB_PATH must be an absolute path with an existing parent")
        super().__init__()
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "event_id INTEGER PRIMARY KEY CHECK (event_id > 0), "
                "event_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS events_no_update "
                "BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS events_no_delete "
                "BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise AuditStoreCorrupt("SQLite integrity check failed")
        finally:
            connection.close()
        result = verify_events(self._all_sync())
        if not result.valid:
            raise AuditStoreCorrupt(f"event chain invalid at event #{result.first_break_at}")

    @staticmethod
    def _decode(row: tuple[int, str]) -> GovernanceEvent:
        row_id, event_json = row
        try:
            event = GovernanceEvent.model_validate_json(event_json)
        except Exception:
            raise AuditStoreCorrupt(f"stored event #{row_id} cannot be decoded") from None
        if event.event_id != row_id:
            raise AuditStoreCorrupt(f"stored event id mismatch at row #{row_id}")
        return event

    def _all_sync(self) -> list[GovernanceEvent]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT event_id, event_json FROM events ORDER BY event_id").fetchall()
            return [self._decode(row) for row in rows]
        finally:
            connection.close()

    def _append_sync(self, req: PublishRequest) -> GovernanceEvent:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT event_id, event_json FROM events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
            previous = self._decode(row) if row else None
            event_id = previous.event_id + 1 if previous else 1
            body: dict[str, Any] = {
                "event_id": event_id,
                "timestamp": req.timestamp or _now_iso(),
                "kind": req.kind,
                "source": req.source,
                "payload": req.payload,
                "prev_hash": previous.hash if previous else GENESIS_HASH,
            }
            event = GovernanceEvent(
                event_id=event_id,
                timestamp=body["timestamp"],
                kind=req.kind,
                source=req.source,
                payload=req.payload,
                prev_hash=body["prev_hash"],
                hash=_canonical_hash(body),
            )
            connection.execute(
                "INSERT INTO events (event_id, event_json) VALUES (?, ?)",
                (event_id, event.model_dump_json()),
            )
            connection.commit()
            return event
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def append(self, req: PublishRequest) -> GovernanceEvent:
        async with self._lock:
            event = await asyncio.to_thread(self._append_sync, req)
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    self._subscribers.discard(queue)
            return event

    async def all(self) -> list[GovernanceEvent]:
        return await asyncio.to_thread(self._all_sync)

    async def by_kind(self, kind: EventKind) -> list[GovernanceEvent]:
        return [event for event in await self.all() if event.kind == kind]

    async def by_source(self, source: str) -> list[GovernanceEvent]:
        return [event for event in await self.all() if event.source == source]

    async def get(self, event_id: int) -> GovernanceEvent | None:
        events = await self.all()
        return events[event_id - 1] if 1 <= event_id <= len(events) else None

    async def latest(self) -> GovernanceEvent | None:
        events = await self.all()
        return events[-1] if events else None

    async def count(self) -> int:
        events = await self.all()
        return len(events)

    async def verify_chain(self) -> ChainVerificationResult:
        try:
            return verify_events(await self.all())
        except AuditStoreCorrupt as error:
            return ChainVerificationResult(valid=False, checked=0, first_break_at=None, reason=str(error))
