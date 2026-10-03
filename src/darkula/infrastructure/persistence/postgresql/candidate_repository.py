# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL :class:`~darkula.app.repositories.SourceCandidateRepository`.

Implements the candidate repository contract over versioned stored
functions. Consistent with the Darkula stored-function invariant, this
module contains **no data-access SQL**: every operation is one
stored-function invocation (the driver's only mechanism to call a
PostgreSQL function), and every result is mapped centrally through
:mod:`darkula.infrastructure.persistence.postgresql.mapping`.

Controlled conflicts (duplicate identity, expected-status mismatch) are
signaled by stored-function return values and surface as typed
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
from darkula.app.repositories import SourceCandidateRepository
from darkula.domain.identifiers import (
    SourceCandidateId,
)
from darkula.domain.source import (
    CandidateStatus,
    ReconAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
)
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_candidate,
    map_event,
    map_recon_assessment,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

# Stored-function invocations (name <op>_v1; versioning documented in
# migrations/0001_initial.sql). Parameter count/order matches the function
# signatures exactly.
_CREATE_FN = (
    "SELECT * FROM candidate_create_v1(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_GET_FN = "SELECT * FROM candidate_get_v1(%s)"
_TRANSITION_FN = (
    "SELECT * FROM candidate_transition_v1(%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_APPEND_HISTORY_FN = (
    "SELECT * FROM candidate_event_append_v1(%s, %s, %s, %s, %s, %s, %s)"
)
_LIST_HISTORY_FN = "SELECT * FROM candidate_history_list_v1(%s)"
_APPEND_RECON_FN = (
    "SELECT * FROM recon_assessment_append_v1(%s, %s, %s, %s, %s, %s, %s)"
)
_LIST_RECON_FN = "SELECT * FROM recon_assessment_list_v1(%s)"


def _jsonb(value: Any) -> Any:
    """Wrap a JSON-compatible value for a jsonb parameter (None stays NULL)."""
    return None if value is None else Jsonb(value)


class PostgresSourceCandidateRepository(SourceCandidateRepository):
    """Candidate repository bound to one PostgresUnitOfWork transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def create(
        self,
        candidate: SourceCandidate,
        *,
        event: SourceCandidateEventHistory,
    ) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _CREATE_FN,
                    (
                        str(candidate.candidate_id),
                        candidate.discovered_at,
                        candidate.discovery_method,
                        candidate.entrypoint,
                        candidate.status.value,
                        _jsonb(candidate.discovery_context),
                        str(event.event_id),
                        event.event_type.value,
                        event.occurred_at,
                        _jsonb(event.context),
                        event.reason,
                        event.provenance,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        if row is None or row[0] is not True:
            raise ConflictError("a candidate with this identity already exists")

    async def get(self, candidate_id: SourceCandidateId) -> SourceCandidate | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_GET_FN, (str(candidate_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_candidate(row)

    async def transition(
        self,
        candidate_id: SourceCandidateId,
        *,
        expected: CandidateStatus,
        new_status: CandidateStatus,
        event: SourceCandidateEventHistory,
    ) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _TRANSITION_FN,
                    (
                        str(candidate_id),
                        expected.value,
                        new_status.value,
                        str(event.event_id),
                        event.event_type.value,
                        event.occurred_at,
                        _jsonb(event.context),
                        event.reason,
                        event.provenance,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        if row is None or row[0] is not True:
            raise ConflictError(
                "candidate status does not equal the expected status; "
                "no transition and no event were applied"
            )

    async def append_history(self, event: SourceCandidateEventHistory) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _APPEND_HISTORY_FN,
                    (
                        str(event.event_id),
                        str(event.candidate_id),
                        event.event_type.value,
                        event.occurred_at,
                        _jsonb(event.context),
                        event.reason,
                        event.provenance,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate":
            raise ConflictError("an event with this identity already exists")
        if outcome == "unknown_candidate":
            raise IntegrityError("the referenced candidate does not exist")

    async def list_history(
        self, candidate_id: SourceCandidateId
    ) -> tuple[SourceCandidateEventHistory, ...]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_LIST_HISTORY_FN, (str(candidate_id),))
                rows = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_event(row) for row in rows)

    async def append_recon_assessment(self, assessment: ReconAssessment) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _APPEND_RECON_FN,
                    (
                        str(assessment.assessment_id),
                        str(assessment.candidate_id),
                        assessment.assessed_at,
                        assessment.disposition.value,
                        assessment.confidence.value,
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
        if outcome == "unknown_candidate":
            raise IntegrityError("the referenced candidate does not exist")

    async def list_recon_assessments(
        self, candidate_id: SourceCandidateId
    ) -> tuple[ReconAssessment, ...]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_LIST_RECON_FN, (str(candidate_id),))
                rows = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_recon_assessment(row) for row in rows)


__all__ = ["PostgresSourceCandidateRepository"]
