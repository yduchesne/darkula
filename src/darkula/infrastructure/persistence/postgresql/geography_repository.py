# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL :class:`~darkula.app.repositories.GeographicResolutionRepository`.

Implements the immutable/versioned geographic-resolution contract over
versioned stored functions. Consistent with the Darkula stored-function-only
invariant, this module contains **no data-access SQL**: every operation is one
stored-function invocation and every result is mapped centrally.
"""

from __future__ import annotations

import asyncio
from typing import Any

from darkula.app.persistence import ConflictError, IntegrityError
from darkula.app.repositories import GeographicResolutionRepository
from darkula.domain.geography import GeographicResolution
from darkula.domain.identifiers import (
    ExtractedEntityId,
    GeographicResolutionId,
    NormalizedContentId,
)
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_geographic_resolution,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

# Stored-function invocations (versioned in migrations/0006_semantic_geography.sql).
_CREATE_FN = (
    "SELECT * FROM geographic_resolution_create_v1("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_GET_FN = "SELECT * FROM geographic_resolution_get_v1(%s)"
_GET_FOR_ENTITY_FN = "SELECT * FROM geographic_resolution_get_for_entity_v1(%s, %s, %s)"
_LIST_FOR_CONTENT_FN = "SELECT * FROM geographic_resolution_list_for_content_v1(%s)"


class PostgresGeographicResolutionRepository(GeographicResolutionRepository):
    """Geographic-resolution repository bound to one unit-of-work transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def create(self, resolution: GeographicResolution) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _CREATE_FN,
                    (
                        str(resolution.resolution_id),
                        str(resolution.extracted_entity_id),
                        resolution.status.value,
                        resolution.resolver.name,
                        resolution.resolver.version,
                        resolution.resolved_at,
                        resolution.canonical_name,
                        resolution.country_code,
                        resolution.administrative_area,
                        resolution.locality,
                        resolution.latitude,
                        resolution.longitude,
                        resolution.confidence,
                        resolution.resolver_reference,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate":
            raise ConflictError("a geographic resolution already exists")
        if outcome == "unknown_entity":
            raise IntegrityError("the referenced extracted entity does not exist")

    async def get(
        self, resolution_id: GeographicResolutionId
    ) -> GeographicResolution | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_GET_FN, (str(resolution_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_geographic_resolution(row)

    async def get_for_entity(
        self,
        entity_id: ExtractedEntityId,
        resolver_name: str,
        resolver_version: str,
    ) -> GeographicResolution | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _GET_FOR_ENTITY_FN,
                    (str(entity_id), resolver_name, resolver_version),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_geographic_resolution(row)

    async def list_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[GeographicResolution, ...]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_LIST_FOR_CONTENT_FN, (str(content_id),))
                rows: list[Any] = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_geographic_resolution(row) for row in rows)


__all__ = ["PostgresGeographicResolutionRepository"]
