# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL :class:`~darkula.app.repositories.ContentRepository`.

Implements the content repository contract over versioned stored functions.
Consistent with the Darkula stored-function-only invariant, this module
contains **no data-access SQL**: every operation is one stored-function
invocation and every result is mapped centrally through
:mod:`darkula.infrastructure.persistence.postgresql.mapping`.

ObjectStore owns artifact bytes; this repository persists only structured
identity/provenance metadata. Controlled conflicts surface as typed
:class:`~darkula.app.persistence.ConflictError` /
:class:`~darkula.app.persistence.IntegrityError`. ``asyncio.CancelledError``
propagates unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

from psycopg.types.json import Jsonb

from darkula.app.object_store import ContentHash
from darkula.app.persistence import ConflictError, IntegrityError
from darkula.app.repositories import ContentRepository
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.identifiers import ContentArtifactId, NormalizedContentId
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_content_artifact,
    map_normalized_content,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

# Stored-function invocations (versioned in migrations/0003_content.sql).
_CREATE_ARTIFACT_FN = (
    "SELECT * FROM content_artifact_create_v1(%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_GET_ARTIFACT_FN = "SELECT * FROM content_artifact_get_v1(%s)"
_FIND_ARTIFACT_FN = "SELECT * FROM content_artifact_find_v1(%s, %s, %s, %s)"
_CREATE_OBSERVATION_FN = (
    "SELECT * FROM normalized_content_create_v1("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_GET_OBSERVATION_FN = "SELECT * FROM normalized_content_get_v1(%s)"


def _jsonb(value: Any) -> Any:
    """Wrap a JSON-compatible value for a jsonb parameter (None stays NULL)."""
    return None if value is None else Jsonb(value)


class PostgresContentRepository(ContentRepository):
    """Content repository bound to one PostgresUnitOfWork transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def create_artifact(self, artifact: ContentArtifact) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _CREATE_ARTIFACT_FN,
                    (
                        str(artifact.artifact_id),
                        artifact.object_key.value,
                        None
                        if artifact.content_type is None
                        else artifact.content_type.value,
                        artifact.size_bytes,
                        (
                            None
                            if artifact.content_hash is None
                            else artifact.content_hash.algorithm
                        ),
                        (
                            None
                            if artifact.content_hash is None
                            else artifact.content_hash.digest_hex
                        ),
                        artifact.artifact_kind.value,
                        artifact.completeness.value,
                        artifact.created_at,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate":
            raise ConflictError("an artifact with this identity already exists")

    async def get_artifact(
        self, artifact_id: ContentArtifactId
    ) -> ContentArtifact | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_GET_ARTIFACT_FN, (str(artifact_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_content_artifact(row)

    async def find_artifact_by_representation(
        self,
        *,
        content_hash: ContentHash,
        kind: ArtifactKind,
        completeness: ArtifactCompleteness,
    ) -> ContentArtifact | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _FIND_ARTIFACT_FN,
                    (
                        content_hash.algorithm,
                        content_hash.digest_hex,
                        kind.value,
                        completeness.value,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_content_artifact(row)

    async def create_observation(self, content: NormalizedContent) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _CREATE_OBSERVATION_FN,
                    (
                        str(content.content_id),
                        (
                            None
                            if content.artifact_id is None
                            else str(content.artifact_id)
                        ),
                        content.source_uri,
                        content.title,
                        _text_preview(content.text),
                        (
                            None
                            if content.content_type is None
                            else content.content_type.value
                        ),
                        content.observed_at,
                        content.crawl_request_id,
                        content.observation_index,
                        content.normalization_version,
                        (
                            None
                            if content.content_hash is None
                            else content.content_hash.algorithm
                        ),
                        (
                            None
                            if content.content_hash is None
                            else content.content_hash.digest_hex
                        ),
                        content.completeness.value,
                        _jsonb(
                            None
                            if content.structural_metadata is None
                            else dict(content.structural_metadata)
                        ),
                        # created_at == observed_at for observations.
                        content.observed_at,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate_provenance":
            raise ConflictError("an observation with this provenance already exists")
        if outcome == "unknown_artifact":
            raise IntegrityError("the referenced artifact does not exist")

    async def get_observation(
        self, content_id: NormalizedContentId
    ) -> NormalizedContent | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_GET_OBSERVATION_FN, (str(content_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_normalized_content(row)


def _text_preview(text: str | None) -> str | None:
    """Return a bounded review of the normalized text for durable metadata.

    The authoritative full normalized text lives in ObjectStore as the
    NORMALIZED_TEXT artifact; PostgreSQL stores only a small bounded preview
    (never the full large body, avoiding redundant authoritative storage).
    """
    if text is None:
        return None
    return text[:2000]


__all__ = ["PostgresContentRepository"]
