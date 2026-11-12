# SPDX-License-Identifier: AGPL-3.0-only
"""PR 17 real-PostgreSQL vertical slices (I02-I18).

Every production-path assertion goes through
``SourceDiscoveryService -> PostgresDarkulaSpi -> PostgresUnitOfWork ->
versioned stored function -> PostgreSQL``. Direct SQL is restricted to
setup/verification (counts, status inspection) as in the rest of the
integration suite.

Covered: exact replay, concurrent insert/conflict, provider/record-version
separation, name-only observations, unwritten access evidence for unadmitted
candidates, orthogonal pending evidence, rollback, and the atomic immutable
qualification decision primitive.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from darkula.app.persistence import (
    ConflictError,
    IntegrityError,
    NotFoundError,
)
from darkula.app.source_discovery import SourceDiscoveryService
from darkula.domain.discovery import (
    DiscoveryDetectionMethod,
    DiscoveryInput,
    DiscoveryObservation,
    DiscoveryReferenceKind,
    QualificationDecision,
    QualificationMethod,
    QualificationOutcome,
    qualification_decision_key,
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
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from tests.integration.conftest import _raw_connect

pytestmark = pytest.mark.integration

OBSERVED_AT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
INGESTED_AT = datetime(2026, 11, 11, 12, 5, tzinfo=UTC)


def _input(
    *,
    url: str = "https://Example.com:443/Path?Q=Value",
    provider_id: str = "ransomlook",
    record_id: str = "record-1",
    record_version: str = "v1",
    requires_auth: bool | None = None,
    observed_at: datetime = OBSERVED_AT,
    detection_method: DiscoveryDetectionMethod = DiscoveryDetectionMethod.REGISTRY,
) -> DiscoveryInput:
    return DiscoveryInput(
        provider_id=provider_id,
        provider_record_id=record_id,
        provider_record_version=record_version,
        detection_method=detection_method,
        reference_kind=DiscoveryReferenceKind.LOCATOR,
        asserted_reference=url,
        observed_at=observed_at,
        evidence_reference="registry://ransomlook/records/record-1",
        requires_auth_claim=requires_auth,
    )


def _name_only() -> DiscoveryInput:
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


def _service(spi: PostgresDarkulaSpi) -> SourceDiscoveryService:
    return SourceDiscoveryService(persistence=spi, clock=lambda: INGESTED_AT)


def _count(database_settings: Any, table: str) -> int:
    """Test-only row count for a known table name."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _candidate_status(database_settings: Any, candidate_id: SourceCandidateId) -> str:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM source_candidate WHERE id = %s",
                (str(candidate_id),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _event_count(database_settings: Any, candidate_id: SourceCandidateId) -> int:
    """Test-only count of the candidate's lifecycle history rows."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM source_candidate_event_history "
                "WHERE candidate_id = %s",
                (str(candidate_id),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _decision_count_for(database_settings: Any, candidate_id: SourceCandidateId) -> int:
    """Test-only count of the candidate's immutable qualification decisions."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM qualification_decision WHERE candidate_id = %s",
                (str(candidate_id),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _event_persisted(database_settings: Any, event_id: CandidateEventId) -> bool:
    """Test-only existence check for one lifecycle history identity."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM source_candidate_event_history WHERE event_id = %s",
                (str(event_id),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return row is not None


async def _seed_candidate(
    spi: PostgresDarkulaSpi,
    *,
    status: CandidateStatus = CandidateStatus.DISCOVERED,
) -> SourceCandidateId:
    candidate_id = SourceCandidateId.generate()
    candidate = SourceCandidate(
        candidate_id=candidate_id,
        discovered_at=OBSERVED_AT,
        discovery_method="index-discovery",
        entrypoint="http://example.invalid/a",
        status=status,
    )
    event = SourceCandidateEventHistory(
        event_id=CandidateEventId.generate(),
        candidate_id=candidate_id,
        event_type=CandidateEventType.DISCOVERED,
        occurred_at=OBSERVED_AT,
    )
    async with spi.unit_of_work() as uow:
        await uow.source_candidates.create(candidate, event=event)
        await uow.commit()
    return candidate_id


async def _reload(
    spi: PostgresDarkulaSpi, observation_id: DiscoveryObservationId
) -> DiscoveryObservation | None:
    async with spi.unit_of_work() as uow:
        observation = await uow.discovery.get(observation_id)
        await uow.commit()
        return observation


class TestDiscoveryIngestion:
    @pytest.mark.asyncio
    async def test_valid_locator_round_trips_through_production_path(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input())
        reloaded = await _reload(spi, observed.observation_id)
        assert reloaded is not None
        assert reloaded.normalized_locator == "https://example.com/Path?Q=Value"
        assert reloaded.ingested_at == INGESTED_AT
        assert reloaded.observed_at == OBSERVED_AT
        assert reloaded.candidate_id is None
        assert reloaded.semantic_key == observed.semantic_key
        assert _count(database_settings, "discovery_observation") == 1

    @pytest.mark.asyncio
    async def test_exact_replay_returns_original_row(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        first = await _service(spi).ingest(_input())
        replay = await _service(spi).ingest(_input())
        assert replay.observation_id == first.observation_id
        assert replay.ingested_at == first.ingested_at
        assert _count(database_settings, "discovery_observation") == 1

    @pytest.mark.asyncio
    async def test_changed_observed_at_conflicts(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        await _service(spi).ingest(_input())
        with pytest.raises(ConflictError):
            await _service(spi).ingest(
                _input(observed_at=OBSERVED_AT + timedelta(seconds=1))
            )
        assert _count(database_settings, "discovery_observation") == 1

    @pytest.mark.asyncio
    async def test_concurrent_identical_ingest_converges(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        first, second = await asyncio.gather(
            _service(spi).ingest(_input()),
            _service(spi).ingest(_input()),
        )
        assert first.observation_id == second.observation_id
        assert _count(database_settings, "discovery_observation") == 1

    @pytest.mark.asyncio
    async def test_concurrent_conflicting_ingest_has_one_winner(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        results = await asyncio.gather(
            _service(spi).ingest(_input()),
            _service(spi).ingest(
                _input(observed_at=OBSERVED_AT + timedelta(seconds=1))
            ),
            return_exceptions=True,
        )
        conflicts = [item for item in results if isinstance(item, ConflictError)]
        winners = [item for item in results if isinstance(item, DiscoveryObservation)]
        assert len(winners) == 1
        assert len(conflicts) == 1
        assert _count(database_settings, "discovery_observation") == 1

    @pytest.mark.asyncio
    async def test_same_locator_across_providers_stays_distinct(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        first = await _service(spi).ingest(_input())
        second = await _service(spi).ingest(_input(provider_id="other-registry"))
        assert first.observation_id != second.observation_id
        assert first.normalized_locator == second.normalized_locator
        assert _count(database_settings, "discovery_observation") == 2

    @pytest.mark.asyncio
    async def test_new_record_version_is_immutable_history(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        await _service(spi).ingest(_input())
        await _service(spi).ingest(_input(record_version="v2"))
        assert _count(database_settings, "discovery_observation") == 2

    @pytest.mark.asyncio
    async def test_name_only_has_no_locator_or_candidate_effects(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_name_only())
        assert observed.normalized_locator is None
        assert observed.validation_status.value == "UNRESOLVED_NAME"
        assert _count(database_settings, "discovery_observation") == 1
        assert _count(database_settings, "source_candidate") == 0
        assert _count(database_settings, "source") == 0
        assert _count(database_settings, "collection_policy") == 0
        assert _count(database_settings, "access_evidence") == 0

    @pytest.mark.asyncio
    async def test_auth_claim_without_candidate_writes_no_evidence(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input(requires_auth=True))
        assert observed.requires_auth_claim is True
        assert _count(database_settings, "access_evidence") == 0

    @pytest.mark.asyncio
    async def test_rollback_discards_observation(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        from darkula.domain.discovery import observation_from_input

        attempt = observation_from_input(
            _input(),
            observation_id=DiscoveryObservationId.generate(),
            ingested_at=INGESTED_AT,
        )
        async with spi.unit_of_work() as uow:
            await uow.discovery.insert_or_get_exact(attempt)
            await uow.rollback()
        assert _count(database_settings, "discovery_observation") == 0


class TestPendingAccessEvidence:
    @pytest.mark.asyncio
    async def test_pending_evidence_is_orthogonal_to_lifecycle(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input(requires_auth=True))
        candidate_id = await _seed_candidate(spi)
        evidence = await _service(spi).record_pending_for_existing_candidate(
            observation_id=observed.observation_id, candidate_id=candidate_id
        )
        assert evidence.status.value == "CREDENTIALS_PENDING"
        assert evidence.reason_code == "PROVIDER_AUTH_CLAIM"
        async with spi.unit_of_work() as uow:
            latest = await uow.access.latest_for_candidate(candidate_id)
            await uow.commit()
        assert latest is not None
        assert latest.access_evidence_id == evidence.access_evidence_id
        assert _candidate_status(database_settings, candidate_id) == "DISCOVERED"

    @pytest.mark.asyncio
    async def test_pending_evidence_replays_without_second_row(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input(requires_auth=True))
        candidate_id = await _seed_candidate(spi)
        service = _service(spi)
        first = await service.record_pending_for_existing_candidate(
            observation_id=observed.observation_id, candidate_id=candidate_id
        )
        replay = await service.record_pending_for_existing_candidate(
            observation_id=observed.observation_id, candidate_id=candidate_id
        )
        assert replay.access_evidence_id == first.access_evidence_id
        assert _count(database_settings, "access_evidence") == 1
        assert _candidate_status(database_settings, candidate_id) == "DISCOVERED"

    @pytest.mark.asyncio
    async def test_pending_requires_claim_and_candidate(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        no_claim = await _service(spi).ingest(
            _input(record_id="record-1", requires_auth=False)
        )
        claimed = await _service(spi).ingest(
            _input(record_id="record-2", requires_auth=True)
        )
        candidate_id = await _seed_candidate(spi)
        with pytest.raises(ConflictError):
            await _service(spi).record_pending_for_existing_candidate(
                observation_id=no_claim.observation_id, candidate_id=candidate_id
            )
        with pytest.raises(NotFoundError):
            await _service(spi).record_pending_for_existing_candidate(
                observation_id=claimed.observation_id,
                candidate_id=SourceCandidateId.generate(),
            )
        assert _count(database_settings, "access_evidence") == 0


def _decision(
    *,
    candidate_id: SourceCandidateId,
    observation_id: DiscoveryObservationId | None = None,
    evidence: tuple[DiscoveryObservationId, ...] | None = None,
    method: str = "REGISTRY_POLICY",
    outcome: str = "QUALIFIED",
    expected: CandidateStatus = CandidateStatus.DISCOVERED,
    recon_id: ReconAssessmentId | None = None,
    provider_id: str | None = "ransomlook",
    provider_record_id: str | None = "record-1",
) -> QualificationDecision:
    qualification_method = QualificationMethod(method)
    qualification_outcome = QualificationOutcome(outcome)
    if evidence is None:
        assert observation_id is not None, "a decision needs cited evidence"
        evidence = (observation_id,)
    return QualificationDecision(
        decision_id=QualificationDecisionId.generate(),
        candidate_id=candidate_id,
        method=qualification_method,
        outcome=qualification_outcome,
        evaluated_at=INGESTED_AT,
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
        }[qualification_outcome],
        decision_key=qualification_decision_key(
            candidate_id=candidate_id,
            method=qualification_method,
            policy_id="registry-trust",
            policy_version="v1",
            evidence_observation_ids=evidence,
            recon_assessment_id=recon_id,
        ),
    )


def _event(
    decision: QualificationDecision,
    event_type: CandidateEventType = CandidateEventType.QUALIFIED,
) -> SourceCandidateEventHistory:
    return SourceCandidateEventHistory(
        event_id=decision.event_id,
        candidate_id=decision.candidate_id,
        event_type=event_type,
        occurred_at=INGESTED_AT,
    )


class TestQualificationPrimitive:
    @pytest.mark.asyncio
    async def test_registry_decision_is_atomic(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input())
        candidate_id = await _seed_candidate(spi)
        decision = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        async with spi.unit_of_work() as uow:
            persisted = await uow.qualification.decide_and_transition(
                decision, _event(decision)
            )
            await uow.commit()
        assert persisted.decision_key == decision.decision_key
        assert _candidate_status(database_settings, candidate_id) == "QUALIFIED"
        assert _count(database_settings, "qualification_decision") == 1
        assert _count(database_settings, "recon_assessment") == 0
        async with spi.unit_of_work() as uow:
            history = await uow.source_candidates.list_history(candidate_id)
            decisions = await uow.qualification.list_for_candidate(candidate_id)
            await uow.commit()
        assert [item.event_type.value for item in history] == [
            "DISCOVERED",
            "QUALIFIED",
        ]
        assert len(decisions) == 1

    @pytest.mark.asyncio
    async def test_replay_appends_no_second_event(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input())
        candidate_id = await _seed_candidate(spi)
        decision = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        async with spi.unit_of_work() as uow:
            await uow.qualification.decide_and_transition(decision, _event(decision))
            await uow.commit()
        retry = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        async with spi.unit_of_work() as uow:
            replay = await uow.qualification.decide_and_transition(
                decision, _event(decision)
            )
            await uow.commit()
        assert replay.decision_id == decision.decision_id
        assert retry.decision_key == decision.decision_key
        assert _count(database_settings, "qualification_decision") == 1
        async with spi.unit_of_work() as uow:
            history = await uow.source_candidates.list_history(candidate_id)
            await uow.commit()
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_stale_expected_state_conflicts_without_partial_rows(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input())
        candidate_id = await _seed_candidate(spi)
        first = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        async with spi.unit_of_work() as uow:
            await uow.qualification.decide_and_transition(first, _event(first))
            await uow.commit()
        stale = _decision(
            candidate_id=candidate_id,
            observation_id=observed.observation_id,
            expected=CandidateStatus.DISCOVERED,
            outcome="REJECTED",
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.qualification.decide_and_transition(
                    stale, _event(stale, CandidateEventType.REJECTED)
                )
            await uow.commit()
        assert _count(database_settings, "qualification_decision") == 1
        assert _candidate_status(database_settings, candidate_id) == "QUALIFIED"

    @pytest.mark.asyncio
    async def test_agent_recon_without_real_assessment_rejected(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input())
        candidate_id = await _seed_candidate(spi)
        decision = _decision(
            candidate_id=candidate_id,
            observation_id=observed.observation_id,
            method="AGENT_RECON",
            recon_id=ReconAssessmentId.generate(),
            provider_id=None,
            provider_record_id=None,
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.qualification.decide_and_transition(
                    decision, _event(decision)
                )
            await uow.commit()
        assert _count(database_settings, "qualification_decision") == 0
        assert _candidate_status(database_settings, candidate_id) == "DISCOVERED"

    @pytest.mark.asyncio
    async def test_agent_recon_with_genuine_assessment(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input())
        candidate_id = await _seed_candidate(spi)
        assessment = ReconAssessment(
            assessment_id=ReconAssessmentId.generate(),
            candidate_id=candidate_id,
            assessed_at=OBSERVED_AT,
            disposition=ReconDisposition.QUALIFY,
            confidence=Confidence(0.9),
        )
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.append_recon_assessment(assessment)
            await uow.commit()
        decision = _decision(
            candidate_id=candidate_id,
            observation_id=observed.observation_id,
            method="AGENT_RECON",
            recon_id=assessment.assessment_id,
            provider_id=None,
            provider_record_id=None,
        )
        async with spi.unit_of_work() as uow:
            persisted = await uow.qualification.decide_and_transition(
                decision, _event(decision)
            )
            await uow.commit()
        assert persisted.recon_assessment_id == assessment.assessment_id
        assert _candidate_status(database_settings, candidate_id) == "QUALIFIED"

    @pytest.mark.asyncio
    async def test_bounded_error_does_not_leak_values(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        observed = await _service(spi).ingest(_input(requires_auth=True))
        missing_candidate = SourceCandidateId.generate()
        with pytest.raises(NotFoundError) as excinfo:
            await _service(spi).record_pending_for_existing_candidate(
                observation_id=observed.observation_id,
                candidate_id=missing_candidate,
            )
        message = str(excinfo.value)
        assert str(missing_candidate) not in message
        assert "ransomlook" not in message
        assert "source_candidate" not in message


async def _commit_decision(
    spi: PostgresDarkulaSpi,
    decision: QualificationDecision,
    event: SourceCandidateEventHistory | None = None,
) -> QualificationDecision:
    """Commit one qualification decision (and its linked event) for a test."""
    async with spi.unit_of_work() as uow:
        persisted = await uow.qualification.decide_and_transition(
            decision, _event(decision) if event is None else event
        )
        await uow.commit()
    return persisted


async def _expect_conflict_then_commit(
    spi: PostgresDarkulaSpi,
    decision: QualificationDecision,
    event: SourceCandidateEventHistory,
) -> None:
    """Assert a typed replay conflict, then prove the caller's commit is inert."""
    async with spi.unit_of_work() as uow:
        with pytest.raises(ConflictError):
            await uow.qualification.decide_and_transition(decision, event)
        await uow.commit()


class TestQualificationAtomicity:
    """F1: the transition, event, and decision share one rollback boundary."""

    @pytest.mark.asyncio
    async def test_duplicate_decision_pk_conflict_rolls_back_candidate_event_and_decision(  # noqa: E501
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(_input())
        candidate_a = await _seed_candidate(spi)
        candidate_b = await _seed_candidate(spi)
        decision_a = _decision(
            candidate_id=candidate_a, observation_id=observed.observation_id
        )
        await _commit_decision(spi, decision_a)

        decision_b = _decision(
            candidate_id=candidate_b, observation_id=observed.observation_id
        )
        # A distinct semantic key/event for B, colliding only on the decision PK
        # inserted *after* the candidate UPDATE inside the stored function.
        forced = dataclasses.replace(decision_b, decision_id=decision_a.decision_id)
        assert forced.decision_key != decision_a.decision_key
        assert forced.event_id != decision_a.event_id

        await _expect_conflict_then_commit(spi, forced, _event(forced))

        assert _candidate_status(database_settings, candidate_b) == "DISCOVERED"
        assert _event_count(database_settings, candidate_b) == 1
        assert _decision_count_for(database_settings, candidate_b) == 0
        assert not _event_persisted(database_settings, forced.event_id)
        assert _count(database_settings, "qualification_decision") == 1
        assert _candidate_status(database_settings, candidate_a) == "QUALIFIED"
        assert _event_count(database_settings, candidate_a) == 2


class TestQualificationReplay:
    """F2: exact replay requires matching event identity and payload."""

    async def _seeded(
        self, spi: PostgresDarkulaSpi
    ) -> tuple[SourceCandidateId, DiscoveryObservation]:
        observed = await _service(spi).ingest(_input())
        candidate_id = await _seed_candidate(spi)
        return candidate_id, observed

    @pytest.mark.asyncio
    async def test_exact_replay_regenerated_decision_id_returns_original(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        candidate_id, observed = await self._seeded(spi)
        decision = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        persisted = await _commit_decision(spi, decision)
        retry = dataclasses.replace(
            decision, decision_id=QualificationDecisionId.generate()
        )
        replay = await _commit_decision(spi, retry, _event(retry))
        assert replay.decision_id == persisted.decision_id
        assert replay.evaluated_at == persisted.evaluated_at
        assert _count(database_settings, "qualification_decision") == 1
        assert _event_count(database_settings, candidate_id) == 2

    @pytest.mark.asyncio
    async def test_changed_event_id_on_retry_conflicts_without_effects(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        candidate_id, observed = await self._seeded(spi)
        decision = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        await _commit_decision(spi, decision)
        retry = dataclasses.replace(decision, event_id=CandidateEventId.generate())
        await _expect_conflict_then_commit(spi, retry, _event(retry))
        assert _count(database_settings, "qualification_decision") == 1
        assert _event_count(database_settings, candidate_id) == 2
        assert not _event_persisted(database_settings, retry.event_id)

    @pytest.mark.asyncio
    async def test_changed_evaluated_at_on_retry_conflicts_without_effects(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        candidate_id, observed = await self._seeded(spi)
        decision = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        await _commit_decision(spi, decision)
        retry = dataclasses.replace(
            decision, evaluated_at=decision.evaluated_at + timedelta(seconds=1)
        )
        await _expect_conflict_then_commit(spi, retry, _event(decision))
        assert _count(database_settings, "qualification_decision") == 1
        assert _event_count(database_settings, candidate_id) == 2

    @pytest.mark.asyncio
    async def test_changed_event_occurred_at_conflicts_without_effects(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        candidate_id, observed = await self._seeded(spi)
        decision = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        original = _event(decision)
        await _commit_decision(spi, decision, original)
        retry = dataclasses.replace(
            original, occurred_at=original.occurred_at + timedelta(seconds=1)
        )
        await _expect_conflict_then_commit(spi, decision, retry)
        assert _count(database_settings, "qualification_decision") == 1
        assert _event_count(database_settings, candidate_id) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "changed"),
        [
            ("context", {"step": "retry"}),
            ("reason", "retry reason"),
            ("provenance", "retry provenance"),
        ],
    )
    async def test_changed_event_payload_conflicts_without_effects(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: Any,
        field: str,
        changed: Any,
    ) -> None:
        candidate_id, observed = await self._seeded(spi)
        decision = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        original = dataclasses.replace(
            _event(decision),
            context={"step": "first"},
            reason="first reason",
            provenance="first provenance",
        )
        await _commit_decision(spi, decision, original)
        retry = dataclasses.replace(original, **{field: changed})
        await _expect_conflict_then_commit(spi, decision, retry)
        assert _count(database_settings, "qualification_decision") == 1
        assert _event_count(database_settings, candidate_id) == 2

    @pytest.mark.asyncio
    async def test_jsonb_key_order_only_replays(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        candidate_id, observed = await self._seeded(spi)
        decision = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        original = dataclasses.replace(
            _event(decision), context={"alpha": 1, "beta": 2}
        )
        persisted = await _commit_decision(spi, decision, original)
        reordered = dataclasses.replace(original, context={"beta": 2, "alpha": 1})
        replay = await _commit_decision(spi, decision, reordered)
        assert replay.decision_id == persisted.decision_id
        assert _count(database_settings, "qualification_decision") == 1
        assert _event_count(database_settings, candidate_id) == 2


class TestRegistryProvenance:
    """F3: REGISTRY_POLICY needs a cited REGISTRY provider/record match."""

    async def _seeded(
        self, spi: PostgresDarkulaSpi
    ) -> tuple[SourceCandidateId, DiscoveryObservation]:
        observed = await _service(spi).ingest(_input())
        candidate_id = await _seed_candidate(spi)
        return candidate_id, observed

    @pytest.mark.asyncio
    async def test_cited_registry_observation_matches_provider_and_record(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        candidate_id, observed = await self._seeded(spi)
        decision = _decision(
            candidate_id=candidate_id, observation_id=observed.observation_id
        )
        persisted = await _commit_decision(spi, decision)
        assert persisted.evidence_observation_ids == (observed.observation_id,)
        assert _candidate_status(database_settings, candidate_id) == "QUALIFIED"
        assert _count(database_settings, "qualification_decision") == 1
        assert _event_count(database_settings, candidate_id) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["provider_id", "provider_record_id"])
    async def test_wrong_provider_or_record_is_rejected(
        self, spi: PostgresDarkulaSpi, database_settings: Any, field: str
    ) -> None:
        candidate_id, observed = await self._seeded(spi)
        claimed = {
            "provider_id": "other-registry",
            "provider_record_id": "record-2",
        }
        claimed[field] = "ransomlook" if field == "provider_id" else "record-1"
        decision = _decision(
            candidate_id=candidate_id,
            observation_id=observed.observation_id,
            provider_id=claimed["provider_id"],
            provider_record_id=claimed["provider_record_id"],
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.qualification.decide_and_transition(
                    decision, _event(decision)
                )
            await uow.commit()
        assert _candidate_status(database_settings, candidate_id) == "DISCOVERED"
        assert _count(database_settings, "qualification_decision") == 0
        assert _event_count(database_settings, candidate_id) == 1

    @pytest.mark.asyncio
    async def test_matching_observation_outside_cited_set_is_rejected(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        matching = await _service(spi).ingest(_input())
        unrelated = await _service(spi).ingest(
            _input(provider_id="other-registry", record_id="record-9")
        )
        candidate_id = await _seed_candidate(spi)
        decision = _decision(
            candidate_id=candidate_id,
            observation_id=unrelated.observation_id,
            provider_id=matching.provider_id,
            provider_record_id=matching.provider_record_id,
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.qualification.decide_and_transition(
                    decision, _event(decision)
                )
            await uow.commit()
        assert _candidate_status(database_settings, candidate_id) == "DISCOVERED"
        assert _count(database_settings, "qualification_decision") == 0
        assert _event_count(database_settings, candidate_id) == 1

    @pytest.mark.asyncio
    async def test_cited_non_registry_detection_is_rejected(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(
            _input(detection_method=DiscoveryDetectionMethod.MANUAL)
        )
        candidate_id = await _seed_candidate(spi)
        decision = _decision(
            candidate_id=candidate_id,
            observation_id=observed.observation_id,
            provider_id=observed.provider_id,
            provider_record_id=observed.provider_record_id,
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.qualification.decide_and_transition(
                    decision, _event(decision)
                )
            await uow.commit()
        assert _candidate_status(database_settings, candidate_id) == "DISCOVERED"
        assert _count(database_settings, "qualification_decision") == 0
        assert _event_count(database_settings, candidate_id) == 1

    @pytest.mark.asyncio
    async def test_mixed_cited_evidence_with_one_match_is_accepted(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        matching = await _service(spi).ingest(_input())
        unrelated = await _service(spi).ingest(
            _input(provider_id="other-registry", record_id="record-9")
        )
        candidate_id = await _seed_candidate(spi)
        decision = _decision(
            candidate_id=candidate_id,
            evidence=(matching.observation_id, unrelated.observation_id),
            provider_id=matching.provider_id,
            provider_record_id=matching.provider_record_id,
        )
        persisted = await _commit_decision(spi, decision)
        assert persisted.evidence_observation_ids == (
            matching.observation_id,
            unrelated.observation_id,
        )
        assert _candidate_status(database_settings, candidate_id) == "QUALIFIED"
        assert _count(database_settings, "qualification_decision") == 1

    @pytest.mark.asyncio
    async def test_agent_recon_needs_no_registry_provenance(
        self, spi: PostgresDarkulaSpi, database_settings: Any
    ) -> None:
        observed = await _service(spi).ingest(
            _input(detection_method=DiscoveryDetectionMethod.MANUAL)
        )
        candidate_id = await _seed_candidate(spi)
        assessment = ReconAssessment(
            assessment_id=ReconAssessmentId.generate(),
            candidate_id=candidate_id,
            assessed_at=OBSERVED_AT,
            disposition=ReconDisposition.QUALIFY,
            confidence=Confidence(0.9),
        )
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.append_recon_assessment(assessment)
            await uow.commit()
        decision = _decision(
            candidate_id=candidate_id,
            observation_id=observed.observation_id,
            method="AGENT_RECON",
            recon_id=assessment.assessment_id,
            provider_id=None,
            provider_record_id=None,
        )
        persisted = await _commit_decision(spi, decision)
        assert persisted.recon_assessment_id == assessment.assessment_id
        assert _candidate_status(database_settings, candidate_id) == "QUALIFIED"
