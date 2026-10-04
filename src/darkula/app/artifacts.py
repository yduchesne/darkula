# SPDX-License-Identifier: AGPL-3.0-only
"""Content-addressed artifact storage over the existing ObjectStore (PR 8).

This module owns the deterministic content-addressable physical key layout
and hash-first deduplication against the reused
:class:`~darkula.app.object_store.ObjectStore` SPI. It never introduces a
second blob/artifact SPI and never leaks provider (S3/R2/filesystem)
concepts.

- ``ObjectStore`` owns bytes.
- :class:`ContentArtifact` owns structured identity/metadata/provenance
  references (persisted in PostgreSQL by the content repository).
- A :class:`ContentArtifact` record and its physical ``ObjectKey`` may be
  shared by many normalized observations; physical deduplication never merges
  provenance.

Key layout (section 14): ``sha256/<first-two-hex>/<full-digest>`` — only
lowercase safe characters, no source-controlled path fragments, no
hostname/filename, no credentials, and no timestamp/random suffix for a
deduplicated complete representation.

Deduplication algorithm (section 16-17): derive the key from the exact
representation hash, ``stat`` it, and:

- missing -> stream ``put`` the bytes (never trusting a caller-supplied hash
  alone; the stored object is checked against the expected representation);
- present -> verify the existing object by streaming and recomputing SHA-256
  (reliable, adapter-agnostic) after a fast size comparison; any mismatch is
  a typed :class:`ArtifactIntegrityError` and the object is never overwritten.

Provider errors are mapped to the existing bounded ObjectStore errors;
provider exception text never escapes.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from darkula.app.object_store import (
    ContentHash,
    ContentType,
    ObjectStore,
)
from darkula.domain.content import (
    CONTENT_ADDRESSED_KEY_ALGORITHM,
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
)
from darkula.domain.identifiers import ContentArtifactId, ObjectKey
from darkula.telemetry.decorators import counted, timed, traced
from darkula.telemetry.metrics import get_counter

#: Default bounded read chunk size when verifying an existing object.
_READ_CHUNK = 64 * 1024


class ArtifactStorageError(RuntimeError):
    """Bounded, provider-neutral artifact-storage failure.

    Public messages never echo object content or provider exception text.
    """


class ArtifactIntegrityError(ArtifactStorageError):
    """The deterministic key exists but its bytes/size do not match the
    expected representation. Nothing is overwritten and no success is
    claimed."""


def content_addressed_key(content_hash: ContentHash) -> ObjectKey:
    """Return the deterministic physical ``ObjectKey`` for a representation.

    The key is derived only from the representation hash (``sha-256``),
    independent of source URI, filename, and provenance.
    """
    if content_hash.algorithm != CONTENT_ADDRESSED_KEY_ALGORITHM:
        raise ValueError(
            "content-addressed keys require the sha-256 representation "
            f"algorithm (got {content_hash.algorithm!r})"
        )
    digest = content_hash.digest_hex
    if len(digest) != 64:
        raise ValueError("sha-256 digest must be 64 hex characters")
    return ObjectKey(f"sha256/{digest[:2]}/{digest}")


def _sha256_of_bytes(data: bytes) -> ContentHash:
    """Return the Darkula sha-256 representation hash of ``data``."""
    return ContentHash(algorithm="sha-256", digest_hex=hashlib.sha256(data).hexdigest())


async def _compute_hash(reader: AsyncIterable[bytes]) -> ContentHash:
    """Recompute the Darkula sha-256 hash of a streamed byte source."""
    hasher = hashlib.sha256()
    async for chunk in reader:
        if not isinstance(chunk, bytes):
            raise ValueError("object chunks must be bytes")
        hasher.update(chunk)
    return ContentHash(algorithm="sha-256", digest_hex=hasher.hexdigest())


async def _iter_bytes(data: bytes) -> AsyncIterator[bytes]:
    """Yield ``data`` in bounded chunks."""
    for index in range(0, len(data), _READ_CHUNK):
        yield data[index : index + _READ_CHUNK]


@dataclass(frozen=True, slots=True)
class StoredArtifactResult:
    """Outcome of one :meth:`ArtifactStorageService.store` call.

    ``artifact`` is the :class:`ContentArtifact` record to persist
    (provider-neutral metadata; the bytes already live in ObjectStore).
    ``deduplicated`` is true when the physical object already existed and was
    verified (no duplicate upload occurred). Provenance remains independent:
    every ``store`` returns a proper record regardless of ``deduplicated``.
    """

    artifact: ContentArtifact
    deduplicated: bool


class ArtifactStorageService:
    """Hash-first, content-addressed artifact storage over ObjectStore.

    Deterministic and provider-neutral; owns no database and holds no
    transaction.
    """

    def __init__(self, *, object_store: ObjectStore) -> None:
        self._store = object_store

    @counted(metric="artifact.store.count")
    @timed(metric="artifact.store.duration")
    @traced(span_name="artifact.store")
    async def store(
        self,
        *,
        kind: ArtifactKind,
        completeness: ArtifactCompleteness,
        content_type: ContentType | None,
        content_hash: ContentHash,
        size_bytes: int,
        data: bytes,
        created_at: datetime,
    ) -> StoredArtifactResult:
        """Deduplicate-and-store one complete representation.

        ``content_hash``/``size_bytes`` describe the exact ``data`` bytes of
        the representation being stored; the caller already has a trusted
        hash/blob from normalization.
        """
        if size_bytes != len(data):
            raise ArtifactStorageError(
                "artifact size does not match the supplied representation"
            )
        key = content_addressed_key(content_hash)
        existing = await self._store.stat(key)
        if existing is None:
            stored = await self._store.put(
                key=key,
                data=_iter_bytes(data),
                content_type=content_type,
                metadata={},
            )
            # Trust the store's returned representation hash only after
            # independently checking the size; a mismatching hash is an
            # integrity failure, never a silent success.
            if stored.size_bytes != len(data):
                raise ArtifactIntegrityError(
                    "stored artifact size does not match the representation"
                )
            if stored.content_hash is not None and stored.content_hash != content_hash:
                raise ArtifactIntegrityError(
                    "stored artifact hash does not match the representation"
                )
            deduplicated = False
        else:
            if existing.size_bytes != size_bytes:
                raise ArtifactIntegrityError(
                    "existing artifact has an incompatible size"
                )
            recomputed = await _compute_hash(await self._store.get(key))
            if recomputed != content_hash:
                raise ArtifactIntegrityError("existing artifact has incompatible bytes")
            deduplicated = True

        if deduplicated:
            get_counter("artifact.deduplicated").add(1)
        else:
            get_counter("artifact.stored").add(1)
            get_counter("artifact.bytes").add(size_bytes)

        artifact = ContentArtifact(
            artifact_id=ContentArtifactId.generate(),
            object_key=key,
            content_type=content_type,
            size_bytes=size_bytes,
            content_hash=content_hash,
            artifact_kind=kind,
            completeness=completeness,
            created_at=created_at,
        )
        return StoredArtifactResult(artifact=artifact, deduplicated=deduplicated)


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactStorageError",
    "ArtifactStorageService",
    "StoredArtifactResult",
    "content_addressed_key",
]
