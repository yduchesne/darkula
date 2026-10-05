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
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from darkula.app.data_stream import StreamMessage
from darkula.app.object_store import ContentHash
from darkula.domain.collection import (
    CollectionFailureCode,
    CollectionPolicy,
    CollectionRun,
    CollectionRunStatus,
)
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.extraction import ExtractedEntity, ExtractionResult
from darkula.domain.geography import GeographicResolution
from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    ConsumerId,
    ContentArtifactId,
    ExtractedEntityId,
    ExtractedRelationshipId,
    ExtractionResultId,
    GeographicResolutionId,
    MessageId,
    NormalizedContentId,
    RelationshipExtractionResultId,
    SourceCandidateId,
    SourceEndpointId,
    SourceId,
    StreamName,
)
from darkula.domain.relationships import (
    ExtractedRelationship,
    RelationshipExtractionResult,
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
    "ClaimOutcome",
    "CollectionRepository",
    "ContentRepository",
    "ExtractionRepository",
    "GeographicResolutionRepository",
    "OutboxRecord",
    "OutboxRepository",
    "ProcessedMessageRepository",
    "ReclaimOutcome",
    "RelationshipRepository",
    "ScheduleOutcome",
    "ScheduledOccurrence",
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


class ScheduleOutcome(StrEnum):
    """Outcome of one atomic due-occurrence admission (PR 9)."""

    SCHEDULED = "scheduled"
    NONE = "none"
    OCCURRENCE_EXISTS = "occurrence_exists"


class ClaimOutcome(StrEnum):
    """Outcome of a QUEUED -> RUNNING execution claim (PR 9)."""

    CLAIMED = "claimed"
    NOT_QUEUED = "not_queued"
    UNKNOWN_RUN = "unknown_run"


class ReclaimOutcome(StrEnum):
    """Outcome of an expired-lease reclaim attempt (PR 9)."""

    RECLAIMED = "reclaimed"
    LEASE_ACTIVE = "lease_active"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    TERMINAL = "terminal"
    UNKNOWN_RUN = "unknown_run"


@dataclass(frozen=True, slots=True)
class ScheduledOccurrence:
    """One atomically admitted policy occurrence plus its QUEUED run.

    ``run_id`` is the authoritative work identity; ``policy_id``/``source_id``
    accompany the ``collection.execute`` command so the worker validates its
    payload without trusting the broker. ``policy_snapshot`` is the frozen
    execution snapshot persisted with the run.
    """

    run_id: CollectionRunId
    policy_id: CollectionPolicyId
    policy_revision: int
    policy_snapshot: dict[str, object] | None
    source_id: SourceId
    scheduled_for: datetime
    outcome: ScheduleOutcome


class CollectionRepository(ABC):
    """Managed-source collection persistence contract (PR 9).

    All methods are bound to the owning unit of work. Production
    implementations invoke versioned PostgreSQL stored functions only.
    ``schedule_due`` performs the atomic occurrence admission; the caller
    appends the outbox record in the same transaction so a scheduled
    occurrence has a durable QUEUED run iff its command is durably in the
    outbox.
    """

    @abstractmethod
    async def create_policy(self, policy: CollectionPolicy) -> None:
        """Persist a policy (identity, schedule, budgets, endpoint list).

        :raises ConflictError: if the policy identity already exists.
        :raises IntegrityError: if the Source or an endpoint is unknown.
        """

    @abstractmethod
    async def get_policy(
        self, policy_id: CollectionPolicyId
    ) -> CollectionPolicy | None:
        """Return the policy with its authorized endpoint identities."""

    @abstractmethod
    async def update_policy(self, policy: CollectionPolicy) -> int:
        """Persist an edit; returns the new revision (old revision + 1).

        :raises NotFoundError: if the policy does not exist.
        :raises IntegrityError: if an authorized endpoint is unknown or
            belongs to another Source.
        """

    @abstractmethod
    async def list_policy_endpoints(
        self, policy_id: CollectionPolicyId
    ) -> tuple[SourceEndpoint, ...]:
        """Return the endpoints currently authorized by a policy,
        deterministically ordered."""

    @abstractmethod
    async def get_endpoint(
        self, endpoint_id: SourceEndpointId
    ) -> SourceEndpoint | None:
        """Return one endpoint's current state by managed identity."""

    @abstractmethod
    async def create_run(self, run: CollectionRun) -> None:
        """Create a QUEUED historical run directly (test/manual path).

        :raises ConflictError: if the run identity or the policy occurrence
            already exists.
        :raises IntegrityError: if the referenced policy does not exist.
        """

    @abstractmethod
    async def get_run(self, run_id: CollectionRunId) -> CollectionRun | None:
        """Return the run with its frozen policy snapshot."""

    @abstractmethod
    async def schedule_due(
        self,
        *,
        now: datetime,
        run_id: CollectionRunId,
        created_at: datetime,
    ) -> ScheduledOccurrence | None:
        """Atomically admit the earliest due occurrence, or return ``None``.

        When ``OCCURRENCE_EXISTS`` the occurrence was already admitted by a
        concurrent scheduler; the caller appends no command and continues.
        """

    @abstractmethod
    async def claim_run(
        self,
        *,
        run_id: CollectionRunId,
        execution_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
    ) -> ClaimOutcome:
        """Claim a QUEUED run: QUEUED -> RUNNING with an execution lease."""

    @abstractmethod
    async def reclaim_run(
        self,
        *,
        run_id: CollectionRunId,
        execution_id: str,
        now: datetime,
        lease_expires_at: datetime,
        max_attempts: int,
    ) -> ReclaimOutcome:
        """Reclaim a RUNNING run whose lease expired, attempt++."""

    @abstractmethod
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
        """Finalize a RUNNING run into a terminal state (PR 9).

        :raises ConflictError: if the run is not RUNNING (wrong state).
        :raises NotFoundError: if the run does not exist.
        """

    @abstractmethod
    async def cancel_run(
        self,
        *,
        run_id: CollectionRunId,
        completed_at: datetime,
    ) -> None:
        """Cancel a QUEUED or RUNNING run.

        :raises ConflictError: if the run is already terminal.
        :raises NotFoundError: if the run does not exist.
        """


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


class ExtractionRepository(ABC):
    """Deterministic extraction persistence contract (PR 11).

    All methods are transaction-scoped through the owning unit of work.
    Results and occurrences are append-only/immutable. ``create_result`` is
    idempotent on the semantic key ``(content_id, profile_name,
    profile_version)``: a concurrent duplicate raises :class:`ConflictError`
    instead of rewriting history. ``create_entity`` raises
    :class:`ConflictError` on an exact duplicate occurrence.
    """

    @abstractmethod
    async def create_result(self, result: ExtractionResult) -> None:
        """Persist one immutable extraction result.

        :raises ConflictError: if ``(content_id, profile_name,
            profile_version)`` or the result identity already exists (no
            overwrite).
        :raises IntegrityError: if the referenced normalized content does
            not exist.
        """

    @abstractmethod
    async def get_result(
        self, result_id: ExtractionResultId
    ) -> ExtractionResult | None:
        """Return one result by identity or ``None`` when absent."""

    @abstractmethod
    async def get_result_by_profile(
        self,
        content_id: NormalizedContentId,
        profile_name: str,
        profile_version: str,
    ) -> ExtractionResult | None:
        """Return the result for the semantic content/profile key or ``None``."""

    @abstractmethod
    async def create_entity(self, entity: ExtractedEntity) -> None:
        """Persist one extracted-entity occurrence.

        :raises ConflictError: if the exact occurrence already exists.
        :raises IntegrityError: if the result/content provenance is unknown
            or inconsistent.
        """

    @abstractmethod
    async def list_entities_for_result(
        self, result_id: ExtractionResultId
    ) -> tuple[ExtractedEntity, ...]:
        """Return a result's occurrences in deterministic order."""

    @abstractmethod
    async def list_entities_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[ExtractedEntity, ...]:
        """Return a content's occurrences across all results, in
        deterministic order."""

    @abstractmethod
    async def get_entity(self, entity_id: ExtractedEntityId) -> ExtractedEntity | None:
        """Return one extracted-entity occurrence by identity or ``None``."""


class GeographicResolutionRepository(ABC):
    """Immutable/versioned geographic-resolution persistence contract (PR 12).

    All methods are transaction-scoped through the owning unit of work. The
    semantic idempotency key is
    ``(extracted_entity_id, resolver_name, resolver_version)``: a duplicate
    raises :class:`ConflictError`; a new resolver version coexists. Resolutions
    are append-only and never mutate the extracted occurrence.
    """

    @abstractmethod
    async def create(self, resolution: GeographicResolution) -> None:
        """Persist one immutable resolution.

        :raises ConflictError: if the semantic key or resolution identity
            already exists (no overwrite).
        :raises IntegrityError: if the referenced occurrence does not exist.
        """

    @abstractmethod
    async def get(
        self, resolution_id: GeographicResolutionId
    ) -> GeographicResolution | None:
        """Return one resolution by identity or ``None``."""

    @abstractmethod
    async def get_for_entity(
        self,
        entity_id: ExtractedEntityId,
        resolver_name: str,
        resolver_version: str,
    ) -> GeographicResolution | None:
        """Return the resolution for the occurrence/resolver key or ``None``."""

    @abstractmethod
    async def list_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[GeographicResolution, ...]:
        """Return a content's resolutions in deterministic order."""


class RelationshipRepository(ABC):
    """Immutable/versioned relationship-assertion persistence contract (PR 13).

    All methods are transaction-scoped through the owning unit of work.
    Results and assertions are append-only and immutable. ``create_result`` is
    idempotent on the semantic key ``(content_id, profile_name,
    profile_version)``: a concurrent duplicate raises :class:`ConflictError`
    instead of rewriting history. ``create_relationship`` raises
    :class:`ConflictError` on an exact duplicate occurrence and
    :class:`IntegrityError` when endpoint/content provenance is inconsistent.
    """

    @abstractmethod
    async def create_result(self, result: RelationshipExtractionResult) -> None:
        """Persist one immutable relationship-extraction result.

        :raises ConflictError: if ``(content_id, profile_name,
            profile_version)`` or the result identity already exists (no
            overwrite).
        :raises IntegrityError: if the referenced normalized content does not
            exist.
        """

    @abstractmethod
    async def get_result(
        self, result_id: RelationshipExtractionResultId
    ) -> RelationshipExtractionResult | None:
        """Return one result by identity or ``None`` when absent."""

    @abstractmethod
    async def get_result_by_profile(
        self,
        content_id: NormalizedContentId,
        profile_name: str,
        profile_version: str,
    ) -> RelationshipExtractionResult | None:
        """Return the result for the semantic content/profile key or ``None``."""

    @abstractmethod
    async def create_relationship(self, relationship: ExtractedRelationship) -> None:
        """Persist one relationship assertion occurrence.

        :raises ConflictError: if the exact occurrence already exists.
        :raises IntegrityError: if the result/content/endpoint provenance is
            unknown or inconsistent, or the assertion is a self-edge.
        """

    @abstractmethod
    async def list_for_result(
        self, result_id: RelationshipExtractionResultId
    ) -> tuple[ExtractedRelationship, ...]:
        """Return a result's assertions in deterministic order."""

    @abstractmethod
    async def list_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[ExtractedRelationship, ...]:
        """Return a content's assertions across all results, in
        deterministic order."""

    @abstractmethod
    async def get_relationship(
        self, relationship_id: ExtractedRelationshipId
    ) -> ExtractedRelationship | None:
        """Return one assertion occurrence by identity or ``None``."""
