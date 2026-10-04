# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral source-domain repository contracts (PR 4).

Repositories are aggregate-oriented and transaction-scoped: instances are
obtained only through :class:`~darkula.app.persistence.UnitOfWork`, never
constructed directly, and a single unit-of-work commit covers all of their
work.

Semantics frozen here:

- ``SourceCandidateRepository`` owns candidates, candidate lifecycle history
  and recon assessments;
- ``SourceRepository`` owns managed sources, endpoints and source
  assessments;
- status transitions pass the *expected* current status and are atomic with
  exactly one apprentice history event (conflict on mismatch);
- candidate event history and both assessment histories are append-only in
  production: no update/delete methods exist;
- every production implementation of these contracts invokes versioned
  PostgreSQL stored functions; no implementation may embed data-access SQL
  in Python (see the PostgreSQL adapter and the migration artifacts).

Conflict/integrity/not-found failures surface through the bounded
``PersistenceError`` subtypes defined in
:mod:`darkula.app.persistence`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID

from darkula.app.data_stream import StreamMessage
from darkula.app.object_store import ContentHash
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.identifiers import (
    ConsumerId,
    ContentArtifactId,
    MessageId,
    NormalizedContentId,
    SourceCandidateId,
    SourceId,
    StreamName,
)
from darkula.domain.source import (
    CandidateStatus,
    ReconAssessment,
    Source,
    SourceAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
    SourceEndpoint,
)

__all__ = [
    "ContentRepository",
    "OutboxRecord",
    "OutboxRepository",
    "ProcessedMessageRepository",
    "SourceCandidateRepository",
    "SourceRepository",
]


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """One claimed outbox row with its full application envelope.

    ``outbox_id`` is the persistence identity of the outbox row; the broker
    position is never persisted and never used as identity here.
    """

    outbox_id: UUID
    stream_name: StreamName
    message: StreamMessage


class OutboxRepository(ABC):
    """Transactional outbox persistence contract (PR 5).

    All methods are bound to the owning unit of work. ``append`` must be
    called inside the same transaction as the business-state mutation it
    announces; the broker publish happens only after that transaction
    commits. ``claim``/``mark_published`` are the short-unit-of-work
    publisher operations and must never run inside a transaction that
    holds broker I/O open.
    """

    @abstractmethod
    async def append(
        self,
        message: StreamMessage,
        *,
        stream_name: StreamName,
    ) -> None:
        """Persist one outbox record for ``message`` on ``stream_name``.

        :raises ConflictError: if ``message.message_id`` was already
            appended (idempotent-duplicate frozen behavior; the original
            record is never overwritten).
        """

    @abstractmethod
    async def claim(
        self,
        *,
        stream_name: StreamName,
        limit: int,
        claim_id: UUID,
        lease_seconds: float,
    ) -> tuple[OutboxRecord, ...]:
        """Claim up to ``limit`` pending, unexpired-lease-free rows.

        Claimed rows carry ``claim_id`` and a lease; a crash after claim
        lets the lease expire and the row becomes retryable.
        """

    @abstractmethod
    async def mark_published(
        self, *, claim_id: UUID, outbox_ids: tuple[UUID, ...]
    ) -> int:
        """Mark a subset of claimed rows published; return the count.

        Rows not marked remain retryable; a crash after broker publish
        before this call may republish the message (consumers deduplicate
        by ``message_id``).
        """


class ProcessedMessageRepository(ABC):
    """Durable consumer idempotency contract (PR 5).

    The idempotency key is ``(stream_name, consumer_id, message_id)`` and
    is never a broker offset or position.
    """

    @abstractmethod
    async def record(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        message_id: MessageId,
    ) -> bool:
        """Atomically record one processed message.

        Returns ``True`` on first delivery (the caller must perform the
        durable business effect inside the same unit of work) and ``False``
        for a duplicate (no repeated effect, no duplicate outgoing outbox).
        """


