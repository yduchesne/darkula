# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded candidate admission service (PR 19).

``CandidateAdmissionService`` is the explicit, trusted post-ingest step that
reconciles one already persisted immutable discovery observation against
existing state:

    immutable DiscoveryObservation
        -> bounded admission batch/one-shot call
        -> durable admission ledger outcome

It never fetches, crawls, resolves DNS, calls an LLM, promotes a Source,
creates an endpoint, or grants collection/access authority. An admission
attempt is transactional: the ledger row, candidate + ``DISCOVERED`` event
(when required), locator slot and optional ``CREDENTIALS_PENDING`` evidence
commit or roll back together. A locator digest is a lookup key only, so
equal URLs never establish logical Source equivalence.

Qualification is a separate policy-controlled call
(:mod:`darkula.app.candidate_qualification`); this service never invokes an
agent, crawler, or model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from darkula.app.persistence import (
    AdmissionInvariantError,
    ConflictError,
    DarkulaSpi,
    NotFoundError,
    PersistenceError,
)
from darkula.app.source_discovery import PROVIDER_AUTH_CLAIM_REASON
from darkula.domain.candidate_admission import (
    AdmissionOutcome,
    AdmissionResult,
    AliasSuggestionsResult,
)
from darkula.domain.discovery import AccessEvidence, AccessStatus
from darkula.domain.identifiers import (
    AccessEvidenceId,
    CandidateEventId,
    DiscoveryObservationId,
    SourceCandidateId,
)

#: Bounded maximum observations processed in one admission batch call.
MAX_ADMISSION_BATCH = 50


class AdmissionRaceError(PersistenceError):
    """A bounded admission race survived the single permitted retry.

    No fabricated success is reported and nothing is left half-written by
    this service; a trusted caller may retry later in a fresh unit of work.
    """


@dataclass(frozen=True, slots=True)
class AdmissionBatchResult:
    """Bounded counts for one admission batch (no URLs or provider values).

    ``observation_ids`` is populated only for internal/deterministic tests;
    production callers must not log it.
    """

    processed: int
    admitted_new: int
    admitted_existing: int
    known_endpoint: int
    unresolved_name: int
    review_required: int
    stopped_by_error: bool = False
    observation_ids: tuple[DiscoveryObservationId, ...] = ()

    def __post_init__(self) -> None:
        counts = (
            self.processed,
            self.admitted_new,
            self.admitted_existing,
            self.known_endpoint,
            self.unresolved_name,
            self.review_required,
        )
        if any(value < 0 for value in counts):
            raise ValueError("admission batch counts must be non-negative")
        if not isinstance(self.stopped_by_error, bool):
            raise ValueError("stopped_by_error must be a bool")
        if (
            self.admitted_new
            + self.admitted_existing
            + self.known_endpoint
            + self.unresolved_name
            + self.review_required
            != self.processed
        ):
            raise ValueError("admission batch outcome counts must total processed")
        if not isinstance(self.observation_ids, tuple):
            raise ValueError("observation_ids must be a tuple")


