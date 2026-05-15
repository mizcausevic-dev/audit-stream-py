"""End-to-end tests for the FastAPI app."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from audit_stream.app import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
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
