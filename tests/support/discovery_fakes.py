# SPDX-License-Identifier: AGPL-3.0-only
"""In-memory parity fakes for the PR 17 discovery persistence contracts.

These fakes implement the same provider-neutral repository contracts as the
PostgreSQL adapter so unit tests can exercise the deterministic
``SourceDiscoveryService`` and the qualification-decision primitive without
a database. They preserve the persisted semantics that matter to callers:
append-only immutability, exact-replay idempotency, typed conflicts, and
commit/rollback atomicity.

They are test support only (never production composition) and are not a
second persistence framework.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import datetime
from types import TracebackType
from typing import Any, Self

from darkula.app.persistence import (
    ConflictError,
    DarkulaSpi,
    IntegrityError,
    NotFoundError,
    UnitOfWork,
)
from darkula.app.repositories import (
    AccessRepository,
    DiscoveryRepository,
    QualificationRepository,
)
from darkula.domain.discovery import (
    AccessEvidence,
    AccessStatus,
    DiscoveryDetectionMethod,
    DiscoveryObservation,
    DiscoveryReferenceKind,
    QualificationDecision,
    QualificationMethod,
    qualification_event_type,
)
from darkula.domain.identifiers import (
    CandidateEventId,
    DiscoveryObservationId,
    QualificationDecisionId,
    ReconAssessmentId,
    SourceCandidateId,
    SourceEndpointId,
)
from darkula.domain.registry_polling import RegistryPollCheckpoint
from darkula.domain.source import (
    CandidateStatus,
    ReconAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
)

ObservationKey = tuple[str, str, str, str, str]


def _same_payload(first: Any, second: Any, *, excluded: frozenset[str]) -> bool:
    """Compare every persisted field except the excluded generated ones."""
    return all(
        getattr(first, field.name) == getattr(second, field.name)
        for field in fields(first)
        if field.name not in excluded
    )


_OBSERVATION_GENERATED = frozenset({"observation_id", "ingested_at"})
_ACCESS_GENERATED = frozenset({"access_evidence_id", "recorded_at"})
_DECISION_GENERATED = frozenset({"decision_id"})


@dataclass
class MemDiscoveryState:
    """Committed in-memory discovery/access/qualification state."""

    observations_by_key: dict[ObservationKey, DiscoveryObservation]
    observations_by_id: dict[DiscoveryObservationId, DiscoveryObservation]
    candidate_status: dict[SourceCandidateId, CandidateStatus]
    candidate_events: list[SourceCandidateEventHistory]
    recon_assessments: dict[ReconAssessmentId, SourceCandidateId]
    access: dict[tuple[DiscoveryObservationId, str, str], AccessEvidence]
    decisions_by_key: dict[str, QualificationDecision]
    decisions_by_id: dict[QualificationDecisionId, QualificationDecision]
    registry_checkpoints: dict[str, RegistryPollCheckpoint]

    @classmethod
    def empty(cls) -> MemDiscoveryState:
        return cls(
            observations_by_key={},
            observations_by_id={},
            candidate_status={},
            candidate_events=[],
            recon_assessments={},
            access={},
            decisions_by_key={},
            decisions_by_id={},
            registry_checkpoints={},
        )


@dataclass
class _Pending:
    observations: list[DiscoveryObservation]
    access: list[AccessEvidence]
    decisions: list[tuple[QualificationDecision, SourceCandidateEventHistory]]
    registry_checkpoints: dict[str, RegistryPollCheckpoint]

    @classmethod
    def empty(cls) -> _Pending:
        return cls(
            observations=[],
            access=[],
            decisions=[],
            registry_checkpoints={},
        )


class MemDiscoveryRepository(DiscoveryRepository):
    """In-memory discovery repository with exact-replay semantics."""

    def __init__(self, state: MemDiscoveryState, pending: _Pending) -> None:
        self._state = state
        self._pending = pending

    def _find(self, key: ObservationKey) -> DiscoveryObservation | None:
        for observation in reversed(self._pending.observations):
            if self._key(observation) == key:
                return observation
        return self._state.observations_by_key.get(key)

    @staticmethod
    def _key(observation: DiscoveryObservation) -> ObservationKey:
        return (
            observation.provider_id,
            observation.provider_record_id,
            observation.provider_record_version,
            observation.detection_method.value,
            observation.reference_kind.value,
        )

    async def insert_or_get_exact(
        self, observation: DiscoveryObservation
    ) -> DiscoveryObservation:
        existing = self._find(self._key(observation))
        if existing is not None:
            if not _same_payload(
                existing, observation, excluded=_OBSERVATION_GENERATED
            ):
                raise ConflictError(
                    "the discovery observation semantic key already exists "
                    "with a different payload"
                )
            return existing
        self._pending.observations.append(observation)
        return observation

    async def get(
        self, observation_id: DiscoveryObservationId
    ) -> DiscoveryObservation | None:
        for observation in reversed(self._pending.observations):
            if observation.observation_id == observation_id:
                return observation
        return self._state.observations_by_id.get(observation_id)

    async def get_by_key(
        self,
        *,
        provider_id: str,
        provider_record_id: str,
        provider_record_version: str,
        detection_method: DiscoveryDetectionMethod,
        reference_kind: DiscoveryReferenceKind,
    ) -> DiscoveryObservation | None:
        return self._find(
            (
                provider_id,
                provider_record_id,
                provider_record_version,
                detection_method.value,
                reference_kind.value,
            )
        )

    async def list_for_provider(
        self,
        provider_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[DiscoveryObservation, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be within 1..200")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        visible = {
            item.observation_id: item
            for item in self._state.observations_by_key.values()
        }
        visible.update(
            {item.observation_id: item for item in self._pending.observations}
        )
        selected = sorted(
            (item for item in visible.values() if item.provider_id == provider_id),
            key=lambda item: (item.observed_at, str(item.observation_id)),
        )
        return tuple(selected[offset : offset + limit])

    def _current_checkpoint(self, provider_id: str) -> RegistryPollCheckpoint | None:
        staged = self._pending.registry_checkpoints.get(provider_id)
        if staged is not None:
            return staged
        return self._state.registry_checkpoints.get(provider_id)

    async def get_registry_checkpoint(self, provider_id: str) -> RegistryPollCheckpoint:
        checkpoint = self._current_checkpoint(provider_id)
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
        current = self._current_checkpoint(provider_id)
        if expected_generation == 0:
            if current is not None:
                raise ConflictError(
                    "the registry poll checkpoint was advanced by another writer"
                )
            updated = RegistryPollCheckpoint(
                provider_id=provider_id,
                cursor=cursor,
                generation=1,
                cooldown_until=None,
                last_completed_at=completed_at,
            )
        else:
            if current is None or current.generation != expected_generation:
                raise ConflictError(
                    "the registry poll checkpoint was advanced by another writer"
                )
            updated = RegistryPollCheckpoint(
                provider_id=provider_id,
                cursor=cursor,
                generation=expected_generation + 1,
                cooldown_until=None,
                last_completed_at=completed_at,
            )
        self._pending.registry_checkpoints[provider_id] = updated
        return updated

    async def defer_registry_checkpoint(
        self,
        provider_id: str,
        *,
        expected_generation: int,
        not_before: datetime,
    ) -> RegistryPollCheckpoint:
        current = self._current_checkpoint(provider_id)
        if expected_generation == 0:
            if current is not None:
                raise ConflictError(
                    "the registry poll checkpoint was advanced by another writer"
                )
            updated = RegistryPollCheckpoint(
                provider_id=provider_id,
                cursor=None,
                generation=1,
                cooldown_until=not_before,
                last_completed_at=None,
            )
        else:
            if current is None or current.generation != expected_generation:
                raise ConflictError(
                    "the registry poll checkpoint was advanced by another writer"
                )
            existing_cooldown = current.cooldown_until
            cooldown = (
                not_before
                if existing_cooldown is None
                else max(existing_cooldown, not_before)
            )
            updated = RegistryPollCheckpoint(
                provider_id=provider_id,
                cursor=current.cursor,
                generation=current.generation + 1,
                cooldown_until=cooldown,
                last_completed_at=current.last_completed_at,
            )
        self._pending.registry_checkpoints[provider_id] = updated
        return updated


class MemAccessRepository(AccessRepository):
    """In-memory access-evidence repository (pending-only write path).

    ``candidate_exists`` lets the composed PR 19 admission fake recognize a
    candidate staged earlier in the same (still open) unit of work. It
    defaults to the committed PR 17 state and never widens the production
    write path.
    """

    def __init__(
        self,
        state: MemDiscoveryState,
        pending: _Pending,
        *,
        candidate_exists: Callable[[SourceCandidateId], bool] | None = None,
    ) -> None:
        self._state = state
        self._pending = pending
        self._candidate_exists = candidate_exists or (
            lambda candidate_id: candidate_id in self._state.candidate_status
        )

    def _observation(
        self, observation_id: DiscoveryObservationId
    ) -> DiscoveryObservation | None:
        for observation in reversed(self._pending.observations):
            if observation.observation_id == observation_id:
                return observation
        return self._state.observations_by_id.get(observation_id)

    def _all(self) -> list[AccessEvidence]:
        return [*self._state.access.values(), *self._pending.access]

    async def append_evidence(self, evidence: AccessEvidence) -> AccessEvidence:
        if evidence.candidate_id is None or evidence.source_endpoint_id is not None:
            raise ValueError("PR17 access evidence must be candidate-linked")
        if evidence.status is not AccessStatus.CREDENTIALS_PENDING:
            raise ValueError("PR17 only appends CREDENTIALS_PENDING evidence")
        observation = self._observation(evidence.observation_id)
        if observation is None:
            raise NotFoundError("the discovery observation does not exist")
        if not self._candidate_exists(evidence.candidate_id):
            raise NotFoundError("the candidate does not exist")
        if (
            observation.candidate_id is not None
            and observation.candidate_id != evidence.candidate_id
        ):
            raise ConflictError(
                "the discovery observation is linked to a different candidate"
            )
        if observation.requires_auth_claim is not True:
            raise ConflictError(
                "the discovery observation does not claim required authentication"
            )
        for existing in self._all():
            if (
                existing.observation_id != evidence.observation_id
                or existing.candidate_id != evidence.candidate_id
            ):
                continue
            if existing.status is not evidence.status:
                raise ConflictError(
                    "conflicting access evidence already exists for this "
                    "observation/candidate"
                )
            if not _same_payload(existing, evidence, excluded=_ACCESS_GENERATED):
                raise ConflictError(
                    "conflicting access evidence already exists for this "
                    "observation/candidate"
                )
            return existing
        self._pending.access.append(evidence)
        return evidence

    async def list_for_candidate(
        self, candidate_id: SourceCandidateId
    ) -> tuple[AccessEvidence, ...]:
        items = [item for item in self._all() if item.candidate_id == candidate_id]
        return tuple(
            sorted(
                items, key=lambda item: (item.recorded_at, str(item.access_evidence_id))
            )
        )

    async def list_for_endpoint(
        self, endpoint_id: SourceEndpointId
    ) -> tuple[AccessEvidence, ...]:
        items = [item for item in self._all() if item.source_endpoint_id == endpoint_id]
        return tuple(
            sorted(
                items, key=lambda item: (item.recorded_at, str(item.access_evidence_id))
            )
        )

    async def has_credentials_pending_for_candidate(
        self, candidate_id: SourceCandidateId
    ) -> bool:
        """Constant-shape probe; never materializes the evidence list API."""
        for evidence in self._state.access.values():
            if (
                evidence.candidate_id == candidate_id
                and evidence.status is AccessStatus.CREDENTIALS_PENDING
            ):
                return True
        for evidence in self._pending.access:
            if (
                evidence.candidate_id == candidate_id
                and evidence.status is AccessStatus.CREDENTIALS_PENDING
            ):
                return True
        return False

    async def latest_for_candidate(
        self, candidate_id: SourceCandidateId
    ) -> AccessEvidence | None:
        items = await self.list_for_candidate(candidate_id)
        return items[-1] if items else None

    async def latest_for_endpoint(
        self, endpoint_id: SourceEndpointId
    ) -> AccessEvidence | None:
        items = await self.list_for_endpoint(endpoint_id)
        return items[-1] if items else None


class MemQualificationRepository(QualificationRepository):
    """In-memory atomic qualification decision + transition + event primitive.

    ``recon_assessment_owner`` lets the composed PR 19 admission fake resolve
    a genuine assessment staged in the shared candidate fake. It defaults to
    the committed PR 17 assessment map.
    """

    def __init__(
        self,
        state: MemDiscoveryState,
        pending: _Pending,
        *,
        recon_assessment_owner: (
            Callable[[ReconAssessmentId], SourceCandidateId | None] | None
        ) = None,
    ) -> None:
        self._state = state
        self._pending = pending
        self._recon_assessment_owner = recon_assessment_owner or (
            self._state.recon_assessments.get
        )

    def _status(self, candidate_id: SourceCandidateId) -> CandidateStatus | None:
        for decision, _event in reversed(self._pending.decisions):
            if decision.candidate_id == candidate_id:
                return decision.new_status
        return self._state.candidate_status.get(candidate_id)

    async def decide_and_transition(
        self,
        decision: QualificationDecision,
        event: SourceCandidateEventHistory,
    ) -> QualificationDecision:
        if event.candidate_id != decision.candidate_id:
            raise ValueError("the qualification event belongs to another candidate")
        if event.event_type is not qualification_event_type(decision.outcome):
            raise ValueError(
                "the qualification event type does not match the decision outcome"
            )
        if event.event_id != decision.event_id:
            raise ValueError("qualification event_id does not match decision.event_id")

        existing = self._existing_decision(decision.decision_key)
        if existing is not None:
            stored_event = self._stored_event(existing.event_id)
            if stored_event is None:
                raise ConflictError(
                    "the stored qualification event for the decision key is missing"
                )
            if not _same_payload(existing, decision, excluded=_DECISION_GENERATED):
                raise ConflictError(
                    "the qualification decision key already exists with a "
                    "different payload"
                )
            if stored_event != event:
                raise ConflictError(
                    "the qualification decision key already exists with a "
                    "different event identity or payload"
                )
            return existing

        current = self._status(decision.candidate_id)
        if current is None:
            raise NotFoundError("the candidate does not exist")
        if current is not decision.expected_status:
            raise ConflictError(
                "the candidate status does not equal the expected status"
            )
        observations = [
            self._observation(observation_id)
            for observation_id in decision.evidence_observation_ids
        ]
        if any(observation is None for observation in observations):
            raise IntegrityError("a referenced discovery observation does not exist")
        if decision.method is QualificationMethod.REGISTRY_POLICY and not any(
            observation is not None
            and observation.provider_id == decision.provider_id
            and observation.provider_record_id == decision.provider_record_id
            and observation.detection_method is DiscoveryDetectionMethod.REGISTRY
            for observation in observations
        ):
            raise IntegrityError(
                "no cited registry observation establishes the claimed provider record"
            )
        if decision.recon_assessment_id is not None:
            owner = self._recon_assessment_owner(decision.recon_assessment_id)
            if owner != decision.candidate_id:
                raise IntegrityError(
                    "the recon assessment does not belong to the candidate"
                )
        if self._decision_id_used(decision.decision_id):
            raise ConflictError("the qualification decision identity is already in use")
        if self._event_id_used(event.event_id):
            raise ConflictError("the qualification event identity is already in use")
        self._pending.decisions.append((decision, event))
        return decision

    def _existing_decision(self, decision_key: str) -> QualificationDecision | None:
        for decision, _event in reversed(self._pending.decisions):
            if decision.decision_key == decision_key:
                return decision
        return self._state.decisions_by_key.get(decision_key)

    def _stored_event(
        self, event_id: CandidateEventId
    ) -> SourceCandidateEventHistory | None:
        for _decision, event in reversed(self._pending.decisions):
            if event.event_id == event_id:
                return event
        for event in reversed(self._state.candidate_events):
            if event.event_id == event_id:
                return event
        return None

    def _observation(
        self, observation_id: DiscoveryObservationId
    ) -> DiscoveryObservation | None:
        for observation in reversed(self._pending.observations):
            if observation.observation_id == observation_id:
                return observation
        return self._state.observations_by_id.get(observation_id)

    def _decision_id_used(self, decision_id: QualificationDecisionId) -> bool:
        if decision_id in self._state.decisions_by_id:
            return True
        return any(
            decision.decision_id == decision_id
            for decision, _event in self._pending.decisions
        )

    def _event_id_used(self, event_id: CandidateEventId) -> bool:
        if any(event.event_id == event_id for event in self._state.candidate_events):
            return True
        return any(
            event.event_id == event_id for _decision, event in self._pending.decisions
        )

    async def get(
        self, decision_id: QualificationDecisionId
    ) -> QualificationDecision | None:
        for decision, _event in self._pending.decisions:
            if decision.decision_id == decision_id:
                return decision
        return self._state.decisions_by_id.get(decision_id)

    async def get_by_key(self, decision_key: str) -> QualificationDecision | None:
        for decision, _event in self._pending.decisions:
            if decision.decision_key == decision_key:
                return decision
        return self._state.decisions_by_key.get(decision_key)

    async def list_for_candidate(
        self, candidate_id: SourceCandidateId
    ) -> tuple[QualificationDecision, ...]:
        visible = {
            item.decision_id: item for item in self._state.decisions_by_id.values()
        }
        for decision, _event in self._pending.decisions:
            visible[decision.decision_id] = decision
        selected = [
            item for item in visible.values() if item.candidate_id == candidate_id
        ]
        return tuple(
            sorted(
                selected, key=lambda item: (item.evaluated_at, str(item.decision_id))
            )
        )


def apply_pending_discovery(state: MemDiscoveryState, pending: _Pending) -> None:
    """Apply one unit of work's staged discovery/access/decision mutations.

    Shared by the PR 17 discovery fake and the PR 19 admission fake so the
    commit semantics exist in exactly one place.
    """
    for observation in pending.observations:
        state.observations_by_key[MemDiscoveryRepository._key(observation)] = (
            observation
        )
        state.observations_by_id[observation.observation_id] = observation
    for evidence in pending.access:
        key = (
            evidence.observation_id,
            str(evidence.candidate_id),
            evidence.status.value,
        )
        state.access[key] = evidence
    for decision, event in pending.decisions:
        state.candidate_status[decision.candidate_id] = decision.new_status
        state.candidate_events.append(event)
        state.decisions_by_key[decision.decision_key] = decision
        state.decisions_by_id[decision.decision_id] = decision
    for provider_id, checkpoint in pending.registry_checkpoints.items():
        state.registry_checkpoints[provider_id] = checkpoint


class MemDiscoveryUnitOfWork(UnitOfWork):
    """One in-memory transaction over :class:`MemDiscoveryState`."""

    def __init__(self, state: MemDiscoveryState) -> None:
        self._state = state
        self._pending = _Pending.empty()
        self._discovery: MemDiscoveryRepository | None = None
        self._access: MemAccessRepository | None = None
        self._qualification: MemQualificationRepository | None = None
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        apply_pending_discovery(self._state, self._pending)
        self._pending = _Pending.empty()
        self.commits += 1

    async def rollback(self) -> None:
        self._pending = _Pending.empty()
        self.rollbacks += 1

    @property
    def discovery(self) -> DiscoveryRepository:
        if self._discovery is None:
            self._discovery = MemDiscoveryRepository(self._state, self._pending)
        return self._discovery

    @property
    def access(self) -> AccessRepository:
        if self._access is None:
            self._access = MemAccessRepository(self._state, self._pending)
        return self._access

    @property
    def qualification(self) -> QualificationRepository:
        if self._qualification is None:
            self._qualification = MemQualificationRepository(self._state, self._pending)
        return self._qualification

    @property
    def admission(self) -> Any:
        raise AssertionError("discovery fake exposes no admission repository")

    @property
    def source_candidates(self) -> Any:
        raise AssertionError("discovery fake exposes no candidate repository")

    @property
    def sources(self) -> Any:
        raise AssertionError("discovery fake exposes no source repository")

    @property
    def outbox(self) -> Any:
        raise AssertionError("discovery fake exposes no outbox repository")

    @property
    def processed_messages(self) -> Any:
        raise AssertionError("discovery fake exposes no processed-message repository")

    @property
    def content(self) -> Any:
        raise AssertionError("discovery fake exposes no content repository")

    @property
    def collection(self) -> Any:
        raise AssertionError("discovery fake exposes no collection repository")

    @property
    def extraction(self) -> Any:
        raise AssertionError("discovery fake exposes no extraction repository")

    @property
    def geography(self) -> Any:
        raise AssertionError("discovery fake exposes no geography repository")

    @property
    def relationships(self) -> Any:
        raise AssertionError("discovery fake exposes no relationship repository")


class MemDiscoverySpi(DarkulaSpi):
    """DarkulaSpi handing out :class:`MemDiscoveryUnitOfWork` instances."""

    def __init__(self, state: MemDiscoveryState | None = None) -> None:
        self.state = state or MemDiscoveryState.empty()

    def unit_of_work(self) -> UnitOfWork:
        return MemDiscoveryUnitOfWork(self.state)

    def is_candidate(self, candidate_id: SourceCandidateId) -> bool:
        return candidate_id in self.state.candidate_status

    def latest_status(self, candidate_id: SourceCandidateId) -> CandidateStatus | None:
        return self.state.candidate_status.get(candidate_id)


async def seed_candidate(
    spi: MemDiscoverySpi,
    candidate: SourceCandidate,
    event: SourceCandidateEventHistory,
) -> None:
    """Seed one candidate identity/status through the fake state."""
    spi.state.candidate_status[candidate.candidate_id] = candidate.status
    spi.state.candidate_events.append(event)


async def seed_observation(
    spi: MemDiscoverySpi, observation: DiscoveryObservation
) -> None:
    """Seed one committed discovery observation through the fake repository."""
    async with spi.unit_of_work() as uow:
        await uow.discovery.insert_or_get_exact(observation)
        await uow.commit()


async def seed_recon_assessment(
    spi: MemDiscoverySpi,
    assessment: ReconAssessment,
    *,
    recorded_at: datetime | None = None,
) -> None:
    """Seed a genuine recon assessment owned by its candidate."""
    _ = recorded_at
    spi.state.recon_assessments[assessment.assessment_id] = assessment.candidate_id


__all__ = [
    "MemAccessRepository",
    "MemDiscoveryRepository",
    "MemDiscoverySpi",
    "MemDiscoveryState",
    "MemDiscoveryUnitOfWork",
    "MemQualificationRepository",
    "apply_pending_discovery",
    "seed_candidate",
    "seed_observation",
    "seed_recon_assessment",
]
