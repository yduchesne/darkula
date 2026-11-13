# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL candidate admission repository (PR 19).

Implements :class:`~darkula.app.repositories.AdmissionRepository` over the
versioned stored functions added by ``migrations/0011_candidate_admission.sql``.

This module contains no data-access SQL: every operation is one fixed,
parameterized stored-function invocation and every row is mapped centrally
through :mod:`darkula.infrastructure.persistence.postgresql.mapping`.

The admission ledger is the only durable record written here. Admission
never fetches, crawls, resolves DNS, calls an LLM, promotes a Source,
creates an endpoint, or grants collection/access authority. A locator digest
is a lookup key only; equal URLs never establish logical Source equivalence.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from darkula.app.persistence import (
    AdmissionInvariantError,
    ConflictError,
    MappingError,
    NotFoundError,
)
from darkula.app.repositories import AdmissionRepository
from darkula.domain.candidate_admission import (
    MAX_POSSIBLE_ALIASES,
    AdmissionOutcome,
    AdmissionResult,
    AliasSuggestionsResult,
)
from darkula.domain.discovery import DiscoveryObservation
from darkula.domain.identifiers import (
    CandidateEventId,
    DiscoveryObservationId,
    SourceCandidateId,
    SourceEndpointId,
)
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_admission_result,
    map_discovery_observation,
    map_possible_alias,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

_PENDING_FN = "SELECT * FROM discovery_admission_pending_v1(%s, %s)"
_GET_FN = "SELECT * FROM discovery_admission_get_v1(%s)"
_ENDPOINTS_FN = "SELECT * FROM discovery_admission_endpoints_v1(%s)"
_CLAIMS_FN = "SELECT * FROM discovery_admission_claims_for_candidate_v1(%s, %s)"
_ALIASES_FN = "SELECT * FROM discovery_possible_aliases_v1(%s, %s)"
_ADMIT_FN = "SELECT * FROM discovery_admit_observation_v1(%s, %s, %s, %s)"
_VERIFY_FN = "SELECT * FROM trusted_registry_observation_record_v1(%s, %s)"
_VERIFIED_IDS_FN = "SELECT * FROM trusted_registry_candidate_ids_v1(%s, %s)"
_CURRENT_ENDPOINT_FN = "SELECT * FROM candidate_current_endpoint_match_v1(%s)"

_MAX_PENDING_LIMIT = 50
_MAX_CLAIMS_LIMIT = 65


