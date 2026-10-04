# SPDX-License-Identifier: AGPL-3.0-only
"""S-series: S3-compatible ObjectStore adapter semantics (PR 8).

Tested against a deterministic fake client (no live AWS/R2 dependency), per
the PR 8 plan (S-series strategy: SDK fakes for unit tests; live S3-compatible
infrastructure deferred to PR 16).
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any

import pytest

from darkula.app.object_store import (
    ContentHash,
    ContentType,
    ObjectNotFoundError,
    ObjectStoreUnavailableError,
    StoredObject,
)
from darkula.domain.identifiers import ObjectKey
from darkula.infrastructure.object_store.s3 import S3CompatibleObjectStore


class _ClientError(Exception):
    """Bounded stand-in for botocore.exceptions.ClientError."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}}


def _missing_client_error() -> BaseException:
    """A real botocore ClientError with HTTP 404 (NoSuchKey/Head)."""
    from typing import cast

    from botocore.exceptions import ClientError

    return cast(
        BaseException,
        ClientError(
            {
                "Error": {
                    "Code": "NoSuchKey",
                    "Message": "The specified key does not exist.",
                },
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        ),
    )


class _FakeClient:
    """Deterministic client implementing the boto3 surface the adapter uses."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_args: list[dict[str, Any]] = []
        self.saw_failures = False

    def _raise_missing(self) -> None:
        raise _missing_client_error()

    def put_object(self, **kw: Any) -> None:
        self.put_args.append(kw)
        if self.saw_failures:
            raise _ClientError(500)
        body = kw["Body"]
        data = body.read() if hasattr(body, "read") else body
        self.objects[kw["Key"]] = bytes(data)

    def head_object(self, **kw: Any) -> dict[str, Any]:
        data = self.objects.get(kw["Key"])
        if data is None:
            self._raise_missing()
        assert data is not None
        return {"ContentLength": len(data), "ContentType": "text/plain"}

    def get_object(self, **kw: Any) -> dict[str, Any]:
        data = self.objects.get(kw["Key"])
        if data is None:
            self._raise_missing()
        assert data is not None
        return {"Body": io.BytesIO(data)}

    def delete_object(self, **kw: Any) -> None:
        self.objects.pop(kw["Key"], None)


class _UnavailableClient(_FakeClient):
    def head_object(self, **kw: Any) -> dict[str, Any]:
        raise _EndpointUnavailable


class _EndpointUnavailable(Exception):
    pass


def _store(client: _FakeClient | None = None) -> S3CompatibleObjectStore:
    return S3CompatibleObjectStore(
        bucket="darkula-test",
        endpoint_url="http://127.0.0.1:9000/",
        access_key_id="k",
        secret_access_key="s",
        client_factory=lambda: client or _FakeClient(),
    )


def _key() -> ObjectKey:
    return ObjectKey("sha256/ab/ab")


async def _chunks(data: bytes, size: int = 16) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


class TestPut:
    """S1/S10/S13: put streams and computes Darkula SHA-256."""

    @pytest.mark.asyncio
    async def test_s1_put_stream_stores_size_and_hash(self) -> None:
        client = _FakeClient()
        store = _store(client)
        payload = b"x" * 100
        stored = await store.put(
            key=_key(), data=_chunks(payload), content_type=ContentType("text/plain")
        )
        assert isinstance(stored, StoredObject)
        assert stored.size_bytes == 100
        assert stored.content_hash is not None
        import hashlib

        assert stored.content_hash.digest_hex == hashlib.sha256(payload).hexdigest()
        # physical key carries the bucket + (no prefix) logical key
        assert "sha256/ab/ab" in client.objects
        assert client.put_args[0]["Bucket"] == "darkula-test"

    @pytest.mark.asyncio
    async def test_s10_hash_independent_of_etag(self) -> None:
        # Darkula SHA-256 is computed on the trusted side; the adapter never
        # surfaces an ETag as a representation hash.
        client = _FakeClient()
        store = _store(client)
        payload = b"abc"
        stored = await store.put(key=_key(), data=_chunks(payload))
        import hashlib

        assert stored.content_hash == ContentHash(
            algorithm="sha-256", digest_hex=hashlib.sha256(payload).hexdigest()
        )
        # The fake client records its own ETag-independent store.
        assert client.put_args[0].get("Metadata") in ({}, None)


class TestGetStatDelete:
    """S2-S6: round-trip and frozen missing semantics."""

    @pytest.mark.asyncio
    async def test_s2_get_stream_round_trip(self) -> None:
        store = _store()
        payload = b"round-trip-bytes"
        await store.put(key=_key(), data=_chunks(payload))
        reader = await store.get(_key())
        assert b"".join([c async for c in reader]) == payload

    @pytest.mark.asyncio
    async def test_s3_stat_present(self) -> None:
        client = _FakeClient()
        store = _store(client)
        await store.put(key=_key(), data=_chunks(b"metadata"))
        found = await store.stat(_key())
        assert found is not None
        assert found.size_bytes == len(b"metadata")

    @pytest.mark.asyncio
    async def test_s4_stat_missing_returns_none(self) -> None:
        store = _store()
        assert await store.stat(_key()) is None

    @pytest.mark.asyncio
    async def test_s5_get_missing_raises_not_found(self) -> None:
        store = _store()
        with pytest.raises(ObjectNotFoundError):
            await store.get(_key())

    @pytest.mark.asyncio
    async def test_s6_delete_missing_is_idempotent(self) -> None:
        store = _store()
        await store.delete(_key())  # must not raise
        # and a present delete works + then stat is missing
        client = _FakeClient()
        mystore = _store(client)
        await mystore.put(key=_key(), data=_chunks(b"abc"))
        await mystore.delete(_key())
        assert await mystore.stat(_key()) is None


class TestFailures:
    """S7/S9/S12: provider errors are bounded, no raw exception leaks."""

    @pytest.mark.asyncio
    async def test_s7_provider_unavailable_maps_to_bounded(self) -> None:
        store = _store(_UnavailableClient())
        with pytest.raises(ObjectStoreUnavailableError):
            await store.stat(_key())

    @pytest.mark.asyncio
    async def test_s9_put_failure_maps_to_bounded_not_provider(self) -> None:
        client = _FakeClient()
        client.saw_failures = True
        store = _store(client)
        try:
            await store.put(key=_key(), data=_chunks(b"boom"))
        except ObjectStoreUnavailableError as exc:
            assert "ClientError" not in str(exc)
            return
        raise AssertionError("expected ObjectStoreUnavailableError")

    @pytest.mark.asyncio
    async def test_s8_endpoint_override_accepted(self) -> None:
        # The adapter accepts an endpoint override (R2/S3-compatible).
        store = S3CompatibleObjectStore(
            bucket="b", endpoint_url="https://r2-custom.example/account/bucket"
        )
        assert store is not None


class TestPrefix:
    """S1b: optional prefix is applied to physical keys only."""

    @pytest.mark.asyncio
    async def test_prefix_isolates_physical_key(self) -> None:
        client = _FakeClient()
        store = S3CompatibleObjectStore(
            bucket="darkula-test", prefix="tenantA", client_factory=lambda: client
        )
        await store.put(key=_key(), data=_chunks(b"data"))
        assert "tenantA/sha256/ab/ab" in client.objects
        # logical key is unchanged
        assert await store.stat(_key()) is not None
