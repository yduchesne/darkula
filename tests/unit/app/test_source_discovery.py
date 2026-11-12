# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic PR 17 discovery service and persistence-primitive tests.

Covers U13/U14/U15-U20 against the in-memory parity fakes: exact replay,
payload conflict, provider separation, access-pending orthogonality, atomic
qualification decisions, and cancellation rollback. No database, network,
model, or crawler is involved.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.support.discovery_fakes import (
    MemDiscoveryRepository,
    MemDiscoverySpi,
    MemDiscoveryUnitOfWork,
    seed_candidate,
    seed_observation,
    seed_recon_assessment,
)

from darkula.app.persistence import (
    ConflictError,
    DarkulaSpi,
    IntegrityError,
    NotFoundError,
    UnitOfWork,
)
from darkula.app.repositories import DiscoveryRepository
from darkula.app.source_discovery import (
    PROVIDER_AUTH_CLAIM_REASON,
    SourceDiscoveryService,
)
from darkula.domain.discovery import (
    AccessStatus,
    DiscoveryDetectionMethod,
    DiscoveryInput,
    DiscoveryObservation,
    DiscoveryReferenceKind,
    DiscoveryValidationStatus,
    QualificationDecision,
    QualificationMethod,
    QualificationOutcome,
    observation_from_input,
    qualification_decision_key,
    qualification_event_type,
)
from darkula.domain.identifiers import (
    CandidateEventId,
    DiscoveryObservationId,
    QualificationDecisionId,
    ReconAssessmentId,
    SourceCandidateId,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    Confidence,
    ReconAssessment,
    ReconDisposition,
    SourceCandidate,
    SourceCandidateEventHistory,
)

OBSERVED_AT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
INGESTED_AT = datetime(2026, 11, 11, 12, 5, tzinfo=UTC)


def valid_input(
    *,
    url: str = "https://Example.com:443/Path?Q=Value",
    provider_id: str = "ransomlook",
    record_id: str = "record-1",
    record_version: str = "v1",
    requires_auth: bool | None = None,
    detection_method: DiscoveryDetectionMethod = DiscoveryDetectionMethod.REGISTRY,
) -> DiscoveryInput:
    """Return one valid untrusted registry LOCATOR observation input."""
    return DiscoveryInput(
        provider_id=provider_id,
        provider_record_id=record_id,
        provider_record_version=record_version,
        detection_method=detection_method,
        reference_kind=DiscoveryReferenceKind.LOCATOR,
        asserted_reference=url,
        observed_at=OBSERVED_AT,
        evidence_reference="registry://ransomlook/records/record-1",
        requires_auth_claim=requires_auth,
    )


def name_only_input() -> DiscoveryInput:
    """Return one valid NAME_ONLY observation input."""
    return DiscoveryInput(
        provider_id="manual",
        provider_record_id="note-1",
        provider_record_version="1",
        detection_method=DiscoveryDetectionMethod.MANUAL,
        reference_kind=DiscoveryReferenceKind.NAME_ONLY,
        asserted_reference="Example Forum",
        observed_at=OBSERVED_AT,
        evidence_reference="operator://note/note-1",
    )


def service(
    spi: DarkulaSpi, *, clock: datetime = INGESTED_AT
) -> SourceDiscoveryService:
    """Build the deterministic ingest service over one persistence boundary."""
    return SourceDiscoveryService(persistence=spi, clock=lambda: clock)


def candidate(candidate_id: SourceCandidateId) -> SourceCandidate:
    """Return one DISCOVERED candidate with the given identity."""
    return SourceCandidate(
        candidate_id=candidate_id,
        discovered_at=OBSERVED_AT,
        discovery_method="index-discovery",
        entrypoint="http://example.invalid/a",
        status=CandidateStatus.DISCOVERED,
    )


