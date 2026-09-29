# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula-owned streaming object-store contract.

Large, raw, and binary artifacts live in the ObjectStore (PostgreSQL remains
authoritative for structured domain state; DataStream carries commands,
events, and references). The contract is streaming-first: writes accept
asynchronous byte chunks and reads yield asynchronous byte chunks, so whole
objects never need to be buffered in memory.

Provider concepts (bucket ARN, S3 ETag, multipart upload ID, presigned URL,
R2 account ID, storage class) are intentionally absent from this generic
interface. A provider-specific ETag is never presented as a universal
content hash; when a hash is represented its algorithm is explicit.

Frozen missing-object semantics:

- ``stat(missing)`` -> ``None``;
- ``get(missing)`` -> ``ObjectNotFoundError``;
- ``delete(missing)`` -> idempotent success.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from darkula.domain.identifiers import ObjectKey

_MEDIA_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_HEX_PATTERN = re.compile(r"^[0-9a-f]+$")


@dataclass(frozen=True, slots=True)
class ContentType:
    """RFC-6838-style media type wrapper (for example ``application/pdf``)."""

    value: str

    def __post_init__(self) -> None:
        """Validate the ``type/subtype`` media-type shape."""
        if not self.value or not self.value.strip():
            raise ValueError("content type must not be blank")
        value = self.value.strip()
        if len(value) > 255:
            raise ValueError("content type must not exceed 255 characters")
        if _MEDIA_TYPE_PATTERN.fullmatch(value) is None:
            raise ValueError("content type must be a valid media type")
        object.__setattr__(self, "value", value)


@dataclass(frozen=True, slots=True)
class ContentHash:
    """Explicit-algorithm representation/content hash.

    ``algorithm`` is a bounded explicit identifier (for example ``sha-256``);
    ``digest_hex`` is the lowercase hex digest. A representation hash is
    distinct from identity and from an ``ObjectKey``.
    """

    algorithm: str
    digest_hex: str

    def __post_init__(self) -> None:
        """Validate the algorithm name and hex digest form."""
        if not self.algorithm or not self.algorithm.strip():
            raise ValueError("hash algorithm must not be blank")
        algorithm = self.algorithm.strip()
        if len(algorithm) > 64:
            raise ValueError("hash algorithm must not exceed 64 characters")
        if any(ord(char) < 32 for char in algorithm):
            raise ValueError("hash algorithm must not contain control characters")
        if not self.digest_hex or not self.digest_hex.strip():
            raise ValueError("hash digest must not be blank")
        digest = self.digest_hex.strip().lower()
        if not _HEX_PATTERN.fullmatch(digest):
            raise ValueError("hash digest must be a lowercase hex string")
        object.__setattr__(self, "algorithm", algorithm)
        object.__setattr__(self, "digest_hex", digest)


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Provider-neutral metadata/result for one stored object.

    No ETag, presigned URL, multipart upload ID, bucket ARN, or storage
    class appears here; ``content_hash`` is explicit about its algorithm.
    """

    key: ObjectKey
    """Logical object-store address (never a content hash)."""

    size_bytes: int
    """Exact byte size, always >= 0."""

    content_type: ContentType | None = None
    """Optional media type of the stored object."""

    content_hash: ContentHash | None = None
    """Optional explicit-algorithm representation hash."""

    metadata: Mapping[str, str] = field(default_factory=dict)
    """Bounded provider-neutral custom metadata (immutable after creation)."""

    def __post_init__(self) -> None:
        """Validate size and freeze the metadata mapping."""
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be >= 0")
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("object metadata keys and values must be strings")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class ObjectStoreError(RuntimeError):
    """Base class for bounded, provider-neutral ObjectStore failures.

    Public messages never echo object content or cloud/filesystem provider
    exception text.
    """


class ObjectNotFoundError(ObjectStoreError):
    """Raised when a requested object does not exist (e.g. on ``get``)."""


class ObjectStoreUnavailableError(ObjectStoreError):
    """Raised when the store itself is unavailable or fails."""


class InvalidObjectKeyError(ObjectStoreError):
    """Raised for an unusable object key at the store boundary."""


class ObjectStore(ABC):
    """Streaming asynchronous object storage.

    Implementations stream bytes in both directions and never expose
    S3/R2/filesystem-specific types or credentials through this contract.
    """

    @abstractmethod
    async def put(
        self,
        *,
        key: ObjectKey,
        data: AsyncIterable[bytes],
        content_type: ContentType | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        """Stream-upload ``data`` to ``key`` and return its new metadata.

        Consumes the full async byte stream; no whole-object buffering is
        required by the caller.
        """

    @abstractmethod
    async def get(self, key: ObjectKey) -> AsyncIterable[bytes]:
        """Open a streaming read of ``key``.

        Raises :class:`ObjectNotFoundError` when the object is missing. The
        returned async iterable is consumed by the caller with ``async for``.
        """

    @abstractmethod
    async def stat(self, key: ObjectKey) -> StoredObject | None:
        """Return metadata for ``key``, or ``None`` when absent."""

    @abstractmethod
    async def delete(self, key: ObjectKey) -> None:
        """Delete ``key``; deleting a missing object is idempotent success."""


__all__ = [
    "ContentHash",
    "ContentType",
    "InvalidObjectKeyError",
    "ObjectNotFoundError",
    "ObjectStore",
    "ObjectStoreError",
    "ObjectStoreUnavailableError",
    "StoredObject",
]
