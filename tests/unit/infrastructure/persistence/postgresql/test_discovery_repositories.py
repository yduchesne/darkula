# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the PR 17 PostgreSQL repositories using fake connections.

Proves that the adapter (a) issues only fixed stored-function invocations,
(b) marshals parameters and maps result rows exactly, and (c) surfaces
controlled outcomes as typed bounded errors. No PostgreSQL is required.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Self

import pytest

from darkula.app.persistence import (
    ConflictError,
    IntegrityError,
    MappingError,
    NotFoundError,
    PersistenceError,
)
from darkula.domain.discovery import (
    AccessEvidence,
    AccessStatus,
    DiscoveryDetectionMethod,
    DiscoveryInput,
    DiscoveryReferenceKind,
    QualificationDecision,
    QualificationMethod,
    QualificationOutcome,
    observation_from_input,
    qualification_decision_key,
)
from darkula.domain.identifiers import (
    AccessEvidenceId,
    CandidateEventId,
    DiscoveryObservationId,
    QualificationDecisionId,
    SourceCandidateId,
    SourceEndpointId,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    SourceCandidateEventHistory,
)
from darkula.infrastructure.persistence.postgresql.discovery_repository import (
    PostgresAccessRepository,
    PostgresDiscoveryRepository,
)
from darkula.infrastructure.persistence.postgresql.qualification_repository import (
    PostgresQualificationRepository,
)

_MOMENT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
_CANDIDATE_ID = SourceCandidateId.generate()
_OBSERVATION_ID = DiscoveryObservationId.generate()