def event_for(
    candidate_id: SourceCandidateId,
    event_type: CandidateEventType = CandidateEventType.DISCOVERED,
    *,
    event_id: CandidateEventId | None = None,
) -> SourceCandidateEventHistory:
    """Return one candidate lifecycle event.

    ``event_id`` defaults to a fresh identity (candidate seeding);
    qualification call sites must pass the decision's linked event ID.
    """
    return SourceCandidateEventHistory(
        event_id=event_id if event_id is not None else CandidateEventId.generate(),
        candidate_id=candidate_id,
        event_type=event_type,
        occurred_at=OBSERVED_AT,
    )


def linked_event(
    decision_record: QualificationDecision,
    **changes: Any,
) -> SourceCandidateEventHistory:
    """Return the event a decision must carry (its linked event identity)."""
    event = event_for(
        decision_record.candidate_id,
        CandidateEventType.QUALIFIED,
        event_id=decision_record.event_id,
    )
    return dataclasses.replace(event, **changes) if changes else event


def decision(
    *,
    candidate_id: SourceCandidateId,
    evidence: tuple[DiscoveryObservationId, ...],
    method: QualificationMethod = QualificationMethod.REGISTRY_POLICY,
    outcome: QualificationOutcome = QualificationOutcome.QUALIFIED,
    expected: CandidateStatus = CandidateStatus.DISCOVERED,
    recon_id: ReconAssessmentId | None = None,
    provider_id: str | None = "ransomlook",
    provider_record_id: str | None = "record-1",
) -> QualificationDecision:
    """Build one valid qualification decision (PR 19 shape)."""
    key = qualification_decision_key(
        candidate_id=candidate_id,
        method=method,
        policy_id="registry-trust",
        policy_version="v1",
        evidence_observation_ids=evidence,
        recon_assessment_id=recon_id,
    )
    return QualificationDecision(
        decision_id=QualificationDecisionId.generate(),
        candidate_id=candidate_id,
        method=method,
        outcome=outcome,
        evaluated_at=OBSERVED_AT,
        policy_id="registry-trust",
        policy_version="v1",
        provider_id=provider_id,
        provider_record_id=provider_record_id,
        evidence_observation_ids=evidence,
        recon_assessment_id=recon_id,
        event_id=CandidateEventId.generate(),
        expected_status=expected,
        new_status={
            QualificationOutcome.QUALIFIED: CandidateStatus.QUALIFIED,
            QualificationOutcome.RECONNAISSANCE_PENDING: (
                CandidateStatus.RECONNAISSANCE_PENDING
            ),
            QualificationOutcome.REJECTED: CandidateStatus.REJECTED,
        }[outcome],
        decision_key=key,
    )


async def stored_observation(
    spi: MemDiscoverySpi, source: DiscoveryInput | None = None
) -> DiscoveryObservation:
    """Persist one committed observation and return it."""
    observation = observation_from_input(
        source or valid_input(),
        observation_id=DiscoveryObservationId.generate(),
        ingested_at=INGESTED_AT,
    )
    await seed_observation(spi, observation)
    return observation


async def seeded_candidate(spi: MemDiscoverySpi) -> SourceCandidateId:
    """Seed one DISCOVERED candidate with its initial history event."""
    candidate_id = SourceCandidateId.generate()
    await seed_candidate(spi, candidate(candidate_id), event_for(candidate_id))
    return candidate_id


class _CancellingDiscoveryRepository(MemDiscoveryRepository):
    async def insert_or_get_exact(
        self, observation: DiscoveryObservation
    ) -> DiscoveryObservation:
        raise asyncio.CancelledError


class _CancellingUnitOfWork(MemDiscoveryUnitOfWork):
    @property
    def discovery(self) -> DiscoveryRepository:
        if self._discovery is None:
            self._discovery = _CancellingDiscoveryRepository(self._state, self._pending)
        return self._discovery


class _CancellingSpi(DarkulaSpi):
    """Persistence boundary whose ingest repository is cancelled."""

    def __init__(self) -> None:
        self.spi = MemDiscoverySpi()
        self.last_unit_of_work: _CancellingUnitOfWork | None = None

    def unit_of_work(self) -> UnitOfWork:
        instance = _CancellingUnitOfWork(self.spi.state)
        self.last_unit_of_work = instance
        return instance


