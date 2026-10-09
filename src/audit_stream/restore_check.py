"""Read-only preflight for a restored SQLite candidate and an operator-held head."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import Checkpoint
from .sqlite_store import AuditStoreCorrupt, SqliteAuditStore
from .store import verify_events

MAX_CHECKPOINT_FILE_BYTES = 1024


@dataclass(frozen=True)
class RestoreCheckResult:
    """A local candidate verdict; it cannot establish checkpoint custody."""

    valid: bool
    checked: int
    head_event_id: int | None
    reason: str | None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate checkpoint field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _read_checkpoint(path: Path) -> Checkpoint:
    if not path.is_absolute():
        raise ValueError("checkpoint path must be absolute")
    with path.open("rb") as source:
        raw = source.read(MAX_CHECKPOINT_FILE_BYTES + 1)
    if len(raw) > MAX_CHECKPOINT_FILE_BYTES:
        raise ValueError("checkpoint file exceeds 1 KiB")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return Checkpoint.model_validate(data, strict=True)


def verify_restore_candidate(db_path: Path, checkpoint_path: Path) -> RestoreCheckResult:
    """Verify SQLite integrity and chain, then require the exact supplied head.

    Callers must obtain the checkpoint from an independently controlled source.
    This function cannot authenticate its origin or prove custody.
    """

    try:
        checkpoint = _read_checkpoint(checkpoint_path)
    except (OSError, UnicodeError, ValueError, ValidationError):
        return RestoreCheckResult(False, 0, None, "checkpoint is invalid or unreadable")
    if not db_path.is_absolute():
        return RestoreCheckResult(False, 0, None, "database path must be absolute")
    try:
        physical_path = db_path.resolve(strict=True)
        if db_path.is_symlink() or not physical_path.is_file() or physical_path.stat().st_nlink != 1:
            return RestoreCheckResult(False, 0, None, "candidate must be a standalone regular file")
    except (OSError, RuntimeError):
        return RestoreCheckResult(False, 0, None, "database is missing, unreadable, or invalid")
    if any(
        Path(f"{path}{suffix}").exists() for path in (db_path, physical_path) for suffix in ("-wal", "-shm")
    ):
        return RestoreCheckResult(False, 0, None, "candidate has WAL sidecars; use a standalone backup")

    try:
        with closing(
            sqlite3.connect(physical_path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5.0)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                return RestoreCheckResult(False, 0, None, "SQLite integrity check failed")
            rows = db.execute("SELECT event_id, event_json FROM events ORDER BY event_id")
            chain = verify_events(SqliteAuditStore._decode(row) for row in rows)
            if not chain.valid:
                return RestoreCheckResult(False, chain.checked, None, chain.reason)
            head = db.execute(
                "SELECT event_id, event_json FROM events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
            if head is None:
                return RestoreCheckResult(False, 0, None, "candidate has no events")
            event = SqliteAuditStore._decode(head)
            if event.event_id != checkpoint.event_id or event.hash != checkpoint.hash:
                return RestoreCheckResult(
                    False, chain.checked, event.event_id, "candidate head differs from checkpoint"
                )
            return RestoreCheckResult(True, chain.checked, event.event_id, None)
    except AuditStoreCorrupt as error:
        return RestoreCheckResult(False, 0, None, str(error))
    except sqlite3.Error:
        return RestoreCheckResult(False, 0, None, "database is missing, unreadable, or invalid")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check a restore candidate against an independently held exact chain head."
    )
    parser.add_argument("--db", required=True, type=Path, help="absolute path to the SQLite candidate")
    parser.add_argument(
        "--checkpoint", required=True, type=Path, help="absolute path to a trusted checkpoint JSON file"
    )
    args = parser.parse_args()
    result = verify_restore_candidate(args.db, args.checkpoint)
    print(json.dumps(asdict(result), sort_keys=True))
    raise SystemExit(0 if result.valid else 1)


if __name__ == "__main__":
    main()
