# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL :class:`~darkula.app.repositories.RelationshipRepository`.

Implements the immutable/versioned relationship-assertion contract over
versioned stored functions. Consistent with the Darkula stored-function-only
invariant, this module contains **no data-access SQL**: every operation is one
stored-function invocation and every result is mapped centrally through
:mod:`darkula.infrastructure.persistence.postgresql.mapping`.

Controlled conflicts surface as typed
:class:`~darkula.app.persistence.ConflictError` /
:class:`~darkula.app.persistence.IntegrityError`. ``asyncio.CancelledError``
propagates unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

from psycopg.types.json import Jsonb

from darkula.app.persistence import ConflictError, IntegrityError
from darkula.app.repositories import RelationshipRepository
from darkula.domain.identifiers import (
    ExtractedRelationshipId,
    NormalizedContentId,
    RelationshipExtractionResultId,
)
from darkula.domain.relationships import (
    ExtractedRelationship,
    RelationshipExtractionResult,
)
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_extracted_relationship,
    map_relationship_extraction_result,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

# Stored-function invocations (versioned in migrations/0007_relationships.sql).
_CREATE_RESULT_FN = (
    "SELECT * FROM relationship_extraction_result_create_v1(%s, %s, %s, %s, %s, %s, %s)"
)
_GET_RESULT_FN = "SELECT * FROM relationship_extraction_result_get_v1(%s)"
_GET_RESULT_BY_PROFILE_FN = (
    "SELECT * FROM relationship_extraction_result_get_by_profile_v1(%s, %s, %s)"
)
_CREATE_RELATIONSHIP_FN = (
    "SELECT * FROM extracted_relationship_create_v1("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_LIST_FOR_RESULT_FN = "SELECT * FROM extracted_relationship_list_for_result_v1(%s)"
_LIST_FOR_CONTENT_FN = "SELECT * FROM extracted_relationship_list_for_content_v1(%s)"
_GET_RELATIONSHIP_FN = "SELECT * FROM extracted_relationship_get_v1(%s)"


def _manifest_json(result: RelationshipExtractionResult) -> list[dict[str, str]]:
    """Serialize the exact extractor manifest for the jsonb parameter."""
    return [
        {"name": entry.name, "version": entry.version}
        for entry in result.extractor_manifest
    ]


class PostgresRelationshipRepository(RelationshipRepository):
    """Relationship repository bound to one PostgresUnitOfWork transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def create_result(self, result: RelationshipExtractionResult) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _CREATE_RESULT_FN,
                    (
                        str(result.result_id),
                        str(result.content_id),
                        result.profile_name,
                        result.profile_version,
                        Jsonb(_manifest_json(result)),
                        result.extracted_at,
                        result.relationship_count,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate":
            raise ConflictError(
                "a relationship result already exists for this content/profile"
            )
        if outcome == "unknown_content":
            raise IntegrityError("the referenced normalized content does not exist")

    async def get_result(
        self, result_id: RelationshipExtractionResultId
    ) -> RelationshipExtractionResult | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_GET_RESULT_FN, (str(result_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_relationship_extraction_result(row)

    async def get_result_by_profile(
        self,
        content_id: NormalizedContentId,
        profile_name: str,
        profile_version: str,
    ) -> RelationshipExtractionResult | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _GET_RESULT_BY_PROFILE_FN,
                    (str(content_id), profile_name, profile_version),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_relationship_extraction_result(row)

    async def create_relationship(self, relationship: ExtractedRelationship) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _CREATE_RELATIONSHIP_FN,
                    (
                        str(relationship.relationship_id),
                        str(relationship.extraction_result_id),
                        str(relationship.content_id),
                        str(relationship.source_entity_id),
                        relationship.predicate.value,
                        str(relationship.target_entity_id),
                        relationship.support_text,
                        relationship.support_span.start,
                        relationship.support_span.end,
                        relationship.extractor.name,
                        relationship.extractor.version,
                        relationship.extraction_confidence,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate":
            raise ConflictError("this relationship assertion already exists")
        if outcome == "unknown_result":
            raise IntegrityError("the referenced relationship result does not exist")
        if outcome == "content_mismatch":
            raise IntegrityError(
                "the relationship content does not match its result content"
            )
        if outcome in ("unknown_source", "unknown_target"):
            raise IntegrityError("a referenced endpoint occurrence does not exist")
        if outcome in ("source_content_mismatch", "target_content_mismatch"):
            raise IntegrityError(
                "an endpoint occurrence belongs to a different content"
            )
        if outcome == "self_edge":
            raise IntegrityError("a relationship must not be a self-edge")
        if outcome == "invalid_support":
            raise IntegrityError("the relationship support span/text is invalid")
        if outcome == "invalid_predicate":
            raise IntegrityError("the relationship predicate is not in the vocabulary")
        if outcome == "invalid_span":
            raise IntegrityError("the relationship support span is invalid")
        if outcome == "invalid_confidence":
            raise IntegrityError("the relationship confidence is invalid")

    async def list_for_result(
        self, result_id: RelationshipExtractionResultId
    ) -> tuple[ExtractedRelationship, ...]:
        return await self._list(_LIST_FOR_RESULT_FN, str(result_id))

    async def list_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[ExtractedRelationship, ...]:
        return await self._list(_LIST_FOR_CONTENT_FN, str(content_id))

    async def get_relationship(
        self, relationship_id: ExtractedRelationshipId
    ) -> ExtractedRelationship | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_GET_RELATIONSHIP_FN, (str(relationship_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_extracted_relationship(row)

    async def _list(
        self, function: str, parameter: str
    ) -> tuple[ExtractedRelationship, ...]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(function, (parameter,))
                rows: list[Any] = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_extracted_relationship(row) for row in rows)


__all__ = ["PostgresRelationshipRepository"]
