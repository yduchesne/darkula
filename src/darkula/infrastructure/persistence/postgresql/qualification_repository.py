# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL qualification-decision repository (PR 17).

Implements :class:`~darkula.app.repositories.QualificationRepository` over
one versioned stored function that appends the immutable decision, applies
the expected-state candidate transition, and appends exactly one existing
candidate event **in the same database transaction**.

This is a persistence primitive only. No PR17 service decides qualification,
evaluates policy trust, or routes reconnaissance; PR 19 owns those decisions.
The module contains no data-access SQL: every operation is one fixed,
parameterized stored-function invocation and every row is mapped centrally.
"""

from __future__ import annotations

import asyncio

from psycopg.types.json import Jsonb

from darkula.app.persistence import (
    ConflictError,
    IntegrityError,
    MappingError,
    NotFoundError,
)
from darkula.app.repositories import QualificationRepository
from darkula.domain.discovery import (
    QualificationDecision,
    qualification_event_type,
)
from darkula.domain.identifiers import (
    QualificationDecisionId,
    SourceCandidateId,
)
from darkula.domain.source import SourceCandidateEventHistory
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_qualification_decision,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

_DECIDE_FN = (
    "SELECT * FROM qualification_decide_and_transition_v1("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_GET_FN = "SELECT * FROM qualification_decision_get_v1(%s)"
_GET_BY_KEY_FN = "SELECT * FROM qualification_decision_get_by_key_v1(%s)"
_LIST_FOR_CANDIDATE_FN = (
    "SELECT * FROM qualification_decision_list_for_candidate_v1(%s)"
)


def _jsonb(value: object) -> object:
    """Wrap a JSON-compatible value for a jsonb parameter (None stays NULL)."""
    return None if value is None else Jsonb(value)


def _validate_event(
    decision: QualificationDecision, event: SourceCandidateEventHistory
) -> None:
    """Reject an event that cannot belong to this decision."""
    if event.candidate_id != decision.candidate_id:
        raise ValueError("the qualification event belongs to another candidate")
    expected_event_type = qualification_event_type(decision.outcome)
    if event.event_type is not expected_event_type:
        raise ValueError(
            "the qualification event type does not match the decision outcome"
        )
    if event.event_id != decision.event_id:
        raise ValueError("qualification event_id does not match decision.event_id")


class PostgresQualificationRepository(QualificationRepository):
    """Immutable qualification-decision repository bound to one transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def _fetch_one(
        self, sql: str, params: tuple[object, ...]
    ) -> tuple[object, ...] | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else tuple(row)

    async def _fetch_all(
        self, sql: str, params: tuple[object, ...]
    ) -> list[tuple[object, ...]]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return [tuple(row) for row in rows]

    async def decide_and_transition(
        self,
        decision: QualificationDecision,
        event: SourceCandidateEventHistory,
    ) -> QualificationDecision:
        _validate_event(decision, event)
        row = await self._fetch_one(
            _DECIDE_FN,
            (
                str(decision.decision_id),
                str(decision.candidate_id),
                decision.method.value,
                decision.outcome.value,
                decision.evaluated_at,
                decision.policy_id,
                decision.policy_version,
                decision.provider_id,
                decision.provider_record_id,
                (
                    None
                    if decision.recon_assessment_id is None
                    else str(decision.recon_assessment_id)
                ),
                str(decision.event_id),
                decision.expected_status.value,
                decision.new_status.value,
                decision.decision_key,
                [str(item) for item in decision.evidence_observation_ids],
                event.event_type.value,
                event.occurred_at,
                _jsonb(event.context),
                event.reason,
                event.provenance,
            ),
        )
        outcome = None if row is None else row[0]
        if outcome == "conflict":
            raise ConflictError(
                "the qualification decision key already exists with a "
                "different payload or event identity; nothing was changed"
            )
        if outcome == "state_conflict":
            raise ConflictError(
                "the candidate status does not equal the expected status; "
                "no decision, transition, or event was applied"
            )
        if outcome == "unknown_candidate":
            raise NotFoundError("the candidate does not exist")
        if outcome == "unknown_evidence":
            raise IntegrityError("a referenced discovery observation does not exist")
        if outcome == "invalid_provenance":
            raise IntegrityError(
                "the qualification provenance is invalid (method, recon "
                "assessment, provider record, or event type)"
            )
        if outcome not in ("decided", "replayed"):
            raise MappingError("unexpected qualification decision outcome")
        persisted = await self._fetch_one(_GET_BY_KEY_FN, (decision.decision_key,))
        if persisted is None:
            raise MappingError("the qualification decision could not be read")
        return map_qualification_decision(persisted)

    async def get(
        self, decision_id: QualificationDecisionId
    ) -> QualificationDecision | None:
        row = await self._fetch_one(_GET_FN, (str(decision_id),))
        return None if row is None else map_qualification_decision(row)

    async def get_by_key(self, decision_key: str) -> QualificationDecision | None:
        row = await self._fetch_one(_GET_BY_KEY_FN, (decision_key,))
        return None if row is None else map_qualification_decision(row)

    async def list_for_candidate(
        self, candidate_id: SourceCandidateId
    ) -> tuple[QualificationDecision, ...]:
        rows = await self._fetch_all(_LIST_FOR_CANDIDATE_FN, (str(candidate_id),))
        return tuple(map_qualification_decision(row) for row in rows)


__all__ = ["PostgresQualificationRepository"]
