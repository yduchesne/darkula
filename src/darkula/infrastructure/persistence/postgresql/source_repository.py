# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL :class:`~darkula.app.repositories.SourceRepository`.

Implements the source repository contract over versioned stored functions.
Consistent with the Darkula stored-function invariant, this module contains
**no data-access SQL**: every operation is one stored-function invocation
and every result is mapped centrally through
:mod:`darkula.infrastructure.persistence.postgresql.mapping`.

Controlled conflicts (duplicate identity, missing owner) are signaled by
stored-function return values and surface as typed
:class:`~darkula.app.persistence.ConflictError` /
:class:`~darkula.app.persistence.IntegrityError` instances, leaving the
enclosing transaction correctly resolvable. ``asyncio.CancelledError``
propagates unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

from psycopg.types.json import Jsonb

from darkula.app.persistence import ConflictError, IntegrityError
from darkula.app.repositories import SourceRepository
from darkula.domain.identifiers import SourceId
from darkula.domain.source import Source, SourceAssessment, SourceEndpoint
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_endpoint,
    map_source,
    map_source_assessment,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

# Stored-function invocations (versioning documented in
# migrations/0001_initial.sql).
_CREATE_FN = "SELECT * FROM source_create_v1(%s, %s, %s, %s, %s)"
_GET_FN = "SELECT * FROM source_get_v1(%s)"
_ADD_ENDPOINT_FN = (
    "SELECT * FROM source_endpoint_add_v1(%s, %s, %s, %s, %s, %s, %s, %s)"
)
_LIST_ENDPOINTS_FN = "SELECT * FROM source_endpoint_list_v1(%s)"
_APPEND_ASSESSMENT_FN = (
    "SELECT * FROM source_assessment_append_v1("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_LIST_ASSESSMENTS_FN = "SELECT * FROM source_assessment_list_v1(%s)"


def _jsonb(value: Any) -> Any:
    """Wrap a JSON-compatible value for a jsonb parameter (None stays NULL)."""
    return None if value is None else Jsonb(value)


class PostgresSourceRepository(SourceRepository):
    """Source repository bound to one PostgresUnitOfWork transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def create(self, source: Source) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _CREATE_FN,
                    (
                        str(source.source_id),
                        source.status.value,
                        source.created_at,
                        source.updated_at,
                        source.name,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        if row is None or row[0] is not True:
            raise ConflictError("a source with this identity already exists")

    async def get(self, source_id: SourceId) -> Source | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_GET_FN, (str(source_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_source(row)

    async def add_endpoint(self, endpoint: SourceEndpoint) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _ADD_ENDPOINT_FN,
                    (
                        str(endpoint.endpoint_id),
                        str(endpoint.source_id),
                        endpoint.uri,
                        endpoint.endpoint_type.value,
                        endpoint.status.value,
                        endpoint.first_observed_at,
                        endpoint.last_observed_at,
                        _jsonb(endpoint.metadata),
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate":
            raise ConflictError("an endpoint with this identity already exists")
        if outcome == "unknown_source":
            raise IntegrityError("the referenced source does not exist")

    async def list_endpoints(self, source_id: SourceId) -> tuple[SourceEndpoint, ...]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_LIST_ENDPOINTS_FN, (str(source_id),))
                rows = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_endpoint(row) for row in rows)

    async def append_source_assessment(self, assessment: SourceAssessment) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _APPEND_ASSESSMENT_FN,
                    (
                        str(assessment.assessment_id),
                        str(assessment.source_id),
                        assessment.assessed_at,
                        assessment.window_start,
                        assessment.window_end,
                        assessment.confidence.value,
                        (
                            None
                            if assessment.relevance is None
                            else assessment.relevance.value
                        ),
                        (
                            None
                            if assessment.activity is None
                            else assessment.activity.value
                        ),
                        (
                            None
                            if assessment.novelty is None
                            else assessment.novelty.value
                        ),
                        _jsonb(list(assessment.evidence_references)),
                        _jsonb(assessment.characteristics),
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate":
            raise ConflictError("an assessment with this identity already exists")
        if outcome == "unknown_source":
            raise IntegrityError("the referenced source does not exist")

    async def list_source_assessments(
        self, source_id: SourceId
    ) -> tuple[SourceAssessment, ...]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_LIST_ASSESSMENTS_FN, (str(source_id),))
                rows = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_source_assessment(row) for row in rows)


__all__ = ["PostgresSourceRepository"]
