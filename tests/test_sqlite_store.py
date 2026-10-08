"""Durable-store restart, backup/restore, and append-order drills."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from audit_stream.models import PublishRequest
from audit_stream.sqlite_store import AuditStoreCorrupt, SqliteAuditStore


def _request(kind: str = "request_allowed") -> PublishRequest:
    return PublishRequest.model_validate(
        {"kind": kind, "source": "synthetic-test", "payload": {"fixture": True}}
    )


@pytest.mark.asyncio
async def test_reopen_retains_chain_and_continues_ids(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite3"
    first = SqliteAuditStore(db)
    one = await first.append(_request())
    two = await first.append(_request("request_denied"))

    reopened = SqliteAuditStore(db)
    assert await reopened.count() == 2
    assert (await reopened.verify_chain()).valid is True
    assert (await reopened.get(1)) == one
    three = await reopened.append(_request())
    assert three.event_id == 3
    assert three.prev_hash == two.hash


@pytest.mark.asyncio
async def test_sqlite_backup_restores_exact_chain(tmp_path: Path) -> None:
    source_path = tmp_path / "events.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    original = SqliteAuditStore(source_path)
    await original.append(_request())
    checkpoint = await original.append(_request("request_denied"))

    source = sqlite3.connect(source_path)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
    finally:
        source.close()
        backup.close()

    restored = SqliteAuditStore(backup_path)
    assert (await restored.verify_chain()).valid is True
    assert await restored.count() == 2
    latest = await restored.latest()
    assert latest is not None and latest.hash == checkpoint.hash


@pytest.mark.asyncio
async def test_two_store_instances_serialize_appends(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    first = SqliteAuditStore(path)
    second = SqliteAuditStore(path)
    await asyncio.gather(*(store.append(_request()) for store in [first, second] * 10))
    reopened = SqliteAuditStore(path)
    assert await reopened.count() == 20
    assert (await reopened.verify_chain()).valid is True


@pytest.mark.asyncio
async def test_normal_sql_update_and_delete_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    store = SqliteAuditStore(path)
    await store.append(_request())
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE events SET event_json='{}' WHERE event_id=1")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM events")
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_corrupt_chain_refuses_reopen(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    store = SqliteAuditStore(path)
    await store.append(_request())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute("UPDATE events SET event_json='{}' WHERE event_id=1")
    with pytest.raises(AuditStoreCorrupt, match="cannot be decoded"):
        SqliteAuditStore(path)


def test_db_path_requires_existing_absolute_parent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        SqliteAuditStore(Path("relative.sqlite3"))
    with pytest.raises(ValueError, match="existing parent"):
        SqliteAuditStore(tmp_path / "missing" / "events.sqlite3")
