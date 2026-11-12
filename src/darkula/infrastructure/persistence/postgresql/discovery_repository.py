# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL discovery-observation and access-evidence repositories (PR 17).

Implements :class:`~darkula.app.repositories.DiscoveryRepository` and
:class:`~darkula.app.repositories.AccessRepository` over versioned stored
functions. Consistent with the Darkula stored-function invariant this module
contains **no data-access SQL**: every operation is one fixed, parameterized
stored-function invocation and every result row is mapped centrally through
:mod:`darkula.infrastructure.persistence.postgresql.mapping`.

Controlled outcomes (replay/conflict/claim/provenance) are signaled by
stored-function return values and surface as typed bounded errors, leaving
the enclosing transaction correctly resolvable. ``asyncio.CancelledError``
always propagates unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from darkula.app.persistence import (
    ConflictError,
    IntegrityError,
    MappingError,
    NotFoundError,
)
from darkula.app.repositories import AccessRepository, DiscoveryRepository
from darkula.domain.discovery import (
    AccessEvidence,
    AccessStatus,
    DiscoveryDetectionMethod,
    DiscoveryObservation,
    DiscoveryReferenceKind,
)
from darkula.domain.identifiers import (
    DiscoveryObservationId,
    SourceCandidateId,
    SourceEndpointId,
)
from darkula.domain.registry_polling import RegistryPollCheckpoint
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_access_evidence,
    map_discovery_observation,
    map_registry_poll_checkpoint,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

#: Maximum observation page size accepted by
#: ``discovery_observation_list_for_provider_v1``.
MAX_DISCOVERY_PAGE_SIZE = 200