class _CountingSpi(DarkulaSpi):
    """Spy proving validation happens before any transaction is opened."""

    def __init__(self) -> None:
        self.inner = MemDiscoverySpi()
        self.requests = 0

    def unit_of_work(self) -> UnitOfWork:
        self.requests += 1
        return self.inner.unit_of_work()


class TestIngest:
    """U06/U19: bounded ingest persists evidence only."""

    @pytest.mark.asyncio
    async def test_valid_locator_round_trips(self) -> None:
        spi = MemDiscoverySpi()
        result = await service(spi).ingest(
            valid_input(url="https://Example.com:443/Path?Q=Value")
        )
        assert result.normalized_locator == "https://example.com/Path?Q=Value"
        assert result.validation_status is DiscoveryValidationStatus.VALID_LOCATOR
        assert result.ingested_at == INGESTED_AT
        assert result.candidate_id is None
        assert spi.state.candidate_events == []
        assert spi.state.candidate_status == {}

    @pytest.mark.asyncio
    async def test_name_only_persists_without_locator(self) -> None:
        spi = MemDiscoverySpi()
        result = await service(spi).ingest(name_only_input())
        assert result.normalized_locator is None
        assert result.validation_status is DiscoveryValidationStatus.UNRESOLVED_NAME

    @pytest.mark.asyncio
    async def test_exact_replay_returns_original_identity(self) -> None:
        spi = MemDiscoverySpi()
        discovery = service(spi)
        first = await discovery.ingest(valid_input())
        replay = await discovery.ingest(valid_input())
        assert replay.observation_id == first.observation_id
        assert replay.ingested_at == first.ingested_at
        assert len(spi.state.observations_by_key) == 1

    @pytest.mark.asyncio
    async def test_replay_keeps_original_acceptance_time(self) -> None:
        spi = MemDiscoverySpi()
        first = await service(spi, clock=INGESTED_AT).ingest(valid_input())
        replay = await service(spi, clock=INGESTED_AT + timedelta(hours=1)).ingest(
            valid_input()
        )
        assert replay.ingested_at == first.ingested_at
        assert replay.observation_id == first.observation_id

    @pytest.mark.asyncio
    async def test_same_key_changed_observed_at_conflicts(self) -> None:
        spi = MemDiscoverySpi()
        await service(spi).ingest(valid_input())
        changed = DiscoveryInput(
            provider_id="ransomlook",
            provider_record_id="record-1",
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
            asserted_reference="https://Example.com:443/Path?Q=Value",
            observed_at=OBSERVED_AT + timedelta(seconds=1),
            evidence_reference="registry://ransomlook/records/record-1",
        )
        with pytest.raises(ConflictError):
            await service(spi).ingest(changed)
        assert len(spi.state.observations_by_key) == 1

    @pytest.mark.asyncio
    async def test_different_provider_keeps_distinct_evidence(self) -> None:
        spi = MemDiscoverySpi()
        await service(spi).ingest(valid_input())
        await service(spi).ingest(valid_input(provider_id="other-registry"))
        assert len(spi.state.observations_by_key) == 2

    @pytest.mark.asyncio
    async def test_new_record_version_keeps_distinct_evidence(self) -> None:
        spi = MemDiscoverySpi()
        await service(spi).ingest(valid_input())
        await service(spi).ingest(valid_input(record_version="v2"))
        assert len(spi.state.observations_by_key) == 2

    @pytest.mark.asyncio
    async def test_auth_claim_creates_no_access_evidence(self) -> None:
        spi = MemDiscoverySpi()
        result = await service(spi).ingest(valid_input(requires_auth=True))
        assert result.requires_auth_claim is True
        assert spi.state.access == {}

    @pytest.mark.asyncio
    async def test_validation_happens_before_any_transaction(self) -> None:
        spi = _CountingSpi()
        with pytest.raises(ValueError):
            await SourceDiscoveryService(persistence=spi).ingest(
                valid_input(url="ftp://example.com/")
            )
        assert spi.requests == 0

    @pytest.mark.asyncio
    async def test_naive_clock_rejected_before_persistence(self) -> None:
        spi = MemDiscoverySpi()
        naive_service = SourceDiscoveryService(
            persistence=spi, clock=lambda: datetime(2026, 11, 11, 12, 0)
        )
        with pytest.raises(ValueError):
            await naive_service.ingest(valid_input())
        assert spi.state.observations_by_key == {}

    @pytest.mark.asyncio
    async def test_cancellation_rolls_back_and_propagates(self) -> None:
        spi = _CancellingSpi()
        with pytest.raises(asyncio.CancelledError):
            await service(spi).ingest(valid_input())
        instance = spi.last_unit_of_work
        assert instance is not None
        assert instance.rollbacks == 1
        assert spi.spi.state.observations_by_key == {}


