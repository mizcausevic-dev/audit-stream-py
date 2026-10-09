"""Offline restore preflight; custody directories here share one test host."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from audit_stream.models import PublishRequest
from audit_stream.restore_check import main, verify_restore_candidate
from audit_stream.sqlite_store import SqliteAuditStore


def _event() -> PublishRequest:
    return PublishRequest(kind="request_allowed", source="synthetic-test", payload={"fixture": True})


def _backup(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(destination)) as backup:
        original.backup(backup)


def _save_checkpoint(path: Path, event_id: int, event_hash: str) -> None:
    path.write_text(json.dumps({"event_id": event_id, "hash": event_hash}), encoding="utf-8")


def _sidecars(path: Path) -> tuple[Path, Path]:
    return Path(f"{path}-wal"), Path(f"{path}-shm")


@pytest.mark.asyncio
async def test_backup_matches_exact_external_copy_without_modifying_candidate(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    candidate = tmp_path / "candidate.sqlite3"
    custody = tmp_path / "custody"
    custody.mkdir()
    checkpoint_path = custody / "head.json"
    store = SqliteAuditStore(source)
    await store.append(_event())
    head = await store.append(_event())
    _save_checkpoint(checkpoint_path, head.event_id, head.hash)
    _backup(source, candidate)
    original_bytes = candidate.read_bytes()
    assert all(not path.exists() for path in _sidecars(candidate))

    result = verify_restore_candidate(candidate, checkpoint_path)
    assert result.valid is True
    assert result.checked == 2
    assert result.head_event_id == 2
    assert result.reason is None
    assert candidate.read_bytes() == original_bytes
    assert all(not path.exists() for path in _sidecars(candidate))


@pytest.mark.asyncio
async def test_valid_tail_truncation_is_rejected_by_external_copy(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite3"
    candidate = tmp_path / "candidate.sqlite3"
    checkpoint_path = tmp_path / "head.json"
    store = SqliteAuditStore(live)
    await store.append(_event())
    head = await store.append(_event())
    _save_checkpoint(checkpoint_path, head.event_id, head.hash)
    with sqlite3.connect(live) as db:
        db.execute("DROP TRIGGER events_no_delete")
        db.execute("DELETE FROM events WHERE event_id=2")
    assert (await SqliteAuditStore(live).verify_chain()).valid is True
    _backup(live, candidate)

    result = verify_restore_candidate(candidate, checkpoint_path)
    assert result.valid is False
    assert result.checked == 1
    assert result.head_event_id == 1
    assert result.reason == "candidate head differs from checkpoint"


@pytest.mark.asyncio
async def test_older_checkpoint_is_not_accepted_as_exact_restore_head(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite3"
    candidate = tmp_path / "candidate.sqlite3"
    checkpoint_path = tmp_path / "head.json"
    store = SqliteAuditStore(live)
    old_head = await store.append(_event())
    _save_checkpoint(checkpoint_path, old_head.event_id, old_head.hash)
    await store.append(_event())
    _backup(live, candidate)

    result = verify_restore_candidate(candidate, checkpoint_path)
    assert result.valid is False
    assert result.checked == 2
    assert result.head_event_id == 2
    assert result.reason == "candidate head differs from checkpoint"


@pytest.mark.asyncio
async def test_corrupt_event_and_unreadable_checkpoint_fail_closed(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite3"
    candidate = tmp_path / "candidate.sqlite3"
    checkpoint_path = tmp_path / "head.json"
    store = SqliteAuditStore(live)
    head = await store.append(_event())
    _save_checkpoint(checkpoint_path, head.event_id, head.hash)
    with sqlite3.connect(live) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET event_json='{}' WHERE event_id=1")
    _backup(live, candidate)
    assert verify_restore_candidate(candidate, checkpoint_path).valid is False

    checkpoint_path.write_text('{"event_id":1,"event_id":1,"hash":"' + head.hash + '"}')
    invalid = verify_restore_candidate(candidate, checkpoint_path)
    assert invalid.valid is False
    assert invalid.reason == "checkpoint is invalid or unreadable"


def test_missing_candidate_is_not_created(tmp_path: Path) -> None:
    candidate = tmp_path / "missing.sqlite3"
    checkpoint_path = tmp_path / "head.json"
    _save_checkpoint(checkpoint_path, 1, "a" * 64)
    result = verify_restore_candidate(candidate, checkpoint_path)
    assert result.valid is False
    assert result.reason == "database is missing, unreadable, or invalid"
    assert not candidate.exists()


@pytest.mark.asyncio
async def test_candidate_with_wal_sidecar_is_rejected_before_open(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite3"
    candidate = tmp_path / "candidate.sqlite3"
    checkpoint_path = tmp_path / "head.json"
    store = SqliteAuditStore(live)
    head = await store.append(_event())
    _save_checkpoint(checkpoint_path, head.event_id, head.hash)
    _backup(live, candidate)
    _sidecars(candidate)[0].write_bytes(b"")

    result = verify_restore_candidate(candidate, checkpoint_path)
    assert result.valid is False
    assert result.reason == "candidate has WAL sidecars; use a standalone backup"


@pytest.mark.asyncio
async def test_hardlink_alias_is_not_accepted_as_standalone_backup(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite3"
    candidate = tmp_path / "candidate.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    checkpoint_path = tmp_path / "head.json"
    store = SqliteAuditStore(live)
    head = await store.append(_event())
    _save_checkpoint(checkpoint_path, head.event_id, head.hash)
    _backup(live, candidate)
    try:
        os.link(candidate, alias)
    except OSError as error:
        pytest.skip(f"hardlinks unavailable on this filesystem: {error}")

    result = verify_restore_candidate(alias, checkpoint_path)
    assert result.valid is False
    assert result.reason == "candidate must be a standalone regular file"


@pytest.mark.asyncio
async def test_symlink_alias_is_rejected_before_immutable_open(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite3"
    candidate = tmp_path / "candidate.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    checkpoint_path = tmp_path / "head.json"
    store = SqliteAuditStore(live)
    head = await store.append(_event())
    _save_checkpoint(checkpoint_path, head.event_id, head.hash)
    _backup(live, candidate)
    try:
        alias.symlink_to(candidate)
    except OSError as error:
        pytest.skip(f"symlinks unavailable on this filesystem: {error}")

    result = verify_restore_candidate(alias, checkpoint_path)
    assert result.valid is False
    assert result.reason == "candidate must be a standalone regular file"


@pytest.mark.asyncio
async def test_parent_symlink_cannot_hide_real_wal_sidecar(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite3"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    candidate = real_dir / "candidate.sqlite3"
    alias_dir = tmp_path / "alias"
    checkpoint_path = tmp_path / "head.json"
    store = SqliteAuditStore(live)
    head = await store.append(_event())
    _save_checkpoint(checkpoint_path, head.event_id, head.hash)
    _backup(live, candidate)
    _sidecars(candidate)[0].write_bytes(b"")
    try:
        alias_dir.symlink_to(real_dir, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable on this filesystem: {error}")

    result = verify_restore_candidate(alias_dir / candidate.name, checkpoint_path)
    assert result.valid is False
    assert result.reason == "candidate has WAL sidecars; use a standalone backup"


@pytest.mark.asyncio
async def test_cli_reports_machine_readable_verdict_and_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live = tmp_path / "live.sqlite3"
    candidate = tmp_path / "candidate.sqlite3"
    checkpoint_path = tmp_path / "head.json"
    store = SqliteAuditStore(live)
    head = await store.append(_event())
    _save_checkpoint(checkpoint_path, head.event_id, head.hash)
    _backup(live, candidate)
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit-stream-verify-restore", "--db", str(candidate), "--checkpoint", str(checkpoint_path)],
    )

    with pytest.raises(SystemExit) as valid:
        main()
    assert valid.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "checked": 1,
        "head_event_id": 1,
        "reason": None,
        "valid": True,
    }

    _save_checkpoint(checkpoint_path, head.event_id, "f" * 64)
    with pytest.raises(SystemExit) as mismatch:
        main()
    assert mismatch.value.code == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
