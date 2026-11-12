# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic source-discovery ingestion (PR 17).

``SourceDiscoveryService`` is a deterministic, one-observation-at-a-time
ingest boundary for untrusted provider claims:

    provider adapter (PR 18/22) -> DiscoveryInput -> validate/normalize
        -> one short UnitOfWork -> versioned stored function
        -> immutable DiscoveryObservation

It never fetches, crawls, resolves DNS, calls an LLM, creates/updates a
candidate or source, appends an event/outbox record, or grants access/crawl
authority. A replay returns the persisted original observation (identity and
acceptance time); a same-key payload difference surfaces as
:class:`~darkula.app.persistence.ConflictError` with zero new records.

``DiscoveryProvider`` is a provider-neutral protocol only: PR 18 owns real
provider adapters, polling, checkpoints, and batching.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from darkula.app.persistence import ConflictError, DarkulaSpi, NotFoundError
from darkula.domain.discovery import (
    AccessEvidence,
    AccessStatus,
    DiscoveryInput,
    DiscoveryObservation,
    observation_from_input,
)
from darkula.domain.identifiers import (
    AccessEvidenceId,
    DiscoveryObservationId,
    SourceCandidateId,
)

#: The fixed PR17 reason code for provider-claimed authentication
#: requirements. Bounded upper-case vocabulary (see migration 0009).
PROVIDER_AUTH_CLAIM_REASON = "PROVIDER_AUTH_CLAIM"


class DiscoveryProvider(ABC):
    """Provider-neutral protocol for bounded discovery-claim sources (PR 18).

    Implementations yield :class:`DiscoveryInput` values from a bounded
    iterable/async iterator. They must not perform Deep/Dark-Web retrieval
    themselves beyond their own narrowly authorized adapter scope, must not
    emit credentials/secrets, and must propagate cancellation unchanged.
    PR 17 defines the protocol only: no concrete provider, fetch, paging,
    polling, checkpoint, or scheduling behavior is implemented here, and it
    is deliberately not exposed as a generic agent tool.
    """

    @abstractmethod
    def observations(self) -> AsyncIterator[DiscoveryInput]:
        """Yield bounded untrusted discovery claims."""
        raise NotImplementedError


class SourceDiscoveryService:
    """Persist untrusted discovery observations idempotently (PR 17).

    ``clock`` supplies Darkula acceptance time and must return a
    timezone-aware UTC ``datetime`` (naive values are rejected by the domain).
    """

    def __init__(
        self,
        *,
        persistence: DarkulaSpi,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._persistence = persistence
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    async def ingest(self, source: DiscoveryInput) -> DiscoveryObservation:
        """Validate, normalize and durably record one discovery observation.

        Validation/normalization happens before any persistence work.
        :class:`asyncio.CancelledError` propagates and rolls the transaction
        back; no observation is reported as persisted.
        """
        attempt = observation_from_input(
            source,
            observation_id=DiscoveryObservationId.generate(),
            ingested_at=self._clock(),
        )
        async with self._persistence.unit_of_work() as uow:
            persisted = await uow.discovery.insert_or_get_exact(attempt)
            await uow.commit()
            return persisted

    async def record_pending_for_existing_candidate(
        self,
        *,
        observation_id: DiscoveryObservationId,
        candidate_id: SourceCandidateId,
    ) -> AccessEvidence:
        """Record ``CREDENTIALS_PENDING`` evidence for an existing candidate.

        Only valid when the stored observation claims ``requires_auth=True``.
        The operation appends immutable, non-authorizing evidence and never
        changes the candidate's lifecycle status, credentials, or access
        readiness (``READY`` is never produced here). Missing observation or
        candidate surfaces as
        :class:`~darkula.app.persistence.NotFoundError`.

        This is an explicit operator/PR19-facing entry point; PR17 ingestion
        itself never links access evidence to a candidate.
        """
        async with self._persistence.unit_of_work() as uow:
            observation = await uow.discovery.get(observation_id)
            if observation is None:
                raise NotFoundError("the discovery observation does not exist")
            if observation.requires_auth_claim is not True:
                raise ConflictError(
                    "the discovery observation does not claim required "
                    "authentication; no access evidence was recorded"
                )
            evidence = AccessEvidence(
                access_evidence_id=AccessEvidenceId.generate(),
                observation_id=observation_id,
                candidate_id=candidate_id,
                source_endpoint_id=None,
                status=AccessStatus.CREDENTIALS_PENDING,
                reason_code=PROVIDER_AUTH_CLAIM_REASON,
                recorded_at=self._clock(),
                evidence_reference=observation.evidence_reference,
            )
            persisted = await uow.access.append_evidence(evidence)
            await uow.commit()
            return persisted


__all__ = [
    "PROVIDER_AUTH_CLAIM_REASON",
    "DiscoveryProvider",
    "SourceDiscoveryService",
]