class SourceCandidateRepository(ABC):
    """Candidate lifecycle + recon assessment persistence contract.

    All methods are bound to the owning unit of work; there is no
    repository-owned transaction or connection.
    """

    @abstractmethod
    async def create(
        self,
        candidate: SourceCandidate,
        *,
        event: SourceCandidateEventHistory,
    ) -> None:
        """Persist a candidate and its initial history event atomically.

        :raises ConflictError: if the candidate or initial event identity
            already exists (no update, no overwrite).
        """

    @abstractmethod
    async def get(self, candidate_id: SourceCandidateId) -> SourceCandidate | None:
        """Return the candidate or ``None`` when it does not exist."""

    @abstractmethod
    async def transition(
        self,
        candidate_id: SourceCandidateId,
        *,
        expected: CandidateStatus,
        new_status: CandidateStatus,
        event: SourceCandidateEventHistory,
    ) -> None:
        """Transition candidate status atomically with one history event.

        The transition applies only when the current status equals
        ``expected``; the stored function updates the status and appends
        exactly one event in one database operation.

        :raises ConflictError: if the current status differs from
            ``expected`` (no update is applied and no event is appended).
        """

    @abstractmethod
    async def append_history(self, event: SourceCandidateEventHistory) -> None:
        """Append one candidate lifecycle history record.

        :raises ConflictError: if the event identity already exists.
        :raises IntegrityError: if the referenced candidate does not exist.
        """

    @abstractmethod
    async def list_history(
        self, candidate_id: SourceCandidateId
    ) -> tuple[SourceCandidateEventHistory, ...]:
        """Return the candidate's history in deterministic order."""

    @abstractmethod
    async def append_recon_assessment(self, assessment: ReconAssessment) -> None:
        """Append one immutable recon assessment.

        :raises ConflictError: if the assessment identity already exists.
        :raises IntegrityError: if the referenced candidate does not exist.
        """

    @abstractmethod
    async def list_recon_assessments(
        self, candidate_id: SourceCandidateId
    ) -> tuple[ReconAssessment, ...]:
        """Return the candidate's recon assessments in deterministic order."""


class SourceRepository(ABC):
    """Managed source, endpoint and source assessment persistence contract.

    All methods are bound to the owning unit of work; there is no
    repository-owned transaction or connection.
    """

    @abstractmethod
    async def create(self, source: Source) -> None:
        """Persist a managed source.

        :raises ConflictError: if the source identity already exists.
        """

    @abstractmethod
    async def get(self, source_id: SourceId) -> Source | None:
        """Return the source or ``None`` when it does not exist."""

    @abstractmethod
    async def add_endpoint(self, endpoint: SourceEndpoint) -> None:
        """Attach one endpoint to its source.

        :raises ConflictError: if the endpoint identity already exists.
        :raises IntegrityError: if the referenced source does not exist.
        """

    @abstractmethod
    async def list_endpoints(self, source_id: SourceId) -> tuple[SourceEndpoint, ...]:
        """Return the source's endpoints in deterministic order."""

    @abstractmethod
    async def append_source_assessment(self, assessment: SourceAssessment) -> None:
        """Append one immutable source assessment.

        :raises ConflictError: if the assessment identity already exists.
        :raises IntegrityError: if the referenced source does not exist.
        """

    @abstractmethod
    async def list_source_assessments(
        self, source_id: SourceId
    ) -> tuple[SourceAssessment, ...]:
        """Return the source's assessments in deterministic order."""


class ContentRepository(ABC):
    """Normalized-content and content-artifact persistence contract (PR 8).

    All methods are transaction-scoped through the owning unit of work.
    ObjectStore owns the artifact bytes; these methods persist only
    structured metadata/provenance. ``create_observation`` is idempotent on
    the provenance identity ``(crawl_request_id, observation_index,
    source_uri)``: a retried observation raises :class:`ConflictError`
    instead of creating an uncontrolled duplicate record.
    """

    @abstractmethod
    async def create_artifact(self, artifact: ContentArtifact) -> None:
        """Persist one logical artifact record.

        :raises ConflictError: if the artifact identity or representation
            already exists. The original record is never overwritten.
        """

    @abstractmethod
    async def get_artifact(
        self, artifact_id: ContentArtifactId
    ) -> ContentArtifact | None:
        """Return the artifact record or ``None`` when absent."""

    @abstractmethod
    async def find_artifact_by_representation(
        self,
        *,
        content_hash: ContentHash,
        kind: ArtifactKind,
        completeness: ArtifactCompleteness,
    ) -> ContentArtifact | None:
        """Return the existing artifact record for a representation identity,
        or ``None`` when none exists."""

    @abstractmethod
    async def create_observation(self, content: NormalizedContent) -> None:
        """Persist one immutable normalized observation.

        :raises ConflictError: if the provenance identity (or content id)
            already exists.
        :raises IntegrityError: if the referenced artifact does not exist.
        """

    @abstractmethod
    async def get_observation(
        self, content_id: NormalizedContentId
    ) -> NormalizedContent | None:
        """Return the normalized observation or ``None`` when absent."""