class PostgresAdmissionRepository(AdmissionRepository):
    """Candidate admission repository bound to one transaction."""

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

    async def _endpoint_ids(
        self, observation_id: DiscoveryObservationId
    ) -> tuple[SourceEndpointId, ...]:
        rows = await self._fetch_all(_ENDPOINTS_FN, (str(observation_id),))
        return tuple(SourceEndpointId.from_str(str(row[0])) for row in rows)

    async def pending_observation_ids(
        self, provider_id: str, *, limit: int = 25
    ) -> tuple[DiscoveryObservationId, ...]:
        if limit < 1 or limit > _MAX_PENDING_LIMIT:
            raise ValueError(f"limit must be within 1..{_MAX_PENDING_LIMIT}")
        rows = await self._fetch_all(_PENDING_FN, (provider_id, limit))
        return tuple(DiscoveryObservationId.from_str(str(row[0])) for row in rows)

    async def admit(
        self,
        observation_id: DiscoveryObservationId,
        candidate_id: SourceCandidateId,
        event_id: CandidateEventId,
        *,
        processed_at: datetime,
    ) -> AdmissionResult:
        row = await self._fetch_one(
            _ADMIT_FN,
            (str(observation_id), str(candidate_id), str(event_id), processed_at),
        )
        if row is None:
            raise MappingError("the admission function returned no result")
        error_code = row[4]
        if error_code == "UNKNOWN_OBSERVATION":
            raise NotFoundError("the discovery observation does not exist")
        if error_code == "HASH_COLLISION":
            raise AdmissionInvariantError(
                "the locator digest collides with a different normalized "
                "locator; no admission was written"
            )
        if error_code == "UNEXPECTED_STATE":
            raise AdmissionInvariantError(
                "the persisted discovery classification is inconsistent; "
                "no admission was written"
            )
        if error_code == "CREATION_CONFLICT":
            raise ConflictError(
                "a concurrent admission used this candidate or event identity; "
                "no admission was written"
            )
        if error_code != "NONE":
            raise MappingError("unexpected admission error code")
        outcome, reason, admitted_candidate = row[0], row[1], row[2]
        if outcome is None or reason is None:
            raise MappingError("the admission result is incomplete")
        endpoint_ids: tuple[SourceEndpointId, ...] = ()
        if outcome == AdmissionOutcome.KNOWN_ENDPOINT.value:
            endpoint_ids = await self._endpoint_ids(observation_id)
        return map_admission_result(
            (observation_id, outcome, reason, admitted_candidate),
            endpoint_ids=endpoint_ids,
            replayed=bool(row[3]),
        )

    async def get(
        self, observation_id: DiscoveryObservationId
    ) -> AdmissionResult | None:
        row = await self._fetch_one(_GET_FN, (str(observation_id),))
        if row is None:
            return None
        endpoint_ids: tuple[SourceEndpointId, ...] = ()
        if row[1] == AdmissionOutcome.KNOWN_ENDPOINT.value:
            endpoint_ids = await self._endpoint_ids(observation_id)
        return map_admission_result(
            (row[0], row[1], row[2], row[3]),
            endpoint_ids=endpoint_ids,
            replayed=False,
        )

    async def claims_for_candidate(
        self, candidate_id: SourceCandidateId, *, limit: int = 65
    ) -> tuple[DiscoveryObservation, ...]:
        if limit < 1 or limit > _MAX_CLAIMS_LIMIT:
            raise ValueError(f"limit must be within 1..{_MAX_CLAIMS_LIMIT}")
        rows = await self._fetch_all(_CLAIMS_FN, (str(candidate_id), limit))
        # Reorder the admission-claim projection into the canonical
        # ``discovery_observation_get_v1`` column order for centralized
        # mapping. The stored function returns the documented admission
        # subset first, then the remaining immutable observation columns.
        return tuple(
            map_discovery_observation(
                (
                    row[0],
                    row[1],
                    row[2],
                    row[11],
                    row[5],
                    row[6],
                    row[12],
                    row[4],
                    row[7],
                    row[8],
                    row[9],
                    row[3],
                    row[13],
                    row[14],
                    row[15],
                    row[10],
                    row[16],
                    row[17],
                )
            )
            for row in rows
        )

    async def record_verified_registry_observation(
        self, observation_id: DiscoveryObservationId, *, verified_at: datetime
    ) -> None:
        row = await self._fetch_one(_VERIFY_FN, (str(observation_id), verified_at))
        outcome = None if row is None else row[0]
        if outcome == "unknown_observation":
            raise NotFoundError("the discovery observation does not exist")
        if outcome == "invalid_provenance":
            raise AdmissionInvariantError(
                "the stored observation is not a RansomLook REGISTRY claim; "
                "no verified marker was written"
            )
        if outcome not in ("added", "replayed"):
            raise MappingError("unexpected verified-ingestion outcome")

    async def verified_registry_observation_ids_for_candidate(
        self, candidate_id: SourceCandidateId, *, limit: int = 65
    ) -> tuple[DiscoveryObservationId, ...]:
        if limit < 1 or limit > _MAX_CLAIMS_LIMIT:
            raise ValueError(f"limit must be within 1..{_MAX_CLAIMS_LIMIT}")
        rows = await self._fetch_all(_VERIFIED_IDS_FN, (str(candidate_id), limit))
        return tuple(DiscoveryObservationId.from_str(str(row[0])) for row in rows)

    async def current_endpoint_match(
        self, candidate_id: SourceCandidateId
    ) -> tuple[bool, bool]:
        row = await self._fetch_one(_CURRENT_ENDPOINT_FN, (str(candidate_id),))
        if row is None or len(row) != 2:
            raise MappingError("the current endpoint match result is malformed")
        slot_found, endpoint_present = row
        if not isinstance(slot_found, bool) or not isinstance(endpoint_present, bool):
            raise MappingError("the current endpoint match result is not boolean")
        return slot_found, endpoint_present

    async def possible_aliases(
        self, candidate_id: SourceCandidateId, *, limit: int = 16
    ) -> AliasSuggestionsResult:
        if limit < 1 or limit > MAX_POSSIBLE_ALIASES:
            raise ValueError(f"limit must be within 1..{MAX_POSSIBLE_ALIASES}")
        rows = await self._fetch_all(_ALIASES_FN, (str(candidate_id), limit))
        if len(rows) > limit:
            return AliasSuggestionsResult(items=(), too_many=True)
        return AliasSuggestionsResult(
            items=tuple(
                map_possible_alias(row, left_candidate_id=candidate_id) for row in rows
            ),
            too_many=False,
        )


__all__ = ["PostgresAdmissionRepository"]
