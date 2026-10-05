# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL :class:`~darkula.app.repositories.ExtractionRepository`.

Implements the deterministic extraction persistence contract over versioned
stored functions. Consistent with the Darkula stored-function-only invariant,
this module contains **no data-access SQL**: every operation is one
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
from darkula.app.repositories import ExtractionRepository
from darkula.domain.extraction import ExtractedEntity, ExtractionResult
from darkula.domain.identifiers import ExtractionResultId, NormalizedContentId
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_extracted_entity,
    map_extraction_result,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

# Stored-function invocations (versioned in migrations/0005_extraction.sql).
_CREATE_RESULT_FN = (
    "SELECT * FROM extraction_result_create_v1(%s, %s, %s, %s, %s, %s, %s)"
)
_GET_RESULT_FN = "SELECT * FROM extraction_result_get_v1(%s)"
_GET_RESULT_BY_PROFILE_FN = (
    "SELECT * FROM extraction_result_get_by_profile_v1(%s, %s, %s)"
)
_CREATE_ENTITY_FN = (
    "SELECT * FROM extracted_entity_create_v1("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_LIST_ENTITIES_FOR_RESULT_FN = "SELECT * FROM extracted_entity_list_for_result_v1(%s)"
_LIST_ENTITIES_FOR_CONTENT_FN = "SELECT * FROM extracted_entity_list_for_content_v1(%s)"


def _manifest_json(result: ExtractionResult) -> list[dict[str, str]]:
    """Serialize the exact extractor manifest for the jsonb parameter."""
    return [
        {"name": entry.name, "version": entry.version}
        for entry in result.extractor_manifest
    ]


class PostgresExtractionRepository(ExtractionRepository):
    """Extraction repository bound to one PostgresUnitOfWork transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def create_result(self, result: ExtractionResult) -> None:
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
                        result.entity_count,
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
                "an extraction result already exists for this content/profile"
            )
        if outcome == "unknown_content":
            raise IntegrityError("the referenced normalized content does not exist")

    async def get_result(
        self, result_id: ExtractionResultId
    ) -> ExtractionResult | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_GET_RESULT_FN, (str(result_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_extraction_result(row)

    async def get_result_by_profile(
        self,
        content_id: NormalizedContentId,
        profile_name: str,
        profile_version: str,
    ) -> ExtractionResult | None:
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
        return None if row is None else map_extraction_result(row)

    async def create_entity(self, entity: ExtractedEntity) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _CREATE_ENTITY_FN,
                    (
                        str(entity.entity_id),
                        str(entity.extraction_result_id),
                        str(entity.content_id),
                        entity.entity_type.value,
                        entity.raw_value,
                        entity.normalized_value,
                        entity.source_span.start,
                        entity.source_span.end,
                        entity.extractor.name,
                        entity.extractor.version,
                        entity.subtype,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate":
            raise ConflictError("this extracted-entity occurrence already exists")
        if outcome == "unknown_result":
            raise IntegrityError("the referenced extraction result does not exist")
        if outcome == "content_mismatch":
            raise IntegrityError(
                "the extracted entity content does not match its result content"
            )

    async def list_entities_for_result(
        self, result_id: ExtractionResultId
    ) -> tuple[ExtractedEntity, ...]:
        return await self._list(_LIST_ENTITIES_FOR_RESULT_FN, str(result_id))

    async def list_entities_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[ExtractedEntity, ...]:
        return await self._list(_LIST_ENTITIES_FOR_CONTENT_FN, str(content_id))

    async def _list(self, function: str, parameter: str) -> tuple[ExtractedEntity, ...]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(function, (parameter,))
                rows: list[Any] = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_extracted_entity(row) for row in rows)


__all__ = ["PostgresExtractionRepository"]