class TestPendingAccessEvidence:
    """U14/D10-D11: orthogonal, non-authorizing access evidence."""

    @pytest.mark.asyncio
    async def test_pending_evidence_for_existing_candidate(self) -> None:
        spi = MemDiscoverySpi()
        observation = await service(spi).ingest(valid_input(requires_auth=True))
        candidate_id = await seeded_candidate(spi)
        evidence = await service(spi).record_pending_for_existing_candidate(
            observation_id=observation.observation_id, candidate_id=candidate_id
        )
        assert evidence.status is AccessStatus.CREDENTIALS_PENDING
        assert evidence.reason_code == PROVIDER_AUTH_CLAIM_REASON
        assert evidence.candidate_id == candidate_id
        assert spi.latest_status(candidate_id) is CandidateStatus.DISCOVERED

    @pytest.mark.asyncio
    async def test_pending_evidence_replays(self) -> None:
        spi = MemDiscoverySpi()
        observation = await service(spi).ingest(valid_input(requires_auth=True))
        candidate_id = await seeded_candidate(spi)
        discovery = service(spi)
        first = await discovery.record_pending_for_existing_candidate(
            observation_id=observation.observation_id, candidate_id=candidate_id
        )
        replay = await discovery.record_pending_for_existing_candidate(
            observation_id=observation.observation_id, candidate_id=candidate_id
        )
        assert replay.access_evidence_id == first.access_evidence_id
        assert len(spi.state.access) == 1

    @pytest.mark.asyncio
    async def test_pending_requires_auth_claim(self) -> None:
        spi = MemDiscoverySpi()
        observation = await service(spi).ingest(valid_input(requires_auth=False))
        candidate_id = await seeded_candidate(spi)
        with pytest.raises(ConflictError):
            await service(spi).record_pending_for_existing_candidate(
                observation_id=observation.observation_id, candidate_id=candidate_id
            )
        assert spi.state.access == {}

    @pytest.mark.asyncio
    async def test_pending_requires_existing_candidate(self) -> None:
        spi = MemDiscoverySpi()
        observation = await service(spi).ingest(valid_input(requires_auth=True))
        with pytest.raises(NotFoundError):
            await service(spi).record_pending_for_existing_candidate(
                observation_id=observation.observation_id,
                candidate_id=SourceCandidateId.generate(),
            )
        assert spi.state.access == {}

    @pytest.mark.asyncio
    async def test_pending_requires_existing_observation(self) -> None:
        spi = MemDiscoverySpi()
        with pytest.raises(NotFoundError):
            await service(spi).record_pending_for_existing_candidate(
                observation_id=DiscoveryObservationId.generate(),
                candidate_id=SourceCandidateId.generate(),
            )


