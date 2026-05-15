"""Unit tests for the in-memory store + hash chain."""

from __future__ import annotations

import pytest

from audit_stream.models import PublishRequest
from audit_stream.store import GENESIS_HASH, AuditStore


def _req(kind: str = "request_allowed", source: str = "policy-as-code-engine") -> PublishRequest:
    return PublishRequest(kind=kind, source=source, payload={"x": 1})  # type: ignore[arg-type]


class TestAppendChain:
    @pytest.mark.asyncio
    async def test_first_event_links_to_genesis(self) -> None:
        store = AuditStore()
        e = await store.append(_req())
        assert e.event_id == 1
        assert e.prev_hash == GENESIS_HASH
        assert len(e.hash) == 64

    @pytest.mark.asyncio
    async def test_subsequent_events_link_to_previous_hash(self) -> None:
        store = AuditStore()
        e1 = await store.append(_req())
        e2 = await store.append(_req(kind="request_denied"))
        e3 = await store.append(_req(kind="watch_drifted", source="aeo-validator-service"))
        assert e2.prev_hash == e1.hash
        assert e3.prev_hash == e2.hash
        assert e2.event_id == 2
        assert e3.event_id == 3

    @pytest.mark.asyncio
    async def test_count_and_latest(self) -> None:
        store = AuditStore()
        assert await store.count() == 0
        assert await store.latest() is None
        await store.append(_req())
        await store.append(_req(kind="request_denied"))
        assert await store.count() == 2
        latest = await store.latest()
        assert latest is not None
        assert latest.event_id == 2


class TestQueries:
    @pytest.mark.asyncio
    async def test_by_kind_filters(self) -> None:
        store = AuditStore()
        await store.append(_req(kind="request_allowed"))
        await store.append(_req(kind="request_denied"))
        await store.append(_req(kind="request_allowed"))
        allowed = await store.by_kind("request_allowed")
        denied = await store.by_kind("request_denied")
        assert len(allowed) == 2
        assert len(denied) == 1

    @pytest.mark.asyncio
    async def test_by_source_filters(self) -> None:
        store = AuditStore()
        await store.append(_req(source="policy-as-code-engine"))
        await store.append(_req(source="aeo-validator-service"))
        await store.append(_req(source="policy-as-code-engine"))
        ps = await store.by_source("policy-as-code-engine")
        avs = await store.by_source("aeo-validator-service")
        assert len(ps) == 2
        assert len(avs) == 1

    @pytest.mark.asyncio
    async def test_get_by_id_round_trip(self) -> None:
        store = AuditStore()
        e1 = await store.append(_req())
        fetched = await store.get(e1.event_id)
        assert fetched == e1
        assert await store.get(99) is None


class TestChainVerification:
    @pytest.mark.asyncio
    async def test_empty_chain_is_valid(self) -> None:
        result = await AuditStore().verify_chain()
        assert result.valid is True
        assert result.checked == 0

    @pytest.mark.asyncio
    async def test_intact_chain_passes_verification(self) -> None:
        store = AuditStore()
        for _ in range(5):
            await store.append(_req())
        result = await store.verify_chain()
        assert result.valid is True
        assert result.checked == 5

    @pytest.mark.asyncio
    async def test_tampered_payload_breaks_chain(self) -> None:
        store = AuditStore()
        await store.append(_req())
        await store.append(_req())
        await store.append(_req())
        # Mutate the middle event's payload (private access — simulating a
        # tampered backing store).
        store._events[1] = store._events[1].model_copy(update={"payload": {"x": 999}})
        result = await store.verify_chain()
        assert result.valid is False
        assert result.first_break_at == 2

    @pytest.mark.asyncio
    async def test_deleted_event_breaks_chain(self) -> None:
        store = AuditStore()
        for _ in range(4):
            await store.append(_req())
        # Remove the second event.
        del store._events[1]
        result = await store.verify_chain()
        assert result.valid is False
        # Now event #2 is what used to be #3 — its event_id is 3, mismatch at #2.
        assert result.first_break_at == 2


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_subscriber_receives_appended_events(self) -> None:
        store = AuditStore()
        received: list[int] = []

        async def consume() -> None:
            async for ev in store.subscribe():
                received.append(ev.event_id)
                if len(received) >= 3:
                    return

        import asyncio

        task = asyncio.create_task(consume())
        # Give the subscriber a chance to register itself.
        await asyncio.sleep(0)
        for _ in range(3):
            await store.append(_req())
        await asyncio.wait_for(task, timeout=1.0)
        assert received == [1, 2, 3]
