# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the streaming ObjectStore contract (OBJ-*)."""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncIterable, AsyncIterator, Mapping

import pytest

from darkula.app.object_store import (
    ContentHash,
    ContentType,
    ObjectNotFoundError,
    ObjectStore,
    StoredObject,
)
from darkula.domain.identifiers import ObjectKey


async def _chunks(data: bytes, size: int = 4) -> AsyncIterator[bytes]:
    for index in range(0, len(data), size):
        yield data[index : index + size]


class _MemoryObjectStore(ObjectStore):
    """Test-local in-memory ObjectStore stub (PR 3 owns the production fakes)."""

    def __init__(self) -> None:
        self._objects: dict[ObjectKey, tuple[bytes, StoredObject]] = {}
        self.unavailable_for: set[ObjectKey] = set()

    async def put(
        self,
        *,
        key: ObjectKey,
        data: AsyncIterable[bytes],
        content_type: ContentType | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        if key in self.unavailable_for:
            from darkula.app.object_store import ObjectStoreUnavailableError

            raise ObjectStoreUnavailableError("store unavailable")
        buffer = bytearray()
        async for chunk in data:
            buffer.extend(chunk)
        payload = bytes(buffer)
        stored = StoredObject(
            key=key,
            size_bytes=len(payload),
            content_type=content_type,
            content_hash=ContentHash(algorithm="sha-256", digest_hex="0" * 64),
            metadata=metadata or {},
        )
        self._objects[key] = (payload, stored)
        return stored

    async def get(self, key: ObjectKey) -> AsyncIterable[bytes]:
        if key not in self._objects:
            raise ObjectNotFoundError(f"object not found: {key}")
        payload, _ = self._objects[key]

        async def read() -> AsyncIterator[bytes]:
            for index in range(0, len(payload), 4):
                yield payload[index : index + 4]

        return read()

    async def stat(self, key: ObjectKey) -> StoredObject | None:
        if key not in self._objects:
            return None
        return self._objects[key][1]

    async def delete(self, key: ObjectKey) -> None:
        self._objects.pop(key, None)


def _key() -> ObjectKey:
    return ObjectKey("tests/artifact.bin")


class TestPutGet:
    """OBJ-01/OBJ-02: streaming bytes in both directions."""

    @pytest.mark.asyncio
    async def test_put_accepts_async_byte_chunks(self) -> None:
        store = _MemoryObjectStore()
        payload = b"hello darkula artifact payload"
        stored = await store.put(key=_key(), data=_chunks(payload))
        assert isinstance(stored, StoredObject)
        assert stored.key == _key()
        assert stored.size_bytes == len(payload)

    @pytest.mark.asyncio
    async def test_get_yields_async_byte_chunks(self) -> None:
        store = _MemoryObjectStore()
        payload = b"0123456789abcdef"
        await store.put(key=_key(), data=_chunks(payload, size=2))
        reader = await store.get(_key())
        collected = b"".join([chunk async for chunk in reader])
        assert collected == payload

    @pytest.mark.asyncio
    async def test_put_records_content_type_and_metadata(self) -> None:
        store = _MemoryObjectStore()
        stored = await store.put(
            key=_key(),
            data=_chunks(b"x" * 10),
            content_type=ContentType("application/octet-stream"),
            metadata={"source": "test"},
        )
        assert stored.content_type == ContentType("application/octet-stream")
        assert stored.metadata["source"] == "test"
        assert stored.content_hash is not None
        assert stored.content_hash.algorithm == "sha-256"


class TestMissingObjectSemantics:
    """OBJ-03/OBJ-04/OBJ-05: frozen missing-object behavior."""

    @pytest.mark.asyncio
    async def test_stat_missing_returns_none(self) -> None:
        store = _MemoryObjectStore()
        assert await store.stat(_key()) is None

    @pytest.mark.asyncio
    async def test_get_missing_raises_typed_not_found(self) -> None:
        store = _MemoryObjectStore()
        with pytest.raises(ObjectNotFoundError):
            await store.get(_key())

    @pytest.mark.asyncio
    async def test_delete_missing_is_idempotent_success(self) -> None:
        store = _MemoryObjectStore()
        await store.delete(_key())  # must not raise

    @pytest.mark.asyncio
    async def test_delete_present_then_missing(self) -> None:
        store = _MemoryObjectStore()
        await store.put(key=_key(), data=_chunks(b"abc"))
        await store.delete(_key())
        assert await store.stat(_key()) is None


class TestCancellation:
    """OBJ-08: cancellation propagates through streaming boundaries."""

    @pytest.mark.asyncio
    async def test_cancel_during_upload_propagates(self) -> None:
        store = _MemoryObjectStore()

        async def cancelled_chunks() -> AsyncIterator[bytes]:
            yield b"a"
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await store.put(key=_key(), data=cancelled_chunks())


class TestValidation:
    """OBJ-06: invalid blank keys are rejected at the value boundary."""

    def test_blank_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            ObjectKey("  ")

    def test_invalid_media_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="media type"):
            ContentType("not-a-media-type")

    def test_valid_media_type_accepted(self) -> None:
        assert ContentType("application/json").value == "application/json"

    def test_hash_algorithm_required_and_hex_validated(self) -> None:
        with pytest.raises(ValueError, match="algorithm"):
            ContentHash(algorithm="  ", digest_hex="00")
        with pytest.raises(ValueError, match="hex"):
            ContentHash(algorithm="sha-256", digest_hex="ZZZZ")

    def test_stored_object_negative_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="size_bytes"):
            StoredObject(key=_key(), size_bytes=-1)

    def test_stored_object_metadata_must_be_strings(self) -> None:
        with pytest.raises(ValueError, match="strings"):
            StoredObject(key=_key(), size_bytes=1, metadata={"a": 1})  # type: ignore[dict-item]

    def test_stored_object_metadata_is_immutable(self) -> None:
        stored = StoredObject(key=_key(), size_bytes=1, metadata={"a": "b"})
        with pytest.raises(TypeError):
            stored.metadata["a"] = "c"  # type: ignore[index]

    def test_object_key_is_never_content_hash(self) -> None:
        # Section 19: object key (logical address) is never equated with a
        # representation/content hash.
        stored = StoredObject(
            key=_key(),
            size_bytes=1,
            content_hash=ContentHash(algorithm="sha-256", digest_hex="0" * 64),
        )
        assert stored.key is not stored.content_hash  # type: ignore[comparison-overlap]
        assert stored.key != stored.content_hash  # type: ignore[comparison-overlap]


class TestProviderNeutrality:
    """OBJ-07: no provider-specific ETag/presigned URL in generic DTOs."""

    def test_stored_object_has_no_provider_fields(self) -> None:
        for prohibited in ("etag", "presigned_url", "bucket", "upload_id", "arn"):
            assert not hasattr(StoredObject, prohibited)
            assert not hasattr(StoredObject(_key(), 0), prohibited)

    def test_no_s3_imports_in_contract_module(self) -> None:
        source = pathlib.Path(__file__).resolve().parents[3] / "src"
        text = (source / "darkula" / "app" / "object_store.py").read_text()
        for forbidden in ("import boto3", "from boto3", "from botocore"):
            assert forbidden not in text, f"provider leak in contract: {forbidden}"
