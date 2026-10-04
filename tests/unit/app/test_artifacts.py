# SPDX-License-Identifier: AGPL-3.0-only
"""A-series: content-addressed artifact storage and deduplication (PR 8)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from datetime import UTC, datetime

import pytest

from darkula.app.artifacts import (
    ArtifactIntegrityError,
    ArtifactStorageService,
    content_addressed_key,
)
from darkula.app.object_store import (
    ContentHash,
    ContentType,
    ObjectStore,
    StoredObject,
)
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
)
from darkula.domain.identifiers import ObjectKey
from darkula.infrastructure.object_store.in_memory import InMemoryObjectStore


def _utc() -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC)


def _sha(data: bytes) -> ContentHash:
    return ContentHash(algorithm="sha-256", digest_hex=hashlib.sha256(data).hexdigest())


def _payload(index: int) -> bytes:
    return f"content-payload-{index}".encode()


class TestKeyDerivation:
    """A5, A11: deterministic content-addressed keys independent of input."""

    def test_a5_deterministic_key_stable(self) -> None:
        h = _sha(b"hello")
        assert content_addressed_key(h).value == content_addressed_key(h).value

    def test_a5_key_layout(self) -> None:
        h = _sha(b"hello")
        key = content_addressed_key(h).value
        digest = h.digest_hex
        assert key == f"sha256/{digest[:2]}/{digest}"
        # only lowercase safe characters
        assert key.isascii() and key == key.lower()

    def test_a11_unsafe_input_never_affects_key(self) -> None:
        # The key derives only from the representation hash, never a hostile
        # URI/filename/query.
        h = _sha(b"payload")
        assert content_addressed_key(h).value == content_addressed_key(h).value
        key = content_addressed_key(h)
        assert not any(seg in key.value for seg in ("../", "..", "/etc", "https://"))

    def test_a11_missing_hash_algorithm_rejected(self) -> None:
        with pytest.raises(ValueError, match="sha-256"):
            content_addressed_key(ContentHash(algorithm="md5", digest_hex="0" * 32))


class TestStorageDedup:
    """A1-A4, A6, A8-A9, A12."""

    @pytest.fixture
    def store(self) -> InMemoryObjectStore:
        return InMemoryObjectStore()

    @pytest.fixture
    def service(self, store: InMemoryObjectStore) -> ArtifactStorageService:
        return ArtifactStorageService(object_store=store)

    @pytest.mark.asyncio
    async def test_a1_new_hash_single_put(
        self, store: InMemoryObjectStore, service: ArtifactStorageService
    ) -> None:
        data = _payload(1)
        snitch = _CountingStore(store)
        target = ArtifactStorageService(object_store=snitch)
        result = await target.store(
            kind=ArtifactKind.NORMALIZED_TEXT,
            completeness=ArtifactCompleteness.COMPLETE,
            content_type=ContentType("text/plain"),
            content_hash=_sha(data),
            size_bytes=len(data),
            data=data,
            created_at=_utc(),
        )
        assert result.deduplicated is False
        assert snitch.put_count == 1

    @pytest.mark.asyncio
    async def test_a2_existing_compatible_hash_no_duplicate_put(
        self, store: InMemoryObjectStore, service: ArtifactStorageService
    ) -> None:
        data = _payload(2)
        await service.store(
            kind=ArtifactKind.NORMALIZED_TEXT,
            completeness=ArtifactCompleteness.COMPLETE,
            content_type=ContentType("text/plain"),
            content_hash=_sha(data),
            size_bytes=len(data),
            data=data,
            created_at=_utc(),
        )
        snitch = _CountingStore(store)
        target = ArtifactStorageService(object_store=snitch)
        result = await target.store(
            kind=ArtifactKind.NORMALIZED_TEXT,
            completeness=ArtifactCompleteness.COMPLETE,
            content_type=ContentType("text/plain"),
            content_hash=_sha(data),
            size_bytes=len(data),
            data=data,
            created_at=_utc(),
        )
        assert result.deduplicated is True
        assert snitch.put_count == 0

    @pytest.mark.asyncio
    async def test_a3_same_hash_new_provenance_same_key_distinct_artifact(
        self, service: ArtifactStorageService
    ) -> None:
        data = _payload(3)
        r1 = await service.store(
            kind=ArtifactKind.NORMALIZED_TEXT,
            completeness=ArtifactCompleteness.COMPLETE,
            content_type=ContentType("text/plain"),
            content_hash=_sha(data),
            size_bytes=len(data),
            data=data,
            created_at=_utc(),
        )
        r2 = await service.store(
            kind=ArtifactKind.NORMALIZED_TEXT,
            completeness=ArtifactCompleteness.COMPLETE,
            content_type=ContentType("text/plain"),
            content_hash=_sha(data),
            size_bytes=len(data),
            data=data,
            created_at=_utc(),
        )
        # Same physical key; distinct artifact records (independent provenance).
        assert r1.artifact.object_key == r2.artifact.object_key
        assert r1.artifact.artifact_id != r2.artifact.artifact_id

    @pytest.mark.asyncio
    async def test_a4_same_uri_changed_bytes_new_key(
        self, service: ArtifactStorageService
    ) -> None:
        r1 = await service.store(
            kind=ArtifactKind.NORMALIZED_TEXT,
            completeness=ArtifactCompleteness.COMPLETE,
            content_type=ContentType("text/plain"),
            content_hash=_sha(b"page-v1"),
            size_bytes=len(b"page-v1"),
            data=b"page-v1",
            created_at=_utc(),
        )
        r2 = await service.store(
            kind=ArtifactKind.NORMALIZED_TEXT,
            completeness=ArtifactCompleteness.COMPLETE,
            content_type=ContentType("text/plain"),
            content_hash=_sha(b"page-v2"),
            size_bytes=len(b"page-v2"),
            data=b"page-v2",
            created_at=_utc(),
        )
        assert r1.artifact.object_key != r2.artifact.object_key

    @pytest.mark.asyncio
    async def test_a6_existing_key_wrong_size_integrity_failure(
        self, store: InMemoryObjectStore, service: ArtifactStorageService
    ) -> None:
        # Simulate a store whose object at the deterministic key has a
        # different size than the representation being stored (corruption /
        # key collision): the store-kernel mismatch must fail closed.
        expected = b"expected-larger-bytes"
        key = content_addressed_key(_sha(expected))

        async def short_chunks() -> AsyncIterator[bytes]:
            yield b"ab"

        await store.put(key=key, data=short_chunks())
        with pytest.raises(ArtifactIntegrityError, match="size"):
            await service.store(
                kind=ArtifactKind.NORMALIZED_TEXT,
                completeness=ArtifactCompleteness.COMPLETE,
                content_type=ContentType("text/plain"),
                content_hash=_sha(expected),
                size_bytes=len(expected),
                data=expected,
                created_at=_utc(),
            )

    @pytest.mark.asyncio
    async def test_a6_existing_key_corrupt_bytes_integrity_failure(
        self, store: InMemoryObjectStore, service: ArtifactStorageService
    ) -> None:
        data = b"secure-bytes"
        await service.store(
            kind=ArtifactKind.NORMALIZED_TEXT,
            completeness=ArtifactCompleteness.COMPLETE,
            content_type=ContentType("text/plain"),
            content_hash=_sha(data),
            size_bytes=len(data),
            data=data,
            created_at=_utc(),
        )
        # Corrupt the existing object bytes in-place (same size, different bytes).
        key = content_addressed_key(_sha(data))
        pure = InMemoryObjectStore()
        # rebuild via put to simulate a corrupt store

        async def chunks() -> AsyncIterator[bytes]:
            yield b"corrupt!" + b"x" * (len(data) - 8)

        await pure.put(key=key, data=chunks())
        corrupt_service = ArtifactStorageService(object_store=pure)
        with pytest.raises(ArtifactIntegrityError, match="bytes"):
            await corrupt_service.store(
                kind=ArtifactKind.NORMALIZED_TEXT,
                completeness=ArtifactCompleteness.COMPLETE,
                content_type=ContentType("text/plain"),
                content_hash=_sha(data),
                size_bytes=len(data),
                data=data,
                created_at=_utc(),
            )

    @pytest.mark.asyncio
    async def test_a8_store_unavailable_bounded_failure(
        self, service: ArtifactStorageService
    ) -> None:
        class _Unavailable(InMemoryObjectStore):
            pass

        from darkula.app.object_store import (
            ObjectStoreUnavailableError,
        )

        class _Failing(_Unavailable):
            async def stat(self, key: ObjectKey) -> StoredObject | None:
                raise ObjectStoreUnavailableError("unavailable")

        failing = ArtifactStorageService(object_store=_Failing())
        with pytest.raises(ObjectStoreUnavailableError):
            await failing.store(
                kind=ArtifactKind.NORMALIZED_TEXT,
                completeness=ArtifactCompleteness.COMPLETE,
                content_type=ContentType("text/plain"),
                content_hash=_sha(b"x"),
                size_bytes=1,
                data=b"x",
                created_at=_utc(),
            )

    @pytest.mark.asyncio
    async def test_a10_cancellation_during_put_propagates(
        self, service: ArtifactStorageService
    ) -> None:

        class _Hanging(InMemoryObjectStore):
            async def put(
                self,
                *,
                key: ObjectKey,
                data: AsyncIterable[bytes],
                content_type: ContentType | None = None,
                metadata: Mapping[str, str] | None = None,
            ) -> StoredObject:
                raise asyncio.CancelledError()

        hanging = ArtifactStorageService(object_store=_Hanging())
        with pytest.raises(asyncio.CancelledError):
            await hanging.store(
                kind=ArtifactKind.NORMALIZED_TEXT,
                completeness=ArtifactCompleteness.COMPLETE,
                content_type=ContentType("text/plain"),
                content_hash=_sha(b"x"),
                size_bytes=1,
                data=b"x",
                created_at=_utc(),
            )


class _CountingStore(ObjectStore):
    """Wraps an ObjectStore and counts put calls."""

    def __init__(self, inner: ObjectStore) -> None:
        self._inner = inner
        self.put_count = 0

    async def put(
        self,
        *,
        key: ObjectKey,
        data: AsyncIterable[bytes],
        content_type: ContentType | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        self.put_count += 1
        return await self._inner.put(
            key=key, data=data, content_type=content_type, metadata=metadata
        )

    async def get(self, key: ObjectKey) -> AsyncIterable[bytes]:
        return await self._inner.get(key)

    async def stat(self, key: ObjectKey) -> StoredObject | None:
        return await self._inner.stat(key)

    async def delete(self, key: ObjectKey) -> None:
        await self._inner.delete(key)
