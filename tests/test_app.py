"""End-to-end tests for the FastAPI app."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audit_stream.app import app

TEST_TOKEN = "synthetic-test-token-0123456789abcdef"
POLICY_TOKEN = "synthetic-policy-token-0123456789abcdef"
REGISTRY_TOKEN = "synthetic-registry-token-0123456789abcdef"
READER_TOKEN = "synthetic-reader-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def _legacy_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_STREAM_AUTH_MODE", "legacy")
    monkeypatch.delenv("AUDIT_STREAM_READER_TOKEN", raising=False)
    monkeypatch.delenv("AUDIT_STREAM_PRODUCER_TOKENS", raising=False)
    monkeypatch.delenv("AUDIT_STREAM_MAX_EVENTS_PER_MINUTE", raising=False)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUDIT_STREAM_TOKEN", TEST_TOKEN)
    monkeypatch.delenv("AUDIT_STREAM_DB_PATH", raising=False)
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        yield c


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "request_allowed",
        "source": "policy-as-code-engine",
        "payload": {"decision_id": "TEST-1"},
    }
    base.update(overrides)
    return base


class TestMeta:
    def test_root(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["name"] == "audit-stream"

    def test_healthz(self, client: TestClient) -> None:
        assert client.get("/healthz").json() == {"status": "ok"}


class TestProducer:
    def test_append_returns_201_and_full_event(self, client: TestClient) -> None:
        r = client.post("/events", json=_event())
        assert r.status_code == 201
        body = r.json()
        assert body["event_id"] == 1
        assert body["prev_hash"] == "0" * 64
        assert len(body["hash"]) == 64

    def test_chain_links_across_two_appends(self, client: TestClient) -> None:
        e1 = client.post("/events", json=_event()).json()
        e2 = client.post("/events", json=_event(kind="request_denied")).json()
        assert e2["event_id"] == 2
        assert e2["prev_hash"] == e1["hash"]

    def test_unknown_kind_rejected(self, client: TestClient) -> None:
        r = client.post("/events", json=_event(kind="not-a-known-kind"))
        # Pydantic Literal raises 422.
        assert r.status_code == 422

    def test_strict_extras_rejected(self, client: TestClient) -> None:
        r = client.post("/events", json={**_event(), "extra_field": True})
        assert r.status_code == 422

    @pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity"])
    def test_nonfinite_payload_rejected_before_append(self, client: TestClient, non_finite: str) -> None:
        content = (
            '{"kind":"request_allowed","source":"synthetic-test","payload":{"value":' + non_finite + "}}"
        )
        response = client.post("/events", content=content, headers={"Content-Type": "application/json"})
        assert response.status_code == 422
        assert client.get("/stats").json()["count"] == 0

    def test_oversized_payload_rejected_before_append(self, client: TestClient) -> None:
        event = _event(payload={"value": "x" * (64 * 1024)})
        response = client.post(
            "/events", content=json.dumps(event), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422
        assert client.get("/stats").json()["count"] == 0

    def test_long_source_and_timestamp_rejected(self, client: TestClient) -> None:
        assert client.post("/events", json=_event(source="x" * 129)).status_code == 422
        assert client.post("/events", json=_event(source="synthetic\nforged")).status_code == 422
        assert client.post("/events", json=_event(source="synthetic\n")).status_code == 422
        assert client.post("/events", json=_event(timestamp="x" * 65)).status_code == 422
        assert client.get("/stats").json()["count"] == 0

    def test_invalid_unicode_rejected_before_append(self, client: TestClient) -> None:
        content = json.dumps(_event(payload={"value": "\ud800"}))
        assert (
            client.post("/events", content=content, headers={"Content-Type": "application/json"}).status_code
            == 422
        )
        content = json.dumps(_event(timestamp="\ud800"))
        assert (
            client.post("/events", content=content, headers={"Content-Type": "application/json"}).status_code
            == 422
        )
        assert client.get("/stats").json()["count"] == 0

    def test_policy_condition_asserted_kind_from_current_producer(self, client: TestClient) -> None:
        r = client.post(
            "/events",
            json=_event(
                kind="policy_condition_asserted",
                payload={"bundle_id": "TEST-1", "condition_id": "synthetic-condition"},
            ),
        )
        assert r.status_code == 201
        assert r.json()["kind"] == "policy_condition_asserted"


class TestAccessBoundary:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/events"),
            ("GET", "/events"),
            ("GET", "/events/1"),
            ("GET", "/stream"),
            ("GET", "/verify"),
            ("GET", "/stats"),
            ("GET", "/checkpoint"),
            ("POST", "/verify/checkpoint"),
        ],
    )
    def test_protected_routes_reject_missing_token(self, client: TestClient, method: str, path: str) -> None:
        response = client.request(
            method, path, headers={"Authorization": ""}, json=_event() if method == "POST" else None
        )
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_wrong_token_rejected(self, client: TestClient) -> None:
        assert client.get("/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401

    def test_wrong_token_cannot_append(self, client: TestClient) -> None:
        response = client.post("/events", json=_event(), headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
        assert client.get("/stats").json()["count"] == 0

    def test_unconfigured_service_fails_closed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AUDIT_STREAM_TOKEN")
        assert client.post("/events", json=_event()).status_code == 503
        assert client.get("/events").status_code == 503
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_legacy_token_requires_explicit_mode_after_upgrade(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AUDIT_STREAM_AUTH_MODE")
        assert client.post("/events", json=_event()).status_code == 503
        assert client.get("/stats").status_code == 503

    @pytest.mark.parametrize("configured", ["short", "has spaces" * 4, "é" * 32])
    def test_weak_or_invalid_config_fails_closed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, configured: str
    ) -> None:
        monkeypatch.setenv("AUDIT_STREAM_TOKEN", configured)
        assert client.get("/stats").status_code == 503

    def test_restart_discards_entire_in_memory_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDIT_STREAM_TOKEN", TEST_TOKEN)
        monkeypatch.delenv("AUDIT_STREAM_DB_PATH", raising=False)
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
        with TestClient(app, headers=headers) as first:
            assert first.post("/events", json=_event()).status_code == 201
            assert first.get("/stats").json()["count"] == 1
        with TestClient(app, headers=headers) as restarted:
            assert restarted.get("/stats").json()["count"] == 0
            assert restarted.get("/verify").json() == {
                "valid": True,
                "checked": 0,
                "first_break_at": None,
                "reason": None,
            }

    def test_sqlite_restart_preserves_chain_and_checkpoint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AUDIT_STREAM_TOKEN", TEST_TOKEN)
        monkeypatch.setenv("AUDIT_STREAM_DB_PATH", str(tmp_path / "events.sqlite3"))
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
        with TestClient(app, headers=headers) as first:
            assert first.post("/events", json=_event()).status_code == 201
            checkpoint = first.get("/checkpoint").json()
            assert checkpoint["event_id"] == 1
        with TestClient(app, headers=headers) as restarted:
            assert restarted.get("/stats").json()["count"] == 1
            assert restarted.get("/verify").json()["valid"] is True
            assert restarted.post("/verify/checkpoint", json=checkpoint).json()["valid"] is True

    def test_truncated_valid_chain_fails_external_checkpoint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "events.sqlite3"
        monkeypatch.setenv("AUDIT_STREAM_TOKEN", TEST_TOKEN)
        monkeypatch.setenv("AUDIT_STREAM_DB_PATH", str(db))
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
        with TestClient(app, headers=headers) as first:
            assert first.post("/events", json=_event()).status_code == 201
            assert first.post("/events", json=_event(kind="request_denied")).status_code == 201
            checkpoint = first.get("/checkpoint").json()

        connection = sqlite3.connect(db)
        try:
            connection.execute("DROP TRIGGER events_no_delete")
            connection.execute("DELETE FROM events WHERE event_id=2")
            connection.commit()
        finally:
            connection.close()

        with TestClient(app, headers=headers) as reopened:
            assert reopened.get("/verify").json()["valid"] is True
            response = reopened.post("/verify/checkpoint", json=checkpoint).json()
            assert response["valid"] is False
            assert response["reason"] == "checkpoint event is missing"


class TestConsumer:
    def _populate(self, client: TestClient) -> None:
        client.post("/events", json=_event(kind="request_allowed"))
        client.post("/events", json=_event(kind="request_denied"))
        client.post("/events", json=_event(kind="watch_drifted", source="aeo-validator-service"))

    def test_query_all(self, client: TestClient) -> None:
        self._populate(client)
        r = client.get("/events")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_query_by_kind(self, client: TestClient) -> None:
        self._populate(client)
        r = client.get("/events", params={"kind": "request_denied"})
        assert len(r.json()) == 1

    def test_query_by_source(self, client: TestClient) -> None:
        self._populate(client)
        r = client.get("/events", params={"source": "aeo-validator-service"})
        assert len(r.json()) == 1

    def test_query_limit(self, client: TestClient) -> None:
        self._populate(client)
        r = client.get("/events", params={"limit": 1})
        body = r.json()
        assert len(body) == 1
        # Limit returns the last N events.
        assert body[0]["event_id"] == 3

    def test_query_invalid_limit_400(self, client: TestClient) -> None:
        assert client.get("/events", params={"limit": 0}).status_code == 400
        assert client.get("/events", params={"limit": 1_000_000}).status_code == 400

    def test_get_specific(self, client: TestClient) -> None:
        self._populate(client)
        r = client.get("/events/2")
        assert r.json()["event_id"] == 2

    def test_get_missing_404(self, client: TestClient) -> None:
        assert client.get("/events/99").status_code == 404


class TestVerifyAndStats:
    def test_intact_chain_verifies(self, client: TestClient) -> None:
        client.post("/events", json=_event())
        client.post("/events", json=_event())
        r = client.get("/verify").json()
        assert r["valid"] is True
        assert r["checked"] == 2

    def test_empty_chain_verifies(self, client: TestClient) -> None:
        r = client.get("/verify").json()
        assert r["valid"] is True
        assert r["checked"] == 0

    def test_stats(self, client: TestClient) -> None:
        client.post("/events", json=_event())
        client.post("/events", json=_event())
        r = client.get("/stats").json()
        assert r["count"] == 2
        assert r["last_event_id"] == 2
        assert len(r["latest_hash"]) == 64

    def test_checkpoint_requires_event_and_detects_wrong_or_missing_anchor(self, client: TestClient) -> None:
        assert client.get("/checkpoint").status_code == 404
        client.post("/events", json=_event())
        checkpoint = client.get("/checkpoint").json()
        assert client.post("/verify/checkpoint", json=checkpoint).json()["valid"] is True
        wrong = {**checkpoint, "hash": "f" * 64}
        result = client.post("/verify/checkpoint", json=wrong).json()
        assert result["valid"] is False
        assert result["reason"] == "checkpoint hash mismatch"
        missing = {**checkpoint, "event_id": 2}
        result = client.post("/verify/checkpoint", json=missing).json()
        assert result["valid"] is False
        assert result["reason"] == "checkpoint event is missing"


class TestScopedBoundary:
    @pytest.fixture
    def scoped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
        monkeypatch.setenv("AUDIT_STREAM_AUTH_MODE", "scoped")
        monkeypatch.delenv("AUDIT_STREAM_TOKEN", raising=False)
        monkeypatch.setenv("AUDIT_STREAM_READER_TOKEN", READER_TOKEN)
        monkeypatch.setenv(
            "AUDIT_STREAM_PRODUCER_TOKENS",
            json.dumps({"policy-as-code-engine": POLICY_TOKEN, "data-contract-registry": REGISTRY_TOKEN}),
        )
        monkeypatch.setenv("AUDIT_STREAM_DB_PATH", str(tmp_path / "scoped.sqlite3"))
        with TestClient(app) as client:
            yield client

    def test_scoped_mode_requires_durable_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDIT_STREAM_AUTH_MODE", "scoped")
        monkeypatch.delenv("AUDIT_STREAM_DB_PATH", raising=False)
        with pytest.raises(RuntimeError, match="requires AUDIT_STREAM_DB_PATH"):
            with TestClient(app):
                pass

    def test_source_bound_write_receipt_and_read_separation(self, scoped: TestClient) -> None:
        producer = {"Authorization": f"Bearer {POLICY_TOKEN}"}
        reader = {"Authorization": f"Bearer {READER_TOKEN}"}
        accepted = scoped.post("/events", json=_event(), headers=producer)
        assert accepted.status_code == 201
        receipt = accepted.json()
        assert receipt["event_id"] == 1
        assert receipt["source"] == "policy-as-code-engine"
        assert len(receipt["hash"]) == 64
        assert scoped.get("/events/1", headers=reader).json() == receipt
        assert scoped.get("/stats", headers=producer).status_code == 401
        assert scoped.get("/stream", headers=producer).status_code == 401
        reader_stream = scoped.get("/stream", headers=reader)
        assert reader_stream.status_code == 501
        assert reader_stream.json()["detail"] == "SSE streaming is unavailable in scoped mode"
        assert scoped.post("/events", json=_event(), headers=reader).status_code == 401
        assert scoped.get("/stats", headers=reader).json()["count"] == 1

    def test_source_spoof_and_timestamp_rejected_without_append(self, scoped: TestClient) -> None:
        producer = {"Authorization": f"Bearer {POLICY_TOKEN}"}
        reader = {"Authorization": f"Bearer {READER_TOKEN}"}
        assert (
            scoped.post("/events", json=_event(source="data-contract-registry"), headers=producer).status_code
            == 403
        )
        assert (
            scoped.post(
                "/events", json=_event(timestamp="2026-01-01T00:00:00Z"), headers=producer
            ).status_code
            == 422
        )
        assert scoped.get("/stats", headers=reader).json()["count"] == 0

    def test_oversized_wire_body_rejected_before_parse(self, scoped: TestClient) -> None:
        producer = {"Authorization": f"Bearer {POLICY_TOKEN}"}
        reader = {"Authorization": f"Bearer {READER_TOKEN}"}
        body = b"x" * (80 * 1024 + 1)
        response = scoped.post(
            "/events", content=body, headers={**producer, "Content-Type": "application/json"}
        )
        assert response.status_code == 413
        chunks = (b"x" * 20_000 for _ in range(5))
        response = scoped.post(
            "/events", content=chunks, headers={**producer, "Content-Type": "application/json"}
        )
        assert response.status_code == 413
        misleading = (b"x" * 20_000 for _ in range(5))
        response = scoped.post(
            "/events",
            content=misleading,
            headers={**producer, "Content-Type": "application/json", "Content-Length": "1"},
        )
        assert response.status_code == 413
        assert scoped.get("/stats", headers=reader).json()["count"] == 0

    def test_unauthorized_oversized_body_is_rejected_before_parse(self, scoped: TestClient) -> None:
        response = scoped.post(
            "/events",
            content=b"x" * (80 * 1024 + 1),
            headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"},
        )
        assert response.status_code == 401

    def test_checkpoint_body_is_bounded_before_parse(self, scoped: TestClient) -> None:
        response = scoped.post(
            "/verify/checkpoint",
            content=b"x" * 1025,
            headers={"Authorization": f"Bearer {READER_TOKEN}", "Content-Type": "application/json"},
        )
        assert response.status_code == 413

    def test_duplicate_authorization_headers_are_rejected(self, scoped: TestClient) -> None:
        response = scoped.post(
            "/events",
            json=_event(),
            headers=[
                ("Authorization", f"Bearer {POLICY_TOKEN}"),
                ("Authorization", f"Bearer {POLICY_TOKEN}"),
            ],
        )
        assert response.status_code == 401

    def test_local_rate_limit_rejects_without_append(
        self, scoped: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUDIT_STREAM_MAX_EVENTS_PER_MINUTE", "2")
        producer = {"Authorization": f"Bearer {POLICY_TOKEN}"}
        reader = {"Authorization": f"Bearer {READER_TOKEN}"}
        assert scoped.post("/events", json=_event(), headers=producer).status_code == 201
        assert scoped.post("/events", json=_event(), headers=producer).status_code == 201
        refused = scoped.post("/events", json=_event(), headers=producer)
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == "60"
        assert scoped.get("/stats", headers=reader).json()["count"] == 2

    @pytest.mark.parametrize(
        "bad_mapping",
        [
            "{}",
            '{"policy-as-code-engine":"short"}',
            json.dumps({"policy-as-code-engine": READER_TOKEN}),
            json.dumps({"policy-as-code-engine": POLICY_TOKEN, "data-contract-registry": POLICY_TOKEN}),
            '{"policy-as-code-engine":"'
            + POLICY_TOKEN
            + '","policy-as-code-engine":"'
            + REGISTRY_TOKEN
            + '"}',
        ],
    )
    def test_invalid_scoped_config_fails_closed(
        self, scoped: TestClient, monkeypatch: pytest.MonkeyPatch, bad_mapping: str
    ) -> None:
        monkeypatch.setenv("AUDIT_STREAM_PRODUCER_TOKENS", bad_mapping)
        assert (
            scoped.post(
                "/events", json=_event(), headers={"Authorization": f"Bearer {POLICY_TOKEN}"}
            ).status_code
            == 503
        )
        assert scoped.get("/stats", headers={"Authorization": f"Bearer {READER_TOKEN}"}).status_code == 503

    def test_mixed_legacy_and_scoped_credentials_fail_closed(
        self, scoped: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUDIT_STREAM_TOKEN", TEST_TOKEN)
        assert (
            scoped.post(
                "/events", json=_event(), headers={"Authorization": f"Bearer {POLICY_TOKEN}"}
            ).status_code
            == 503
        )
        assert scoped.get("/stats", headers={"Authorization": f"Bearer {READER_TOKEN}"}).status_code == 503

    def test_external_checkpoint_copy_and_backup_restore_drill(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        service_dir = tmp_path / "service"
        custody_dir = tmp_path / "synthetic-custody"
        restore_dir = tmp_path / "restore"
        for directory in (service_dir, custody_dir, restore_dir):
            directory.mkdir()
        db_path = service_dir / "events.sqlite3"
        backup_path = restore_dir / "events.sqlite3"
        checkpoint_path = custody_dir / "checkpoint.json"
        monkeypatch.setenv("AUDIT_STREAM_AUTH_MODE", "scoped")
        monkeypatch.delenv("AUDIT_STREAM_TOKEN", raising=False)
        monkeypatch.setenv("AUDIT_STREAM_READER_TOKEN", READER_TOKEN)
        monkeypatch.setenv(
            "AUDIT_STREAM_PRODUCER_TOKENS", json.dumps({"policy-as-code-engine": POLICY_TOKEN})
        )
        monkeypatch.setenv("AUDIT_STREAM_DB_PATH", str(db_path))
        producer = {"Authorization": f"Bearer {POLICY_TOKEN}"}
        reader = {"Authorization": f"Bearer {READER_TOKEN}"}
        with TestClient(app) as live:
            accepted = live.post("/events", json=_event(), headers=producer)
            assert accepted.status_code == 201
            receipt = accepted.json()
            assert live.get("/events/1", headers=reader).json() == receipt
            checkpoint = live.get("/checkpoint", headers=reader).json()
            assert checkpoint == {"event_id": receipt["event_id"], "hash": receipt["hash"]}
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as backup:
                source.backup(backup)

        monkeypatch.setenv("AUDIT_STREAM_DB_PATH", str(backup_path))
        trusted_copy = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        with TestClient(app) as restored:
            assert restored.get("/verify", headers=reader).json()["valid"] is True
            assert (
                restored.post("/verify/checkpoint", json=trusted_copy, headers=reader).json()["valid"] is True
            )
            assert restored.get("/events/1", headers=reader).json() == receipt

        with sqlite3.connect(backup_path) as connection:
            connection.execute("DROP TRIGGER events_no_delete")
            connection.execute("DELETE FROM events")
        with TestClient(app) as truncated:
            assert truncated.get("/verify", headers=reader).json()["valid"] is True
            verdict = truncated.post("/verify/checkpoint", json=trusted_copy, headers=reader).json()
            assert verdict["valid"] is False
            assert verdict["reason"] == "checkpoint event is missing"