class _Cursor:
    """Async cursor recording execution and serving queued rows in order."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = list(rows or [])
        self.executed_sql: list[str] = []
        self.executed_params: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed_sql.append(sql)
        self.executed_params.append(params or ())

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows.pop(0) if self.rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        rows = list(self.rows)
        self.rows.clear()
        return rows


class _Scope:
    """Deterministic ExecutionScope under test (no real DB connection)."""

    def __init__(self, cursor: _Cursor, *, closed: bool = False) -> None:
        self.cursor = cursor
        self.closed = closed

    def ensure_open(self) -> None:
        if self.closed:
            raise PersistenceError("this unit of work is closed")

    def connection(self) -> Any:
        self.ensure_open()
        cursor = self.cursor

        class _Conn:
            def cursor(self) -> _Cursor:
                return cursor

        return _Conn()


def _input(*, requires_auth: bool | None = None) -> DiscoveryInput:
    return DiscoveryInput(
        provider_id="ransomlook",
        provider_record_id="record-1",
        provider_record_version="v1",
        detection_method=DiscoveryDetectionMethod.REGISTRY,
        reference_kind=DiscoveryReferenceKind.LOCATOR,
        asserted_reference="https://Example.com:443/a?b=c",
        observed_at=_MOMENT,
        evidence_reference="registry://ransomlook/record-1",
        requires_auth_claim=requires_auth,
    )


def _observation_row() -> tuple[Any, ...]:
    observation = observation_from_input(
        _input(), observation_id=_OBSERVATION_ID, ingested_at=_MOMENT
    )
    return (
        observation.observation_id.value,
        observation.provider_id,
        observation.provider_record_id,
        observation.provider_record_version,
        observation.detection_method.value,
        observation.reference_kind.value,
        observation.asserted_reference,
        observation.normalized_locator,
        observation.validation_status.value,
        None,
        observation.requires_auth_claim,
        None,
        observation.evidence_reference,
        None,
        observation.observed_at,
        observation.ingested_at,
        None,
        observation.semantic_key,
    )


def _access_row(
    status: AccessStatus = AccessStatus.CREDENTIALS_PENDING,
) -> tuple[Any, ...]:
    return (
        AccessEvidenceId.generate().value,
        _OBSERVATION_ID.value,
        _CANDIDATE_ID.value,
        None,
        status.value,
        "PROVIDER_AUTH_CLAIM",
        _MOMENT,
        "registry://ransomlook/record-1",
    )


def _decision() -> QualificationDecision:
    evidence = (_OBSERVATION_ID,)
    return QualificationDecision(
        decision_id=QualificationDecisionId.generate(),
        candidate_id=_CANDIDATE_ID,
        method=QualificationMethod.REGISTRY_POLICY,
        outcome=QualificationOutcome.QUALIFIED,
        evaluated_at=_MOMENT,
        policy_id="registry-trust",
        policy_version="v1",
        provider_id="ransomlook",
        provider_record_id="record-1",
        evidence_observation_ids=evidence,
        recon_assessment_id=None,
        event_id=CandidateEventId.generate(),
        expected_status=CandidateStatus.DISCOVERED,
        new_status=CandidateStatus.QUALIFIED,
        decision_key=qualification_decision_key(
            candidate_id=_CANDIDATE_ID,
            method=QualificationMethod.REGISTRY_POLICY,
            policy_id="registry-trust",
            policy_version="v1",
            evidence_observation_ids=evidence,
            recon_assessment_id=None,
        ),
    )


def _decision_row(decision: QualificationDecision) -> tuple[Any, ...]:
    return (
        decision.decision_id.value,
        decision.candidate_id.value,
        decision.method.value,
        decision.outcome.value,
        decision.evaluated_at,
        decision.policy_id,
        decision.policy_version,
        decision.provider_id,
        decision.provider_record_id,
        None,
        decision.event_id.value,
        decision.expected_status.value,
        decision.new_status.value,
        decision.decision_key,
        [item.value for item in decision.evidence_observation_ids],
    )


class TestDiscoveryRepository:
    @pytest.mark.asyncio
    async def test_insert_returns_persisted_row(self) -> None:
        observation = observation_from_input(
            _input(), observation_id=_OBSERVATION_ID, ingested_at=_MOMENT
        )
        cursor = _Cursor([("added",), _observation_row()])
        repository = PostgresDiscoveryRepository(_Scope(cursor))
        result = await repository.insert_or_get_exact(observation)
        assert result.observation_id == _OBSERVATION_ID
        assert result.semantic_key == observation.semantic_key
        assert cursor.executed_sql[0].startswith(
            "SELECT * FROM discovery_observation_insert_v1("
        )
        assert cursor.executed_params[0][0] == str(_OBSERVATION_ID)

    @pytest.mark.asyncio
    async def test_insert_conflict_raises(self) -> None:
        observation = observation_from_input(
            _input(), observation_id=_OBSERVATION_ID, ingested_at=_MOMENT
        )
        repository = PostgresDiscoveryRepository(_Scope(_Cursor([("conflict",)])))
        with pytest.raises(ConflictError):
            await repository.insert_or_get_exact(observation)

    @pytest.mark.asyncio
    async def test_insert_unexpected_outcome_maps_boundedly(self) -> None:
        observation = observation_from_input(
            _input(), observation_id=_OBSERVATION_ID, ingested_at=_MOMENT
        )
        repository = PostgresDiscoveryRepository(_Scope(_Cursor([("weird",)])))
        with pytest.raises(MappingError):
            await repository.insert_or_get_exact(observation)

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self) -> None:
        repository = PostgresDiscoveryRepository(_Scope(_Cursor()))
        assert await repository.get(_OBSERVATION_ID) is None

    @pytest.mark.asyncio
    async def test_get_by_key_maps_row(self) -> None:
        repository = PostgresDiscoveryRepository(_Scope(_Cursor([_observation_row()])))
        result = await repository.get_by_key(
            provider_id="ransomlook",
            provider_record_id="record-1",
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
        )
        assert result is not None
        assert result.normalized_locator == "https://example.com/a?b=c"

    @pytest.mark.asyncio
    async def test_list_validates_pagination(self) -> None:
        repository = PostgresDiscoveryRepository(_Scope(_Cursor()))
        with pytest.raises(ValueError):
            await repository.list_for_provider("ransomlook", limit=0)
        with pytest.raises(ValueError):
            await repository.list_for_provider("ransomlook", limit=201)
        with pytest.raises(ValueError):
            await repository.list_for_provider("ransomlook", offset=-1)

    @pytest.mark.asyncio
    async def test_list_maps_rows(self) -> None:
        repository = PostgresDiscoveryRepository(_Scope(_Cursor([_observation_row()])))
        rows = await repository.list_for_provider("ransomlook")
        assert len(rows) == 1
        assert rows[0].observation_id == _OBSERVATION_ID

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        class _CancellingRepository(PostgresDiscoveryRepository):
            async def _fetch_one(
                self, sql: str, params: tuple[Any, ...]
            ) -> tuple[Any, ...] | None:
                raise asyncio.CancelledError

        repository = _CancellingRepository(_Scope(_Cursor()))
        with pytest.raises(asyncio.CancelledError):
            await repository.get(_OBSERVATION_ID)


class TestAccessRepository:
    def _evidence(self, status: AccessStatus) -> AccessEvidence:
        return AccessEvidence(
            access_evidence_id=AccessEvidenceId.generate(),
            observation_id=_OBSERVATION_ID,
            candidate_id=_CANDIDATE_ID,
            source_endpoint_id=None,
            status=status,
            reason_code="PROVIDER_AUTH_CLAIM",
            recorded_at=_MOMENT,
            evidence_reference="registry://ransomlook/record-1",
        )

    @pytest.mark.asyncio
    async def test_append_rejects_non_pending_status(self) -> None:
        repository = PostgresAccessRepository(_Scope(_Cursor()))
        with pytest.raises(ValueError):
            await repository.append_evidence(self._evidence(AccessStatus.READY))

    @pytest.mark.asyncio
    async def test_append_rejects_endpoint_target(self) -> None:
        repository = PostgresAccessRepository(_Scope(_Cursor()))
        evidence = AccessEvidence(
            access_evidence_id=AccessEvidenceId.generate(),
            observation_id=_OBSERVATION_ID,
            candidate_id=None,
            source_endpoint_id=SourceEndpointId.generate(),
            status=AccessStatus.CREDENTIALS_PENDING,
            reason_code="PROVIDER_AUTH_CLAIM",
            recorded_at=_MOMENT,
            evidence_reference="registry://ransomlook/record-1",
        )
        with pytest.raises(ValueError):
            await repository.append_evidence(evidence)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("outcome", "error"),
        [
            ("unknown_observation", NotFoundError),
            ("unknown_candidate", NotFoundError),
            ("claim_absent", ConflictError),
            ("wrong_candidate", ConflictError),
            ("conflict", ConflictError),
        ],
    )
    async def test_append_outcomes(
        self, outcome: str, error: type[PersistenceError]
    ) -> None:
        repository = PostgresAccessRepository(_Scope(_Cursor([(outcome,)])))
        with pytest.raises(error):
            await repository.append_evidence(
                self._evidence(AccessStatus.CREDENTIALS_PENDING)
            )

    @pytest.mark.asyncio
    async def test_append_returns_persisted_evidence(self) -> None:
        evidence = self._evidence(AccessStatus.CREDENTIALS_PENDING)
        cursor = _Cursor([("added",), _access_row()])
        repository = PostgresAccessRepository(_Scope(cursor))
        result = await repository.append_evidence(evidence)
        assert result.status is AccessStatus.CREDENTIALS_PENDING
        assert cursor.executed_sql[0].startswith(
            "SELECT * FROM access_evidence_append_pending_v1("
        )

    @pytest.mark.asyncio
    async def test_latest_missing_returns_none(self) -> None:
        repository = PostgresAccessRepository(_Scope(_Cursor()))
        assert await repository.latest_for_candidate(_CANDIDATE_ID) is None

    @pytest.mark.asyncio
    async def test_list_and_latest_map_rows(self) -> None:
        cursor = _Cursor([_access_row(), _access_row()])
        repository = PostgresAccessRepository(_Scope(cursor))
        rows = await repository.list_for_candidate(_CANDIDATE_ID)
        assert len(rows) == 2
        latest_cursor = _Cursor([_access_row()])
        latest = await PostgresAccessRepository(
            _Scope(latest_cursor)
        ).latest_for_endpoint(SourceEndpointId.generate())
        assert latest is not None


class TestQualificationRepository:
    @pytest.mark.asyncio
    async def test_event_must_match_candidate(self) -> None:
        repository = PostgresQualificationRepository(_Scope(_Cursor()))
        decision = _decision()
        event = SourceCandidateEventHistory(
            event_id=CandidateEventId.generate(),
            candidate_id=SourceCandidateId.generate(),
            event_type=CandidateEventType.QUALIFIED,
            occurred_at=_MOMENT,
        )
        with pytest.raises(ValueError):
            await repository.decide_and_transition(decision, event)

    @pytest.mark.asyncio
    async def test_event_type_must_match_outcome(self) -> None:
        repository = PostgresQualificationRepository(_Scope(_Cursor()))
        decision = _decision()
        event = SourceCandidateEventHistory(
            event_id=CandidateEventId.generate(),
            candidate_id=decision.candidate_id,
            event_type=CandidateEventType.REJECTED,
            occurred_at=_MOMENT,
        )
        with pytest.raises(ValueError):
            await repository.decide_and_transition(decision, event)

    @pytest.mark.asyncio
    async def test_event_id_must_match_decision_event_id(self) -> None:
        cursor = _Cursor()
        repository = PostgresQualificationRepository(_Scope(cursor))
        decision = _decision()
        event = SourceCandidateEventHistory(
            event_id=CandidateEventId.generate(),
            candidate_id=decision.candidate_id,
            event_type=CandidateEventType.QUALIFIED,
            occurred_at=_MOMENT,
        )
        assert event.event_id != decision.event_id
        with pytest.raises(ValueError):
            await repository.decide_and_transition(decision, event)
        assert cursor.executed_sql == []

    def _event(self, decision: QualificationDecision) -> SourceCandidateEventHistory:
        return SourceCandidateEventHistory(
            event_id=decision.event_id,
            candidate_id=decision.candidate_id,
            event_type=CandidateEventType.QUALIFIED,
            occurred_at=_MOMENT,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("outcome", "error"),
        [
            ("conflict", ConflictError),
            ("state_conflict", ConflictError),
            ("unknown_candidate", NotFoundError),
            ("unknown_evidence", IntegrityError),
            ("invalid_provenance", IntegrityError),
        ],
    )
    async def test_decide_outcomes(
        self, outcome: str, error: type[PersistenceError]
    ) -> None:
        repository = PostgresQualificationRepository(_Scope(_Cursor([(outcome,)])))
        decision = _decision()
        with pytest.raises(error):
            await repository.decide_and_transition(decision, self._event(decision))

    @pytest.mark.asyncio
    async def test_decide_returns_persisted_decision(self) -> None:
        decision = _decision()
        cursor = _Cursor([("decided",), _decision_row(decision)])
        repository = PostgresQualificationRepository(_Scope(cursor))
        result = await repository.decide_and_transition(decision, self._event(decision))
        assert result.decision_key == decision.decision_key
        assert result.evidence_observation_ids == (_OBSERVATION_ID,)
        assert cursor.executed_sql[0].startswith(
            "SELECT * FROM qualification_decide_and_transition_v1("
        )

    @pytest.mark.asyncio
    async def test_get_by_key_missing_returns_none(self) -> None:
        repository = PostgresQualificationRepository(_Scope(_Cursor()))
        assert await repository.get_by_key("f" * 64) is None

    @pytest.mark.asyncio
    async def test_list_maps_decisions(self) -> None:
        decision = _decision()
        repository = PostgresQualificationRepository(
            _Scope(_Cursor([_decision_row(decision)]))
        )
        rows = await repository.list_for_candidate(_CANDIDATE_ID)
        assert len(rows) == 1
        assert rows[0].outcome is QualificationOutcome.QUALIFIED

    @pytest.mark.asyncio
    async def test_mapping_failure_is_bounded(self) -> None:
        row = list(_decision_row(_decision()))
        row[2] = "UNKNOWN_METHOD"
        repository = PostgresQualificationRepository(_Scope(_Cursor([tuple(row)])))
        with pytest.raises(MappingError):
            await repository.get_by_key("f" * 64)