class TestQualificationPrimitive:
    """U15-U18/D14-D16: atomic, immutable qualification persistence."""

    async def _seeded(
        self,
    ) -> tuple[MemDiscoverySpi, SourceCandidateId, DiscoveryObservation]:
        spi = MemDiscoverySpi()
        observation = await stored_observation(spi)
        candidate_id = await seeded_candidate(spi)
        return spi, candidate_id, observation

    @pytest.mark.asyncio
    async def test_registry_decision_is_atomic_with_transition_and_event(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        async with spi.unit_of_work() as uow:
            persisted = await uow.qualification.decide_and_transition(
                decision_record,
                event_for(
                    candidate_id,
                    CandidateEventType.QUALIFIED,
                    event_id=decision_record.event_id,
                ),
            )
            await uow.commit()
        assert persisted.decision_key == decision_record.decision_key
        assert spi.latest_status(candidate_id) is CandidateStatus.QUALIFIED
        assert len(spi.state.candidate_events) == 2
        assert len(spi.state.decisions_by_id) == 1

    @pytest.mark.asyncio
    async def test_replay_returns_original_without_second_event(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        linked = event_for(
            candidate_id,
            CandidateEventType.QUALIFIED,
            event_id=decision_record.event_id,
        )
        async with spi.unit_of_work() as uow:
            first = await uow.qualification.decide_and_transition(
                decision_record, linked
            )
            await uow.commit()
        async with spi.unit_of_work() as uow:
            replay = await uow.qualification.decide_and_transition(
                decision_record, linked
            )
            await uow.commit()
        assert replay.decision_id == first.decision_id
        assert len(spi.state.candidate_events) == 2
        assert len(spi.state.decisions_by_id) == 1

    @pytest.mark.asyncio
    async def test_stale_expected_state_conflicts_without_effects(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id,
            evidence=(observation.observation_id,),
            expected=CandidateStatus.UNDER_RECONNAISSANCE,
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.qualification.decide_and_transition(
                    decision_record,
                    event_for(
                        candidate_id,
                        CandidateEventType.QUALIFIED,
                        event_id=decision_record.event_id,
                    ),
                )
            await uow.commit()
        assert spi.latest_status(candidate_id) is CandidateStatus.DISCOVERED
        assert len(spi.state.candidate_events) == 1
        assert spi.state.decisions_by_id == {}

    @pytest.mark.asyncio
    async def test_agent_recon_requires_real_assessment(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id,
            evidence=(observation.observation_id,),
            method=QualificationMethod.AGENT_RECON,
            provider_id=None,
            provider_record_id=None,
            recon_id=ReconAssessmentId.generate(),
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.qualification.decide_and_transition(
                    decision_record,
                    event_for(
                        candidate_id,
                        CandidateEventType.QUALIFIED,
                        event_id=decision_record.event_id,
                    ),
                )
            await uow.commit()
        assert spi.state.decisions_by_id == {}

    @pytest.mark.asyncio
    async def test_agent_recon_with_genuine_assessment(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        assessment = ReconAssessment(
            assessment_id=ReconAssessmentId.generate(),
            candidate_id=candidate_id,
            assessed_at=OBSERVED_AT,
            disposition=ReconDisposition.QUALIFY,
            confidence=Confidence(0.9),
        )
        await seed_recon_assessment(spi, assessment)
        decision_record = decision(
            candidate_id=candidate_id,
            evidence=(observation.observation_id,),
            method=QualificationMethod.AGENT_RECON,
            provider_id=None,
            provider_record_id=None,
            recon_id=assessment.assessment_id,
        )
        async with spi.unit_of_work() as uow:
            await uow.qualification.decide_and_transition(
                decision_record,
                event_for(
                    candidate_id,
                    CandidateEventType.QUALIFIED,
                    event_id=decision_record.event_id,
                ),
            )
            await uow.commit()
        assert spi.latest_status(candidate_id) is CandidateStatus.QUALIFIED

    @pytest.mark.asyncio
    async def test_unknown_evidence_rejected(self) -> None:
        spi, candidate_id, _observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id,
            evidence=(DiscoveryObservationId.generate(),),
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.qualification.decide_and_transition(
                    decision_record,
                    event_for(
                        candidate_id,
                        CandidateEventType.QUALIFIED,
                        event_id=decision_record.event_id,
                    ),
                )
            await uow.commit()
        assert spi.state.decisions_by_id == {}

    @pytest.mark.asyncio
    async def test_event_type_mismatch_rejected(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(ValueError):
                await uow.qualification.decide_and_transition(
                    decision_record,
                    event_for(
                        candidate_id,
                        CandidateEventType.REJECTED,
                        event_id=decision_record.event_id,
                    ),
                )
            await uow.commit()
        assert len(spi.state.candidate_events) == 1
        assert spi.state.decisions_by_id == {}

    @pytest.mark.asyncio
    async def test_unknown_candidate_rejected(self) -> None:
        spi = MemDiscoverySpi()
        observation = await stored_observation(spi)
        candidate_id = SourceCandidateId.generate()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(NotFoundError):
                await uow.qualification.decide_and_transition(
                    decision_record,
                    event_for(
                        candidate_id,
                        CandidateEventType.QUALIFIED,
                        event_id=decision_record.event_id,
                    ),
                )
            await uow.commit()

    def test_outcome_event_mapping(self) -> None:
        assert (
            qualification_event_type(QualificationOutcome.RECONNAISSANCE_PENDING)
            is CandidateEventType.NEEDS_MORE_RECON
        )
        assert (
            qualification_event_type(QualificationOutcome.REJECTED)
            is CandidateEventType.REJECTED
        )


class TestQualificationReplayParity:
    """F2 parity: exact replay requires matching event identity and payload."""

    async def _seeded(
        self,
    ) -> tuple[MemDiscoverySpi, SourceCandidateId, DiscoveryObservation]:
        spi = MemDiscoverySpi()
        observation = await stored_observation(spi)
        candidate_id = await seeded_candidate(spi)
        return spi, candidate_id, observation

    async def _commit(
        self,
        spi: MemDiscoverySpi,
        decision_record: QualificationDecision,
        event: SourceCandidateEventHistory,
    ) -> QualificationDecision:
        async with spi.unit_of_work() as uow:
            persisted = await uow.qualification.decide_and_transition(
                decision_record, event
            )
            await uow.commit()
        return persisted

    async def _expect_conflict(
        self,
        spi: MemDiscoverySpi,
        decision_record: QualificationDecision,
        event: SourceCandidateEventHistory,
    ) -> None:
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.qualification.decide_and_transition(decision_record, event)
            await uow.commit()

    @pytest.mark.asyncio
    async def test_exact_replay_regenerated_decision_id_returns_original(
        self,
    ) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        event = linked_event(decision_record)
        persisted = await self._commit(spi, decision_record, event)
        retry = dataclasses.replace(
            decision_record, decision_id=QualificationDecisionId.generate()
        )
        replay = await self._commit(spi, retry, event)
        assert replay.decision_id == persisted.decision_id
        assert len(spi.state.candidate_events) == 2
        assert len(spi.state.decisions_by_id) == 1

    @pytest.mark.asyncio
    async def test_changed_event_identity_on_same_key_conflicts(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        await self._commit(spi, decision_record, linked_event(decision_record))
        retry = dataclasses.replace(
            decision_record, event_id=CandidateEventId.generate()
        )
        await self._expect_conflict(spi, retry, linked_event(retry))
        assert spi.latest_status(candidate_id) is CandidateStatus.QUALIFIED
        assert len(spi.state.candidate_events) == 2
        assert len(spi.state.decisions_by_id) == 1

    @pytest.mark.asyncio
    async def test_changed_evaluated_at_on_same_key_conflicts(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        event = linked_event(decision_record)
        await self._commit(spi, decision_record, event)
        retry = dataclasses.replace(
            decision_record,
            evaluated_at=decision_record.evaluated_at + timedelta(seconds=1),
        )
        await self._expect_conflict(spi, retry, event)
        assert spi.latest_status(candidate_id) is CandidateStatus.QUALIFIED
        assert len(spi.state.candidate_events) == 2

    @pytest.mark.asyncio
    async def test_changed_event_occurred_at_on_same_key_conflicts(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        event = linked_event(decision_record)
        await self._commit(spi, decision_record, event)
        retry = dataclasses.replace(
            event, occurred_at=event.occurred_at + timedelta(seconds=1)
        )
        await self._expect_conflict(spi, decision_record, retry)
        assert len(spi.state.candidate_events) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "changed"),
        [
            ("context", {"step": "retry"}),
            ("reason", "retry reason"),
            ("provenance", "retry provenance"),
        ],
    )
    async def test_changed_event_payload_on_same_key_conflicts(
        self, field: str, changed: Any
    ) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        event = linked_event(
            decision_record,
            context={"step": "first"},
            reason="first reason",
            provenance="first provenance",
        )
        await self._commit(spi, decision_record, event)
        retry = dataclasses.replace(event, **{field: changed})
        await self._expect_conflict(spi, decision_record, retry)
        assert len(spi.state.candidate_events) == 2

    @pytest.mark.asyncio
    async def test_json_key_order_only_is_exact_replay(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        event = linked_event(decision_record, context={"alpha": 1, "beta": 2})
        persisted = await self._commit(spi, decision_record, event)
        reordered = dataclasses.replace(event, context={"beta": 2, "alpha": 1})
        replay = await self._commit(spi, decision_record, reordered)
        assert replay.decision_id == persisted.decision_id
        assert len(spi.state.candidate_events) == 2
        assert len(spi.state.decisions_by_id) == 1

    @pytest.mark.asyncio
    async def test_same_key_repeated_before_commit_replays_pending(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        event = linked_event(decision_record)
        async with spi.unit_of_work() as uow:
            first = await uow.qualification.decide_and_transition(
                decision_record, event
            )
            replay = await uow.qualification.decide_and_transition(
                decision_record, event
            )
            assert replay.decision_id == first.decision_id
            await uow.commit()
        assert len(spi.state.candidate_events) == 2
        assert len(spi.state.decisions_by_id) == 1

    @pytest.mark.asyncio
    async def test_duplicate_event_identity_on_new_key_conflicts(self) -> None:
        spi, candidate_id, observation = await self._seeded()
        decision_record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        event = linked_event(decision_record)
        await self._commit(spi, decision_record, event)
        other_candidate = await seeded_candidate(spi)
        other = decision(
            candidate_id=other_candidate, evidence=(observation.observation_id,)
        )
        colliding = dataclasses.replace(other, event_id=event.event_id)
        await self._expect_conflict(spi, colliding, linked_event(colliding))
        assert len(spi.state.candidate_events) == 3
        assert len(spi.state.decisions_by_id) == 1


class TestRegistryProvenanceParity:
    """F3 parity: a cited REGISTRY observation must prove provider/record."""

    async def _seeded(
        self,
    ) -> tuple[MemDiscoverySpi, SourceCandidateId, DiscoveryObservation]:
        spi = MemDiscoverySpi()
        observation = await stored_observation(spi)
        candidate_id = await seeded_candidate(spi)
        return spi, candidate_id, observation

    async def _accepted(
        self,
        spi: MemDiscoverySpi,
        decision_record: QualificationDecision,
    ) -> QualificationDecision:
        async with spi.unit_of_work() as uow:
            persisted = await uow.qualification.decide_and_transition(
                decision_record, linked_event(decision_record)
            )
            await uow.commit()
        return persisted

    async def _rejected(
        self,
        spi: MemDiscoverySpi,
        candidate_id: SourceCandidateId,
        decision_record: QualificationDecision,
    ) -> None:
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.qualification.decide_and_transition(
                    decision_record, linked_event(decision_record)
                )
            await uow.commit()
        assert spi.latest_status(candidate_id) is CandidateStatus.DISCOVERED
        assert spi.state.decisions_by_id == {}
        assert len(spi.state.candidate_events) == 1

    @pytest.mark.asyncio
    async def test_cited_registry_observation_matches_provider_and_record(
        self,
    ) -> None:
        spi, candidate_id, observation = await self._seeded()
        record = decision(
            candidate_id=candidate_id, evidence=(observation.observation_id,)
        )
        persisted = await self._accepted(spi, record)
        assert persisted.evidence_observation_ids == (observation.observation_id,)
        assert spi.latest_status(candidate_id) is CandidateStatus.QUALIFIED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["provider_id", "provider_record_id"])
    async def test_wrong_provider_or_record_is_rejected(self, field: str) -> None:
        spi, candidate_id, observation = await self._seeded()
        claimed = {"provider_id": "other-registry", "provider_record_id": "r2"}
        claimed[field] = "ransomlook" if field == "provider_id" else "record-1"
        record = decision(
            candidate_id=candidate_id,
            evidence=(observation.observation_id,),
            provider_id=claimed["provider_id"],
            provider_record_id=claimed["provider_record_id"],
        )
        await self._rejected(spi, candidate_id, record)

    @pytest.mark.asyncio
    async def test_matching_observation_outside_cited_set_is_rejected(self) -> None:
        spi = MemDiscoverySpi()
        matching = await stored_observation(spi)
        unrelated = await stored_observation(
            spi, valid_input(provider_id="other-registry", record_id="record-9")
        )
        candidate_id = await seeded_candidate(spi)
        record = decision(
            candidate_id=candidate_id,
            evidence=(unrelated.observation_id,),
            provider_id=matching.provider_id,
            provider_record_id=matching.provider_record_id,
        )
        await self._rejected(spi, candidate_id, record)

    @pytest.mark.asyncio
    async def test_cited_non_registry_detection_is_rejected(self) -> None:
        spi = MemDiscoverySpi()
        observed = await stored_observation(
            spi, valid_input(detection_method=DiscoveryDetectionMethod.MANUAL)
        )
        candidate_id = await seeded_candidate(spi)
        record = decision(
            candidate_id=candidate_id,
            evidence=(observed.observation_id,),
            provider_id=observed.provider_id,
            provider_record_id=observed.provider_record_id,
        )
        await self._rejected(spi, candidate_id, record)

    @pytest.mark.asyncio
    async def test_mixed_cited_evidence_with_one_match_is_accepted(self) -> None:
        spi = MemDiscoverySpi()
        matching = await stored_observation(spi)
        unrelated = await stored_observation(
            spi, valid_input(provider_id="other-registry", record_id="record-9")
        )
        candidate_id = await seeded_candidate(spi)
        record = decision(
            candidate_id=candidate_id,
            evidence=(matching.observation_id, unrelated.observation_id),
            provider_id=matching.provider_id,
            provider_record_id=matching.provider_record_id,
        )
        persisted = await self._accepted(spi, record)
        assert persisted.evidence_observation_ids == (
            matching.observation_id,
            unrelated.observation_id,
        )
        assert spi.latest_status(candidate_id) is CandidateStatus.QUALIFIED

    @pytest.mark.asyncio
    async def test_agent_recon_needs_no_registry_provenance(self) -> None:
        spi = MemDiscoverySpi()
        observed = await stored_observation(
            spi, valid_input(detection_method=DiscoveryDetectionMethod.MANUAL)
        )
        candidate_id = await seeded_candidate(spi)
        assessment = ReconAssessment(
            assessment_id=ReconAssessmentId.generate(),
            candidate_id=candidate_id,
            assessed_at=OBSERVED_AT,
            disposition=ReconDisposition.QUALIFY,
            confidence=Confidence(0.9),
        )
        await seed_recon_assessment(spi, assessment)
        record = decision(
            candidate_id=candidate_id,
            evidence=(observed.observation_id,),
            method=QualificationMethod.AGENT_RECON,
            provider_id=None,
            provider_record_id=None,
            recon_id=assessment.assessment_id,
        )
        persisted = await self._accepted(spi, record)
        assert persisted.recon_assessment_id == assessment.assessment_id
        assert spi.latest_status(candidate_id) is CandidateStatus.QUALIFIED
