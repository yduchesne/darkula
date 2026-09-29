# SPDX-License-Identifier: AGPL-3.0-only
"""InMemoryObjectStore tests (streaming contract matrix)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator

import pytest

from darkula.app.object_store import (
    ContentType,
    ObjectNotFoundError,
)
from darkula.domain.identifiers import ObjectKey
from darkula.infrastructure.object_store import InMemoryObjectStore


async def _chunks(data: bytes, size: int = 3) -> AsyncIterator[bytes]:
    """Yield ``data`` in deterministic bounded chunks."""
    for index in range(0, len(data), size):
        yield data[index : index + size]


async def _collect(source: AsyncIterable[bytes]) -> bytes:
    """Collect an async byte iterable fully."""
    return b"".join([chunk async for chunk in source])


def _key() -> ObjectKey:
    return ObjectKey("tests/artifact.bin")


class TestRoundTrip:
    """Multi-chunk put/get round trips with exact sizes."""

    @pytest.mark.asyncio
    async def test_multi_chunk_round_trip(self) -> None:
        store = InMemoryObjectStore()
        payload = b"0123456789abcdefghij"
        stored = await store.put(key=_key(), data=_chunks(payload, size=4))
        assert stored.key == _key()
        assert stored.size_bytes == len(payload)
        assert await _collect(await store.get(_key())) == payload

    @pytest.mark.asyncio
    async def test_empty_payload_round_trip(self) -> None:
        store = InMemoryObjectStore()
        stored = await store.put(key=_key(), data=_chunks(b""))
        assert stored.size_bytes == 0
        assert await _collect(await store.get(_key())) == b""

    @pytest.mark.asyncio
    async def test_get_returns_async_iterable_never_raw_bytes(self) -> None:
        store = InMemoryObjectStore()
        await store.put(key=_key(), data=_chunks(b"abc"))
        reader = await store.get(_key())
        assert not isinstance(reader, bytes)


class TestStatAndDelete:
    """stat/delete semantics and content metadata."""

    @pytest.mark.asyncio
    async def test_stat_returns_recorded_metadata(self) -> None:
        store = InMemoryObjectStore()
        content_type = ContentType("application/pdf")
        await store.put(
            key=_key(),
            data=_chunks(b"%PDF"),
            content_type=content_type,
            metadata={"source": "test"},
        )
        stat = await store.stat(_key())
        assert stat is not None
        assert stat.size_bytes == 4
        assert stat.content_type == content_type
        assert stat.content_hash is not None
        assert stat.content_hash.algorithm == "sha-256"
        assert len(stat.content_hash.digest_hex) == 64

    @pytest.mark.asyncio
    async def test_content_hash_matches_payload(self) -> None:
        import hashlib

        store = InMemoryObjectStore()
        payload = b"deterministic payload"
        stored = await store.put(key=_key(), data=_chunks(payload))
        assert stored.content_hash is not None
        assert stored.content_hash.digest_hex == hashlib.sha256(payload).hexdigest()

    @pytest.mark.asyncio
    async def test_delete_present_then_missing(self) -> None:
        store = InMemoryObjectStore()
        await store.put(key=_key(), data=_chunks(b"abc"))
        await store.delete(_key())
        assert await store.stat(_key()) is None


class TestMissingSemantics:
    """Frozen missing behavior."""

    @pytest.mark.asyncio
    async def test_stat_missing_returns_none(self) -> None:
        store = InMemoryObjectStore()
        assert await store.stat(_key()) is None

    @pytest.mark.asyncio
    async def test_get_missing_raises_typed_not_found(self) -> None:
        store = InMemoryObjectStore()
        with pytest.raises(ObjectNotFoundError):
            await store.get(_key())

    @pytest.mark.asyncio
    async def test_delete_missing_is_idempotent_success(self) -> None:
        store = InMemoryObjectStore()
        await store.delete(_key())


class TestInvalidChunks:
    """Invalid chunks are rejected loudly."""

    @pytest.mark.asyncio
    async def test_non_bytes_chunk_rejected(self) -> None:
        store = InMemoryObjectStore()

        async def bad_chunks() -> AsyncIterator[object]:
            yield b"ok"
            yield "not-bytes"

        with pytest.raises(ValueError, match="must be bytes"):
            await store.put(key=_key(), data=bad_chunks())  # type: ignore[arg-type]


class TestIsolation:
    """Instance-isolated state."""

    @pytest.mark.asyncio
    async def test_instances_are_isolated(self) -> None:
        first = InMemoryObjectStore()
        second = InMemoryObjectStore()
        await first.put(key=_key(), data=_chunks(b"abc"))
        assert await second.stat(_key()) is None
        assert await first.stat(_key()) is not None

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        store = InMemoryObjectStore()

        async def cancelled_chunks() -> AsyncIterator[bytes]:
            yield b"a"
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await store.put(key=_key(), data=cancelled_chunks())
