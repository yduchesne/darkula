# SPDX-License-Identifier: AGPL-3.0-only
"""Shared canonical normalized-text loading and integrity verification.

Both deterministic (PR 11) and semantic (PR 12) extraction, plus geographic
resolution, must load the canonical ``NORMALIZED_TEXT`` representation from
ObjectStore --- never the bounded PostgreSQL preview --- and verify it before
use. This module owns that single bounded/verified read so the rules cannot
drift between services.

The DB preview is never a fallback. A representation is "extractable" only
when it is a persisted ``NORMALIZED_TEXT`` artifact with an explicit
``text/plain`` media type and a representation hash.
"""

from __future__ import annotations

import hashlib

from darkula.app.object_store import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
)
from darkula.domain.content import (
    MAX_NORMALIZED_TEXT_BYTES,
    ArtifactKind,
    ContentArtifact,
)

#: Canonical media type required of an extractable normalized-text artifact.
NORMALIZED_TEXT_CONTENT_TYPE = "text/plain"


class CanonicalInputError(RuntimeError):
    """Base class for bounded canonical-input failures.

    Public messages never echo ObjectKey, URI, content, or provider text.
    """


class CanonicalInputUnavailableError(CanonicalInputError):
    """The canonical representation is missing/unavailable in ObjectStore."""


class CanonicalInputIntegrityError(CanonicalInputError):
    """The canonical bytes do not match persisted metadata or are not text."""


class CanonicalInputTooLargeError(CanonicalInputError):
    """The canonical representation exceeds the input byte bound."""


def is_extractable(artifact: ContentArtifact) -> bool:
    """Return whether an artifact is the canonical normalized-text representation."""
    return (
        artifact.artifact_kind is ArtifactKind.NORMALIZED_TEXT
        and artifact.content_type is not None
        and artifact.content_type.value == NORMALIZED_TEXT_CONTENT_TYPE
        and artifact.content_hash is not None
    )


async def load_canonical_text(
    object_store: ObjectStore,
    artifact: ContentArtifact,
    *,
    max_bytes: int = MAX_NORMALIZED_TEXT_BYTES,
) -> str:
    """Stream, verify, and decode the canonical normalized-text representation.

    Verifies: sha-256 algorithm, bounded byte count, object existence, exact
    persisted size, matching SHA-256, and strict UTF-8. S3/R2 ETag is never
    treated as the Darkula SHA-256 representation hash.
    """
    if artifact.content_hash is None:
        raise CanonicalInputIntegrityError("canonical artifact has no hash")
    if artifact.content_hash.algorithm != "sha-256":
        raise CanonicalInputIntegrityError(
            "canonical artifact does not use the sha-256 representation hash"
        )
    if max_bytes < 1:
        raise ValueError("max_bytes must be >= 1")
    if artifact.size_bytes > max_bytes:
        raise CanonicalInputTooLargeError(
            "canonical representation exceeds the input byte bound"
        )

    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        reader = await object_store.get(artifact.object_key)
        async for chunk in reader:
            if not isinstance(chunk, bytes):
                raise CanonicalInputIntegrityError(
                    "canonical representation yielded non-byte data"
                )
            total += len(chunk)
            if total > max_bytes:
                raise CanonicalInputTooLargeError(
                    "canonical representation exceeds the input byte bound"
                )
            hasher.update(chunk)
            chunks.append(chunk)
    except ObjectNotFoundError as exc:
        raise CanonicalInputUnavailableError(
            "canonical representation is unavailable"
        ) from exc
    except ObjectStoreError as exc:
        raise CanonicalInputUnavailableError(
            "canonical representation could not be read"
        ) from exc

    if total != artifact.size_bytes:
        raise CanonicalInputIntegrityError(
            "canonical representation size does not match persisted metadata"
        )
    if hasher.hexdigest() != artifact.content_hash.digest_hex:
        raise CanonicalInputIntegrityError(
            "canonical representation hash does not match persisted metadata"
        )
    try:
        return b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CanonicalInputIntegrityError(
            "canonical representation is not valid UTF-8 text"
        ) from exc


__all__ = [
    "NORMALIZED_TEXT_CONTENT_TYPE",
    "CanonicalInputError",
    "CanonicalInputIntegrityError",
    "CanonicalInputTooLargeError",
    "CanonicalInputUnavailableError",
    "is_extractable",
    "load_canonical_text",
]
