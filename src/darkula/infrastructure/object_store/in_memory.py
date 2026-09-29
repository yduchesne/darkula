# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical in-memory ObjectStore implementation (PR 3).

Implements the exact streaming contract of
:class:`~darkula.app.object_store.ObjectStore` with instance-isolated state:

- ``put`` consumes an :class:`AsyncIterable[bytes]` and rejects invalid
  (non-bytes) chunks;
- ``get`` returns an async byte iterable (never raw bytes) and streams in
  bounded chunks;
- object sizes are exact; content type and metadata are preserved;
- a SHA-256 :class:`ContentHash` is computed consistently on every ``put``
  (representation hash only; never an ``ObjectKey`` and never identity);
- frozen missing semantics: ``stat(missing)`` -> ``None``,
  ``get(missing)`` -> :class:`ObjectNotFoundError`,
  ``delete(missing)`` -> idempotent success.

The store never buffers whole objects in caller memory beyond one object's
payload (in-memory by definition), and never touches the network or
filesystem.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass

from darkula.app.object_store import (
    ContentHash,
    ContentType,
    ObjectNotFoundError,
    ObjectStore,
    StoredObject,
)
from darkula.domain.identifiers import ObjectKey

#: Bounded chunk size used when streaming reads back out.
_READ_CHUNK_SIZE = 4096


@dataclass(slots=True)
class _Entry:
    """One stored object's payload plus its frozen metadata."""

    payload: bytes
    stored: StoredObject


class InMemoryObjectStore(ObjectStore):
    """Deterministic, offline, instance-isolated in-memory object store."""

    def __init__(self) -> None:
        """Start with no stored objects."""
        self._objects: dict[ObjectKey, _Entry] = {}

    async def put(
        self,
        *,
        key: ObjectKey,
        data: AsyncIterable[bytes],
        content_type: ContentType | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        """Consume the full byte stream and store it under ``key``."""
        buffer = bytearray()
        async for chunk in data:
            if not isinstance(chunk, bytes):
                raise ValueError("object chunks must be bytes")
            buffer.extend(chunk)
        payload = bytes(buffer)
        stored = StoredObject(
            key=key,
            size_bytes=len(payload),
            content_type=content_type,
            content_hash=ContentHash(
                algorithm="sha-256", digest_hex=hashlib.sha256(payload).hexdigest()
            ),
            metadata=metadata or {},
        )
        self._objects[key] = _Entry(payload=payload, stored=stored)
        return stored

    async def get(self, key: ObjectKey) -> AsyncIterable[bytes]:
        """Open a streaming read of ``key`` in bounded chunks."""
        entry = self._objects.get(key)
        if entry is None:
            raise ObjectNotFoundError(f"object not found: {key}")

        async def read() -> AsyncIterator[bytes]:
            for index in range(0, len(entry.payload), _READ_CHUNK_SIZE):
                yield entry.payload[index : index + _READ_CHUNK_SIZE]

        return read()

    async def stat(self, key: ObjectKey) -> StoredObject | None:
        """Return the stored metadata or ``None`` when absent."""
        entry = self._objects.get(key)
        return None if entry is None else entry.stored

    async def delete(self, key: ObjectKey) -> None:
        """Delete ``key``; deleting a missing object is idempotent success."""
        self._objects.pop(key, None)


__all__ = ["InMemoryObjectStore"]
