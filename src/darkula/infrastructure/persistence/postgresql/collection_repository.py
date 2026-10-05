# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL :class:`~darkula.app.repositories.CollectionRepository`.

Implements the managed-source collection persistence contract over versioned
stored functions from ``migrations/0004_collection.sql``. Consistent with the
Darkula stored-function invariant this module contains **no data-access SQL**:
every operation is one fixed parameterized stored-function invocation and
results are mapped by
:mod:`darkula.infrastructure.persistence.postgresql.mapping`.

Controlled conflicts surface as typed
:class:`~darkula.app.persistence.ConflictError` /
:class:`~darkula.app.persistence.NotFoundError` /
:class:`~darkula.app.persistence.IntegrityError` instances and the
expected-state claim/reclaim outcomes are returned verbatim for the caller;
``asyncio.CancelledError`` propagates unchanged and the repository never
performs broker I/O.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

from psycopg.types.json import Jsonb

from darkula.app.persistence import (
    ConflictError,
    IntegrityError,
    NotFoundError,
)
from darkula.app.repositories import (
    ClaimOutcome,
    CollectionRepository,
    ReclaimOutcome,
    ScheduledOccurrence,
    ScheduleOutcome,
)
from darkula.domain.collection import (
    CollectionFailureCode,
    CollectionPolicy,
    CollectionRun,
    CollectionRunStatus,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    SourceEndpointId,
    SourceId,
)
from darkula.domain.source import SourceEndpoint
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_collection_policy,
    map_collection_run,
    map_endpoint,
    map_scheduled_occurrence,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

