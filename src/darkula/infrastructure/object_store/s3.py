# SPDX-License-Identifier: AGPL-3.0-only
"""S3-compatible ObjectStore adapter (PR 8).

One provider-neutral implementation serves both AWS S3 and Cloudflare R2
through configuration (endpoint/credentials), preserving the existing
:class:`~darkula.app.object_store.ObjectStore` streaming SPI. Provider
concepts (ETag, multipart IDs, bucket ARN, R2 account IDs, storage class)
never cross the Darkula boundary.

SDK contract verified against ``boto3`` 1.43.x on Python 3.14:

- pure-Python over urllib3 (no CRT); installs and imports on 3.14;
- ``endpoint_url`` override is supported for R2/S3-compatible stores;
- streaming upload via ``put_object(Body=file-like)`` (single-PUT; no
  multipart state because PR 8 artifacts are bounded);
- streaming download via ``get_object()['Body']``;
- missing-object identification via ``ClientError`` 404 (``NoSuchKey``); and
- blocking calls are isolated with bounded ``asyncio.to_thread`` so the event
  loop is never blocked.

Frozen missing semantics: ``stat(missing)`` -> ``None``, ``get(missing)`` ->
:class:`ObjectNotFoundError`, ``delete(missing)`` -> idempotent success.
Provider errors map to the existing bounded Darkula errors; provider
exception text never escapes. The Darkula SHA-256 representation hash is
computed on the trusted side over streamed input and is never equated with
the S3 ETag.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import tempfile
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from typing import Any, Literal

from darkula.app.object_store import (
    ContentHash,
    ContentType,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectStoreUnavailableError,
    StoredObject,
)
from darkula.domain.identifiers import ObjectKey

#: Bounded read chunk size for streaming downloads.
_DOWNLOAD_CHUNK = 64 * 1024

#: In-memory spool threshold; larger artifacts roll to a temp file so whole
#: objects never need to be buffered in caller memory.
_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024

#: What a missing object (404) means for a given operation.
_MissingMode = Literal["none", "not_found", "unavailable"]


def _map_client_error(exc: BaseException, *, missing: _MissingMode) -> ObjectStoreError:
    """Translate a boto3/botocore exception to a bounded Darkula error.

    A 404 maps to ``None`` for ``stat``, to :class:`ObjectNotFoundError` for
    ``get``, and is an ordinary unavailable failure otherwise. Provider
    exception text never escapes.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    if isinstance(exc, ClientError):
        status = (exc.response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 404 and missing == "none":
            return None  # type: ignore[return-value]
        if status == 404 and missing == "not_found":
            return ObjectNotFoundError("object not found")
        return ObjectStoreUnavailableError("object store request failed")
    if isinstance(exc, BotoCoreError):
        return ObjectStoreUnavailableError("object store unavailable")
    return ObjectStoreUnavailableError("object store unavailable")


class S3CompatibleObjectStore(ObjectStore):
    """Streaming S3-compatible object store over the Darkula SPI."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        prefix: str | None = None,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 60.0,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Build an S3-compatible adapter; no network call happens here."""
        if not bucket or not bucket.strip():
            raise ValueError("bucket must not be blank")
        self._bucket = bucket.strip()
        self._prefix = (prefix or "").strip("/")
        self._client_factory = client_factory
        self._client_cache: Any | None = None

        kwargs: dict[str, Any] = {}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if region:
            kwargs["region_name"] = region
        if access_key_id:
            kwargs["aws_access_key_id"] = access_key_id
        if secret_access_key:
            kwargs["aws_secret_access_key"] = secret_access_key
        if session_token:
            kwargs["aws_session_token"] = session_token
        import botocore.config

        kwargs["config"] = botocore.config.Config(
            connect_timeout=connect_timeout_seconds,
            read_timeout=read_timeout_seconds,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self._client_kwargs = kwargs

    def _client(self) -> Any:
        """Return the lazily built S3 client, cached for the adapter lifetime.

        No network call happens at construction time; the client is built once
        on first use (boto3 clients are session-cached anyway).
        """
        if self._client_cache is None:
            if self._client_factory is not None:
                self._client_cache = self._client_factory()
            else:
                import boto3

                self._client_cache = boto3.client("s3", **self._client_kwargs)
        return self._client_cache

    def _physical(self, key: ObjectKey) -> str:
        """Return the physically stored object key (with optional prefix)."""
        if not self._prefix:
            return key.value
        return f"{self._prefix}/{key.value}"

    async def put(
        self,
        *,
        key: ObjectKey,
        data: AsyncIterable[bytes],
        content_type: ContentType | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        """Stream ``data`` to ``key`` via one streaming PUT (bounded)."""
        hasher = hashlib.sha256()
        size = 0
        with tempfile.SpooledTemporaryFile(
            max_size=_SPOOL_MEMORY_BYTES, mode="w+b"
        ) as spool:
            async for chunk in data:
                if not isinstance(chunk, bytes):
                    raise ValueError("object chunks must be bytes")
                size += len(chunk)
                hasher.update(chunk)
                await asyncio.to_thread(spool.write, chunk)
            await asyncio.to_thread(spool.flush)
            await asyncio.to_thread(spool.seek, 0)

            def _upload() -> None:
                args: dict[str, Any] = {
                    "Bucket": self._bucket,
                    "Key": self._physical(key),
                    "Body": spool,
                }
                if content_type is not None:
                    args["ContentType"] = content_type.value
                if metadata:
                    args["Metadata"] = dict(metadata)
                self._client().put_object(**args)

            try:
                await asyncio.to_thread(_upload)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise _map_client_error(exc, missing="unavailable") from None

        return StoredObject(
            key=key,
            size_bytes=size,
            content_type=content_type,
            content_hash=ContentHash(
                algorithm="sha-256", digest_hex=hasher.hexdigest()
            ),
            metadata=metadata or {},
        )

    async def get(self, key: ObjectKey) -> AsyncIterable[bytes]:
        """Open a streaming read of ``key``; missing raises not-found."""
        try:
            response = await asyncio.to_thread(
                lambda: self._client().get_object(
                    Bucket=self._bucket, Key=self._physical(key)
                )
            )
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _map_client_error(exc, missing="not_found") from None
        body = response["Body"]

        async def read() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await asyncio.to_thread(body.read, _DOWNLOAD_CHUNK)
                    if not chunk:
                        return
                    yield chunk
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(body.close)

        return read()

    async def stat(self, key: ObjectKey) -> StoredObject | None:
        """Return metadata for ``key``, or ``None`` when absent."""
        try:
            response = await asyncio.to_thread(
                lambda: self._client().head_object(
                    Bucket=self._bucket, Key=self._physical(key)
                )
            )
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            mapped = _map_client_error(exc, missing="none")
            if mapped is None:
                return None
            raise mapped from None
        content_type = response.get("ContentType")
        return StoredObject(
            key=key,
            size_bytes=int(response["ContentLength"]),
            content_type=(
                None if content_type is None else ContentType(str(content_type))
            ),
        )

    async def delete(self, key: ObjectKey) -> None:
        """Delete ``key``; deleting a missing object is idempotent success."""
        try:
            await asyncio.to_thread(
                lambda: self._client().delete_object(
                    Bucket=self._bucket, Key=self._physical(key)
                )
            )
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _map_client_error(exc, missing="unavailable") from None


__all__ = ["S3CompatibleObjectStore"]
