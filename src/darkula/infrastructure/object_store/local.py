# SPDX-License-Identifier: AGPL-3.0-only
"""Root-confined local filesystem ObjectStore (PR 3).

A deterministic, offline implementation of
:class:`~darkula.app.object_store.ObjectStore` on the local filesystem. It
is local development/telemetry infrastructure, **not** the Podman or real
infrastructure integration path.

Guarantees:

- constructed with an explicit root; every key must resolve under that root
  (traversal segments and backslashes are rejected, and escaped
  symlink targets fail closed with :class:`InvalidObjectKeyError`);
- ``put`` consumes :class:`AsyncIterable[bytes]`, rejects non-bytes chunks,
  and writes incrementally to a temporary file that is atomically renamed
  over the final path only on success, so ordinary failures/cancellation
  never expose a partial final object and clean up their temporary file;
- ``get`` yields bounded chunks while streaming;
- ``stat``/``get``/``delete`` follow the frozen missing semantics;
- a SHA-256 :class:`ContentHash` is computed consistently on every ``put``.
- custom metadata/content type are kept in memory only (a sidecar subsystem
  is explicitly out of scope); after a process restart ``stat`` returns the
  durable size but ``content_type``/``metadata``/hash are no longer known.

All blocking filesystem calls are dispatched through ``asyncio.to_thread``
(stdlib only); no async-file dependency is introduced.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import os
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from pathlib import Path

from darkula.app.object_store import (
    ContentHash,
    ContentType,
    InvalidObjectKeyError,
    ObjectNotFoundError,
    ObjectStore,
    StoredObject,
)
from darkula.domain.identifiers import ObjectKey

#: Default read/write chunk size for streaming (bounded).
DEFAULT_CHUNK_SIZE = 64 * 1024

#: Key segments that would make a key non-plain (empty, ``.``, ``..``).
_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})


def _unlink_if_present(path: Path) -> None:
    """Best-effort unlink used for cleanup; never raises."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


class LocalFileObjectStore(ObjectStore):
    """Streaming local filesystem object store confined to one root."""

    def __init__(
        self,
        root: Path | str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        """Freeze the absolute root and the bounded chunk size."""
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        self._root = Path(root).expanduser().resolve()
        self._chunk_size = chunk_size
        self._metadata: dict[ObjectKey, StoredObject] = {}

    @property
    def root(self) -> Path:
        """Return the frozen absolute confinement root."""
        return self._root

    def _resolve(self, key: ObjectKey) -> Path:
        """Return the root-confined destination path or raise.

        Rejects traversal segments (``.``/``..``/empty), backslashes, and
        any resolved path that escapes the root (including through
        symlinks). This is fail-closed at the store boundary.
        """
        value = key.value
        if "\\" in value:
            raise InvalidObjectKeyError(
                "object key must not contain backslash characters"
            )
        segments = value.split("/")
        if any(segment in _FORBIDDEN_SEGMENTS for segment in segments):
            raise InvalidObjectKeyError(
                "object key must be a plain relative path without traversal"
            )
        resolved = self._root.joinpath(*segments).resolve()
        if not resolved.is_relative_to(self._root):
            raise InvalidObjectKeyError("object key escapes the store root")
        return resolved

    async def put(
        self,
        *,
        key: ObjectKey,
        data: AsyncIterable[bytes],
        content_type: ContentType | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        """Stream ``data`` to ``key`` via temp file + atomic replace."""
        destination = self._resolve(key)
        await asyncio.to_thread(
            functools.partial(destination.parent.mkdir, parents=True, exist_ok=True)
        )
        temporary = (
            destination.parent / f".{destination.name}.darkula-tmp-{uuid.uuid4().hex}"
        )
        handle = await asyncio.to_thread(open, temporary, "wb")
        size = 0
        hasher = hashlib.sha256()
        try:
            async for chunk in data:
                if not isinstance(chunk, bytes):
                    raise ValueError("object chunks must be bytes")
                size += len(chunk)
                hasher.update(chunk)
                await asyncio.to_thread(handle.write, chunk)
            await asyncio.to_thread(handle.flush)
        except BaseException:
            # Best-effort cleanup; it must never mask the original error.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(handle.close)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_unlink_if_present, temporary)
            raise
        else:
            await asyncio.to_thread(handle.close)
            await asyncio.to_thread(os.replace, temporary, destination)

        stored = StoredObject(
            key=key,
            size_bytes=size,
            content_type=content_type,
            content_hash=ContentHash(
                algorithm="sha-256", digest_hex=hasher.hexdigest()
            ),
            metadata=metadata or {},
        )
        self._metadata[key] = stored
        return stored

    async def get(self, key: ObjectKey) -> AsyncIterable[bytes]:
        """Stream ``key`` in bounded chunks; missing raises not-found."""
        path = self._resolve(key)
        if not await asyncio.to_thread(path.is_file):
            raise ObjectNotFoundError(f"object not found: {key}")

        async def read() -> AsyncIterator[bytes]:
            handle = await asyncio.to_thread(open, path, "rb")
            try:
                while True:
                    chunk = await asyncio.to_thread(handle.read, self._chunk_size)
                    if not chunk:
                        return
                    yield chunk
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(handle.close)

        return read()

    async def stat(self, key: ObjectKey) -> StoredObject | None:
        """Return stored metadata, a durable size-only record, or ``None``."""
        cached = self._metadata.get(key)
        if cached is not None:
            return cached
        path = self._resolve(key)
        if not await asyncio.to_thread(path.is_file):
            return None
        size = (await asyncio.to_thread(path.stat)).st_size
        return StoredObject(key=key, size_bytes=size)

    async def delete(self, key: ObjectKey) -> None:
        """Delete ``key``; deleting a missing object is idempotent success."""
        path = self._resolve(key)
        self._metadata.pop(key, None)
        await asyncio.to_thread(_unlink_if_present, path)


__all__ = ["DEFAULT_CHUNK_SIZE", "LocalFileObjectStore"]