# Stored-function invocations (versioning documented in
# migrations/0004_collection.sql).
_POLICY_CREATE_FN = (
    "SELECT * FROM collection_policy_create_v1("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_ENDPOINT_ADD_FN = "SELECT * FROM collection_policy_endpoint_add_v1(%s, %s)"
_POLICY_GET_FN = "SELECT * FROM collection_policy_get_v1(%s)"
_POLICY_UPDATE_FN = (
    "SELECT * FROM collection_policy_update_v1("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_ENDPOINT_LIST_FN = "SELECT * FROM collection_policy_endpoint_list_v1(%s)"
_ENDPOINT_GET_FN = "SELECT * FROM collection_endpoint_get_v1(%s)"
_RUN_CREATE_FN = "SELECT * FROM collection_run_create_v1(%s, %s, %s, %s, %s, %s, %s)"
_RUN_GET_FN = "SELECT * FROM collection_run_get_v1(%s)"
_RUN_LIST_FOR_SOURCE_WINDOW_FN = (
    "SELECT * FROM collection_run_list_for_source_window_v1(%s, %s, %s)"
)
_SCHEDULE_DUE_FN = "SELECT * FROM collection_schedule_due_v1(%s, %s, %s)"
_RUN_CLAIM_FN = "SELECT * FROM collection_run_claim_v1(%s, %s, %s, %s)"
_RUN_RECLAIM_FN = "SELECT * FROM collection_run_reclaim_v1(%s, %s, %s, %s, %s)"
_RUN_COMPLETE_FN = (
    "SELECT * FROM collection_run_complete_v1(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_RUN_CANCEL_FN = "SELECT * FROM collection_run_cancel_v1(%s, %s)"


def _jsonb(value: object) -> object:
    """Wrap a JSON-compatible value for a jsonb parameter (None stays NULL)."""
    return None if value is None else Jsonb(value)


def _uuid_list(value: tuple[SourceEndpointId, ...]) -> list[UUID] | None:
    """Adapt an endpoint identity tuple for a uuid[] parameter."""
    if not value:
        return None
    return [UUID(str(endpoint_id)) for endpoint_id in value]


def _str_uuid(
    value: SourceEndpointId | CollectionPolicyId | CollectionRunId | SourceId,
) -> str:
    """Return the canonical UUID string of one identity value."""
    return str(value)


class PostgresCollectionRepository(CollectionRepository):
    """Collection repository bound to one PostgresUnitOfWork transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def create_policy(self, policy: CollectionPolicy) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _POLICY_CREATE_FN,
                    (
                        _str_uuid(policy.policy_id),
                        _str_uuid(policy.source_id),
                        policy.active,
                        policy.created_at,
                        policy.updated_at,
                        policy.revision,
                        policy.interval_seconds,
                        policy.next_due_at,
                        _jsonb(list(policy.allowed_paths)),
                        policy.max_pages,
                        policy.max_requests,
                        policy.max_depth,
                        policy.timeout_seconds,
                        policy.authentication_reference,
                    ),
                )
                row = await cur.fetchone()
                policy_outcome = "missing" if row is None else str(row[0])
                endpoint_outcomes: list[str] = []
                for endpoint_id in policy.allowed_endpoint_ids:
                    await cur.execute(
                        _ENDPOINT_ADD_FN,
                        (_str_uuid(policy.policy_id), _str_uuid(endpoint_id)),
                    )
                    endpoint_row = await cur.fetchone()
                    endpoint_outcomes.append(
                        "missing" if endpoint_row is None else str(endpoint_row[0])
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        if policy_outcome == "duplicate":
            raise ConflictError("a policy with this identity already exists")
        if policy_outcome == "unknown_source":
            raise IntegrityError("the referenced source does not exist")
        if policy_outcome != "added":
            raise ConflictError("the policy was not created")
        if "unknown_policy" in endpoint_outcomes:
            raise IntegrityError("the referenced policy does not exist")
        if "duplicate" in endpoint_outcomes:
            raise ConflictError("a policy endpoint authorization already exists")
        if "unknown_endpoint" in endpoint_outcomes:
            raise IntegrityError("an authorized endpoint does not exist")
        if "endpoint_not_owned" in endpoint_outcomes:
            raise IntegrityError("an authorized endpoint belongs to another source")

    async def get_policy(
        self, policy_id: CollectionPolicyId
    ) -> CollectionPolicy | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_POLICY_GET_FN, (_str_uuid(policy_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_collection_policy(row)

    async def update_policy(self, policy: CollectionPolicy) -> int:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _POLICY_UPDATE_FN,
                    (
                        _str_uuid(policy.policy_id),
                        policy.active,
                        policy.updated_at,
                        policy.interval_seconds,
                        policy.next_due_at,
                        _jsonb(list(policy.allowed_paths)),
                        policy.max_pages,
                        policy.max_requests,
                        policy.max_depth,
                        policy.timeout_seconds,
                        policy.authentication_reference,
                        _uuid_list(policy.allowed_endpoint_ids),
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = "missing" if row is None else str(row[0])
        if outcome == "unknown_policy":
            raise NotFoundError("the policy does not exist")
        if outcome == "unknown_endpoint":
            raise IntegrityError("an authorized endpoint does not exist")
        if outcome == "endpoint_not_owned":
            raise IntegrityError("an authorized endpoint belongs to another source")
        if outcome != "updated":
            raise ConflictError("the policy update did not apply")
        return policy.revision + 1

    async def list_policy_endpoints(
        self, policy_id: CollectionPolicyId
    ) -> tuple[SourceEndpoint, ...]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_ENDPOINT_LIST_FN, (_str_uuid(policy_id),))
                rows = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_endpoint(row) for row in rows)

    async def get_endpoint(
        self, endpoint_id: SourceEndpointId
    ) -> SourceEndpoint | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_ENDPOINT_GET_FN, (_str_uuid(endpoint_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_endpoint(row)

    async def create_run(self, run: CollectionRun) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _RUN_CREATE_FN,
                    (
                        _str_uuid(run.run_id),
                        _str_uuid(run.policy_id),
                        run.policy_revision,
                        _jsonb(dict(run.policy_snapshot or {})),
                        _str_uuid(run.source_id),
                        run.scheduled_for,
                        run.created_at,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = "missing" if row is None else str(row[0])
        if outcome == "unknown_policy":
            raise IntegrityError("the referenced policy does not exist")
        if outcome == "duplicate":
            raise ConflictError("a run with this identity already exists")
        if outcome == "occurrence_exists":
            raise ConflictError("a run for this policy occurrence already exists")
        if outcome != "added":
            raise ConflictError("the run was not created")

    async def get_run(self, run_id: CollectionRunId) -> CollectionRun | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_RUN_GET_FN, (_str_uuid(run_id),))
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return None if row is None else map_collection_run(row)

    async def list_runs_for_source_window(
        self,
        source_id: SourceId,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[CollectionRun, ...]:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _RUN_LIST_FOR_SOURCE_WINDOW_FN,
                    (_str_uuid(source_id), window_start, window_end),
                )
                rows = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_collection_run(row) for row in rows)

    async def schedule_due(
        self,
        *,
        now: datetime,
        run_id: CollectionRunId,
        created_at: datetime,
    ) -> ScheduledOccurrence | None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _SCHEDULE_DUE_FN,
                    (now, _str_uuid(run_id), created_at),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        if row is None:
            return None
        # The 'none' outcome carries NULL identity columns; only map when a
        # run was actually admitted.
        if row[6] == "none":
            return None
        occurrence = map_scheduled_occurrence(row)
        if occurrence.outcome is ScheduleOutcome.SCHEDULED or (
            occurrence.outcome is ScheduleOutcome.OCCURRENCE_EXISTS
        ):
            return occurrence
        return None

    async def claim_run(
        self,
        *,
        run_id: CollectionRunId,
        execution_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
    ) -> ClaimOutcome:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _RUN_CLAIM_FN,
                    (
                        _str_uuid(run_id),
                        UUID(execution_id),
                        started_at,
                        lease_expires_at,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = "missing" if row is None else str(row[0])
        return ClaimOutcome(outcome)

    async def reclaim_run(
        self,
        *,
        run_id: CollectionRunId,
        execution_id: str,
        now: datetime,
        lease_expires_at: datetime,
        max_attempts: int,
    ) -> ReclaimOutcome:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _RUN_RECLAIM_FN,
                    (
                        _str_uuid(run_id),
                        UUID(execution_id),
                        now,
                        lease_expires_at,
                        max_attempts,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = "missing" if row is None else str(row[0])
        return ReclaimOutcome(outcome)

    async def complete_run(
        self,
        *,
        run_id: CollectionRunId,
        new_status: CollectionRunStatus,
        completed_at: datetime,
        crawl_requests_attempted: int,
        pages_observed: int,
        content_observations: int,
        content_created: int,
        content_deduplicated: int,
        failure_code: CollectionFailureCode | None,
        failure_summary: str | None,
    ) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _RUN_COMPLETE_FN,
                    (
                        _str_uuid(run_id),
                        new_status.value,
                        completed_at,
                        crawl_requests_attempted,
                        pages_observed,
                        content_observations,
                        content_created,
                        content_deduplicated,
                        None if failure_code is None else failure_code.value,
                        failure_summary,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = "missing" if row is None else str(row[0])
        if outcome == "unknown_run":
            raise NotFoundError("the run does not exist")
        if outcome == "wrong_state":
            raise ConflictError("the run is not in the expected executing state")

    async def cancel_run(
        self,
        *,
        run_id: CollectionRunId,
        completed_at: datetime,
    ) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _RUN_CANCEL_FN,
                    (_str_uuid(run_id), completed_at),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = "missing" if row is None else str(row[0])
        if outcome == "unknown_run":
            raise NotFoundError("the run does not exist")
        if outcome == "wrong_state":
            raise ConflictError("the run is already terminal")


__all__ = ["PostgresCollectionRepository"]