_INSERT_FN = (
    "SELECT * FROM discovery_observation_insert_v1("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_GET_FN = "SELECT * FROM discovery_observation_get_v1(%s)"
_GET_BY_KEY_FN = "SELECT * FROM discovery_observation_get_by_key_v1(%s, %s, %s, %s, %s)"
_LIST_FOR_PROVIDER_FN = (
    "SELECT * FROM discovery_observation_list_for_provider_v1(%s, %s, %s)"
)
_CHECKPOINT_GET_FN = "SELECT * FROM registry_poll_checkpoint_get_v1(%s)"
_CHECKPOINT_ADVANCE_FN = (
    "SELECT * FROM registry_poll_checkpoint_advance_v1(%s, %s, %s, %s)"
)
_CHECKPOINT_DEFER_FN = "SELECT * FROM registry_poll_checkpoint_defer_v1(%s, %s, %s)"
_ACCESS_APPEND_PENDING_FN = (
    "SELECT * FROM access_evidence_append_pending_v1(%s, %s, %s, %s, %s, %s)"
)
_ACCESS_GET_PENDING_FN = "SELECT * FROM access_evidence_get_pending_v1(%s, %s)"
_ACCESS_LIST_FOR_CANDIDATE_FN = (
    "SELECT * FROM access_evidence_list_for_candidate_v1(%s)"
)
_ACCESS_LIST_FOR_ENDPOINT_FN = "SELECT * FROM access_evidence_list_for_endpoint_v1(%s)"
_ACCESS_LATEST_FOR_CANDIDATE_FN = (
    "SELECT * FROM access_evidence_latest_for_candidate_v1(%s)"
)
_ACCESS_LATEST_FOR_ENDPOINT_FN = (
    "SELECT * FROM access_evidence_latest_for_endpoint_v1(%s)"
)


class PostgresDiscoveryRepository(DiscoveryRepository):
    """Immutable discovery-observation repository bound to one transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def _fetch_one(
        self, sql: str, params: tuple[Any, ...]
    ) -> tuple[Any, ...] | None:
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
        self, sql: str, params: tuple[Any, ...]
    ) -> list[tuple[Any, ...]]:
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

    async def insert_or_get_exact(
        self, observation: DiscoveryObservation
    ) -> DiscoveryObservation:
        row = await self._fetch_one(
            _INSERT_FN,
            (
                str(observation.observation_id),
                observation.provider_id,
                observation.provider_record_id,
                observation.provider_record_version,
                observation.detection_method.value,
                observation.reference_kind.value,
                observation.asserted_reference,
                observation.normalized_locator,
                observation.validation_status.value,
                (
                    None
                    if observation.source_kind_hint is None
                    else observation.source_kind_hint.value
                ),
                observation.requires_auth_claim,
                observation.origin_reference,
                observation.evidence_reference,
                observation.evidence_excerpt,
                observation.observed_at,
                observation.ingested_at,
                (
                    None
                    if observation.candidate_id is None
                    else str(observation.candidate_id)
                ),
                observation.semantic_key,
            ),
        )
        outcome = None if row is None else row[0]
        if outcome == "conflict":
            raise ConflictError(
                "the discovery observation semantic key already exists with a "
                "different payload; no record was changed"
            )
        if outcome not in ("added", "replayed"):
            raise MappingError("unexpected discovery observation insert outcome")
        persisted = await self._fetch_one(
            _GET_BY_KEY_FN,
            (
                observation.provider_id,
                observation.provider_record_id,
                observation.provider_record_version,
                observation.detection_method.value,
                observation.reference_kind.value,
            ),
        )
        if persisted is None:
            raise MappingError("the accepted discovery observation could not be read")
        return map_discovery_observation(persisted)

    async def get(
        self, observation_id: DiscoveryObservationId
    ) -> DiscoveryObservation | None:
        row = await self._fetch_one(_GET_FN, (str(observation_id),))
        return None if row is None else map_discovery_observation(row)

    async def get_by_key(
        self,
        *,
        provider_id: str,
        provider_record_id: str,
        provider_record_version: str,
        detection_method: DiscoveryDetectionMethod,
        reference_kind: DiscoveryReferenceKind,
    ) -> DiscoveryObservation | None:
        row = await self._fetch_one(
            _GET_BY_KEY_FN,
            (
                provider_id,
                provider_record_id,
                provider_record_version,
                detection_method.value,
                reference_kind.value,
            ),
        )
        return None if row is None else map_discovery_observation(row)

    async def list_for_provider(
        self,
        provider_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[DiscoveryObservation, ...]:
        if limit < 1 or limit > MAX_DISCOVERY_PAGE_SIZE:
            raise ValueError(f"limit must be within 1..{MAX_DISCOVERY_PAGE_SIZE}")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        rows = await self._fetch_all(
            _LIST_FOR_PROVIDER_FN, (provider_id, limit, offset)
        )
        return tuple(map_discovery_observation(row) for row in rows)

    async def _read_checkpoint(self, provider_id: str) -> RegistryPollCheckpoint | None:
        row = await self._fetch_one(_CHECKPOINT_GET_FN, (provider_id,))
        return None if row is None else map_registry_poll_checkpoint(row)

    async def get_registry_checkpoint(self, provider_id: str) -> RegistryPollCheckpoint:
        checkpoint = await self._read_checkpoint(provider_id)
        if checkpoint is None:
            return RegistryPollCheckpoint.unpersisted(provider_id)
        return checkpoint

    async def advance_registry_checkpoint(
        self,
        provider_id: str,
        *,
        expected_generation: int,
        cursor: str | None,
        completed_at: datetime,
    ) -> RegistryPollCheckpoint:
        row = await self._fetch_one(
            _CHECKPOINT_ADVANCE_FN,
            (provider_id, expected_generation, cursor, completed_at),
        )
        outcome = None if row is None else row[0]
        if outcome == "conflict":
            raise ConflictError(
                "the registry poll checkpoint was advanced by another writer"
            )
        if outcome != "advanced":
            raise MappingError("unexpected registry poll checkpoint advance outcome")
        checkpoint = await self._read_checkpoint(provider_id)
        if checkpoint is None:
            raise MappingError(
                "the advanced registry poll checkpoint could not be read"
            )
        return checkpoint

    async def defer_registry_checkpoint(
        self,
        provider_id: str,
        *,
        expected_generation: int,
        not_before: datetime,
    ) -> RegistryPollCheckpoint:
        row = await self._fetch_one(
            _CHECKPOINT_DEFER_FN,
            (provider_id, expected_generation, not_before),
        )
        outcome = None if row is None else row[0]
        if outcome == "conflict":
            raise ConflictError(
                "the registry poll checkpoint was advanced by another writer"
            )
        if outcome != "deferred":
            raise MappingError("unexpected registry poll checkpoint defer outcome")
        checkpoint = await self._read_checkpoint(provider_id)
        if checkpoint is None:
            raise MappingError(
                "the deferred registry poll checkpoint could not be read"
            )
        return checkpoint


class PostgresAccessRepository(AccessRepository):
    """Immutable access-evidence repository bound to one transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def _fetch_one(
        self, sql: str, params: tuple[Any, ...]
    ) -> tuple[Any, ...] | None:
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
        self, sql: str, params: tuple[Any, ...]
    ) -> list[tuple[Any, ...]]:
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

    async def append_evidence(self, evidence: AccessEvidence) -> AccessEvidence:
        if evidence.candidate_id is None or evidence.source_endpoint_id is not None:
            raise ValueError(
                "PR17 access evidence must be candidate-linked "
                "(endpoint evidence is a later trusted flow)"
            )
        if evidence.status is not AccessStatus.CREDENTIALS_PENDING:
            raise ValueError(
                "PR17 only appends CREDENTIALS_PENDING access evidence; "
                "PUBLIC/READY/AUTH_FAILED/ACCESS_REVOKED require trusted flows"
            )
        row = await self._fetch_one(
            _ACCESS_APPEND_PENDING_FN,
            (
                str(evidence.access_evidence_id),
                str(evidence.observation_id),
                str(evidence.candidate_id),
                evidence.reason_code,
                evidence.recorded_at,
                evidence.evidence_reference,
            ),
        )
        outcome = None if row is None else row[0]
        if outcome == "unknown_observation":
            raise NotFoundError("the discovery observation does not exist")
        if outcome == "unknown_candidate":
            raise NotFoundError("the candidate does not exist")
        if outcome == "claim_absent":
            raise ConflictError(
                "the discovery observation does not claim required authentication"
            )
        if outcome == "wrong_candidate":
            raise ConflictError(
                "the discovery observation is linked to a different candidate"
            )
        if outcome == "conflict":
            raise ConflictError(
                "conflicting access evidence already exists for this "
                "observation/candidate"
            )
        if outcome not in ("added", "replayed"):
            raise MappingError("unexpected access evidence append outcome")
        persisted = await self._fetch_one(
            _ACCESS_GET_PENDING_FN,
            (str(evidence.observation_id), str(evidence.candidate_id)),
        )
        if persisted is None:
            raise IntegrityError(
                "the access evidence record could not be read after append"
            )
        return map_access_evidence(persisted)

    async def list_for_candidate(
        self, candidate_id: SourceCandidateId
    ) -> tuple[AccessEvidence, ...]:
        rows = await self._fetch_all(
            _ACCESS_LIST_FOR_CANDIDATE_FN, (str(candidate_id),)
        )
        return tuple(map_access_evidence(row) for row in rows)

    async def list_for_endpoint(
        self, endpoint_id: SourceEndpointId
    ) -> tuple[AccessEvidence, ...]:
        rows = await self._fetch_all(_ACCESS_LIST_FOR_ENDPOINT_FN, (str(endpoint_id),))
        return tuple(map_access_evidence(row) for row in rows)

    async def latest_for_candidate(
        self, candidate_id: SourceCandidateId
    ) -> AccessEvidence | None:
        row = await self._fetch_one(
            _ACCESS_LATEST_FOR_CANDIDATE_FN, (str(candidate_id),)
        )
        return None if row is None else map_access_evidence(row)

    async def latest_for_endpoint(
        self, endpoint_id: SourceEndpointId
    ) -> AccessEvidence | None:
        row = await self._fetch_one(_ACCESS_LATEST_FOR_ENDPOINT_FN, (str(endpoint_id),))
        return None if row is None else map_access_evidence(row)


__all__ = [
    "MAX_DISCOVERY_PAGE_SIZE",
    "PostgresAccessRepository",
    "PostgresDiscoveryRepository",
]