class CandidateAdmissionService:
    """Deterministic one-observation-at-a-time candidate admission (PR 19).

    ``clock`` supplies Darkula processing time and must return a
    timezone-aware UTC ``datetime``.
    """

    def __init__(
        self,
        *,
        persistence: DarkulaSpi,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._persistence = persistence
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    async def admit_observation(
        self, observation_id: DiscoveryObservationId
    ) -> AdmissionResult:
        """Admit one immutable observation exactly once.

        A controlled admission race is retried once in a fresh unit of work.
        A second conflict raises :class:`AdmissionRaceError`; no fabricated
        success is returned. Cancellation propagates with nothing committed.
        """
        try:
            return await self._admit_once(observation_id)
        except ConflictError:
            try:
                return await self._admit_once(observation_id)
            except ConflictError as exc:
                raise AdmissionRaceError(
                    "the admission race persisted after one bounded retry; "
                    "no fabricated admission was reported"
                ) from exc

    async def _admit_once(
        self, observation_id: DiscoveryObservationId
    ) -> AdmissionResult:
        async with self._persistence.unit_of_work() as uow:
            observation = await uow.discovery.get(observation_id)
            if observation is None:
                raise NotFoundError("the discovery observation does not exist")
            result = await uow.admission.admit(
                observation_id,
                SourceCandidateId.generate(),
                CandidateEventId.generate(),
                processed_at=self._clock(),
            )
            if result.candidate_id is not None and (
                observation.requires_auth_claim is True
            ):
                evidence = AccessEvidence(
                    access_evidence_id=AccessEvidenceId.generate(),
                    observation_id=observation_id,
                    candidate_id=result.candidate_id,
                    source_endpoint_id=None,
                    status=AccessStatus.CREDENTIALS_PENDING,
                    reason_code=PROVIDER_AUTH_CLAIM_REASON,
                    recorded_at=self._clock(),
                    evidence_reference=observation.evidence_reference,
                )
                await uow.access.append_evidence(evidence)
            await uow.commit()
            return result

    async def process_pending(
        self, *, provider_id: str, limit: int = 25
    ) -> AdmissionBatchResult:
        """Process at most ``limit`` unhandled provider observations.

        Pending identifiers are selected in one short read-only unit of work
        that is closed before any admission work starts; every observation is
        then admitted in its own transaction. On a bounded persistence
        failure the batch stops, previously committed entries survive, and
        the exact completed counts are returned with ``stopped_by_error``.
        This method never invokes an LLM, crawler, or qualification policy.
        """
        if limit < 1 or limit > MAX_ADMISSION_BATCH:
            raise ValueError(f"limit must be within 1..{MAX_ADMISSION_BATCH}")
        async with self._persistence.unit_of_work() as uow:
            observation_ids = await uow.admission.pending_observation_ids(
                provider_id, limit=limit
            )
            await uow.commit()

        counts = {
            AdmissionOutcome.ADMITTED_NEW: 0,
            AdmissionOutcome.ADMITTED_EXISTING: 0,
            AdmissionOutcome.KNOWN_ENDPOINT: 0,
            AdmissionOutcome.UNRESOLVED_NAME: 0,
            AdmissionOutcome.REVIEW_REQUIRED: 0,
        }
        processed_ids: list[DiscoveryObservationId] = []
        stopped = False
        for observation_id in observation_ids:
            try:
                result = await self.admit_observation(observation_id)
            except asyncio.CancelledError:
                raise
            except AdmissionInvariantError, PersistenceError:
                stopped = True
                break
            counts[result.outcome] += 1
            processed_ids.append(observation_id)
        return AdmissionBatchResult(
            processed=len(processed_ids),
            admitted_new=counts[AdmissionOutcome.ADMITTED_NEW],
            admitted_existing=counts[AdmissionOutcome.ADMITTED_EXISTING],
            known_endpoint=counts[AdmissionOutcome.KNOWN_ENDPOINT],
            unresolved_name=counts[AdmissionOutcome.UNRESOLVED_NAME],
            review_required=counts[AdmissionOutcome.REVIEW_REQUIRED],
            stopped_by_error=stopped,
            observation_ids=tuple(processed_ids),
        )

    async def possible_aliases(
        self, candidate_id: SourceCandidateId
    ) -> AliasSuggestionsResult:
        """Return bounded, read-only possible mirror/alias claims.

        No candidate, status, endpoint, or Source ownership is changed.
        """
        async with self._persistence.unit_of_work() as uow:
            suggestions = await uow.admission.possible_aliases(candidate_id)
            await uow.commit()
            return suggestions


__all__ = [
    "MAX_ADMISSION_BATCH",
    "AdmissionBatchResult",
    "AdmissionRaceError",
    "CandidateAdmissionService",
]
