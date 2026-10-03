# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate repository integration matrix (C1-C10) over real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from darkula.app.persistence import ConflictError, IntegrityError
from darkula.config.settings import DatabaseSettings
from darkula.domain.identifiers import (
    CandidateEventId,
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
from tests.integration.conftest import _raw_connect, row_values

pytestmark = pytest.mark.integration


_MOMENT = datetime(2025, 2, 3, 4, 5, 6, tzinfo=UTC)


def _candidate(*, n: int = 0) -> SourceCandidate:
    return SourceCandidate(
        candidate_id=SourceCandidateId.generate(),
        discovered_at=_MOMENT + timedelta(seconds=n),
        discovery_method="seed-index",
        entrypoint=f"http://example.invalid/{n}",
        status=CandidateStatus.DISCOVERED,
        discovery_context={"seed": n},
    )


def _event(
    *,
    n: int = 0,
    candidate_id: SourceCandidateId | None = None,
    kind: str = "DISCOVERED",
) -> SourceCandidateEventHistory:
    return SourceCandidateEventHistory(
        event_id=CandidateEventId.generate(),
        candidate_id=candidate_id or SourceCandidateId.generate(),
        event_type=CandidateEventType(kind),
        occurred_at=_MOMENT + timedelta(seconds=n),
        context={"n": n},
        reason="seed",
        provenance="integration",
    )


class TestCreateAndRead:
    """C1/C2/C3/C4: create+commit, uncommitted absence, duplicate, missing."""

    @pytest.mark.asyncio
    async def test_c1_create_commit_then_retrieve_in_new_uow(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = _candidate()
        event = _event(candidate_id=candidate.candidate_id)
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            read = await uow.source_candidates.get(candidate.candidate_id)
            assert read is not None
            assert read.candidate_id == candidate.candidate_id
            assert read.status is CandidateStatus.DISCOVERED

    @pytest.mark.asyncio
    async def test_c2_create_without_commit_is_absent(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = _candidate()
        event = _event(candidate_id=candidate.candidate_id)
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            # no commit: context exit rolls back

        async with spi.unit_of_work() as uow:
            assert await uow.source_candidates.get(candidate.candidate_id) is None

    @pytest.mark.asyncio
    async def test_c3_duplicate_id_is_typed_conflict(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = _candidate()
        event = _event(candidate_id=candidate.candidate_id)
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.source_candidates.create(
                    candidate, event=_event(candidate_id=candidate.candidate_id)
                )
            await uow.commit()

    @pytest.mark.asyncio
    async def test_c4_missing_id_returns_none(self, spi: PostgresDarkulaSpi) -> None:
        async with spi.unit_of_work() as uow:
            assert await uow.source_candidates.get(SourceCandidateId.generate()) is None


class TestTransitions:
    """C5/C6/C7/C8: atomic expected-state transitions."""

    @pytest.mark.asyncio
    async def test_c5_valid_transition_status_plus_exactly_one_event(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        candidate = _candidate()
        event = _event(candidate_id=candidate.candidate_id)
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            await uow.commit()

        transition_event = SourceCandidateEventHistory(
            event_id=CandidateEventId.generate(),
            candidate_id=candidate.candidate_id,
            event_type=CandidateEventType.QUALIFIED,
            occurred_at=_MOMENT + timedelta(seconds=10),
        )
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.transition(
                candidate.candidate_id,
                expected=CandidateStatus.DISCOVERED,
                new_status=CandidateStatus.QUALIFIED,
                event=transition_event,
            )
            await uow.commit()

        async with spi.unit_of_work() as uow:
            read = await uow.source_candidates.get(candidate.candidate_id)
            assert read is not None
            assert read.status is CandidateStatus.QUALIFIED
            history = await uow.source_candidates.list_history(candidate.candidate_id)
            assert len(history) == 2  # DISCOVERED + QUALIFIED

    @pytest.mark.asyncio
    async def test_c6_wrong_expected_status_no_update_no_event(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        candidate = _candidate()
        event = _event(candidate_id=candidate.candidate_id)
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.source_candidates.transition(
                    candidate.candidate_id,
                    expected=CandidateStatus.UNDER_RECONNAISSANCE,
                    new_status=CandidateStatus.QUALIFIED,
                    event=_event(candidate_id=candidate.candidate_id, kind="QUALIFIED"),
                )
            await uow.commit()

        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM source_candidate WHERE id = %s",
                    (str(candidate.candidate_id),),
                )
                assert row_values(cur, cur.fetchone())[0] == "DISCOVERED"
                cur.execute(
                    "SELECT count(*) FROM source_candidate_event_history "
                    "WHERE candidate_id = %s",
                    (str(candidate.candidate_id),),
                )
                # only the creation event
                assert row_values(cur, cur.fetchone())[0] == 1
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_c7_multiple_events_deterministic_order(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = _candidate()
        event = _event(candidate_id=candidate.candidate_id)
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            await uow.commit()

        kinds = [
            (CandidateEventType.RECONNAISSANCE_STARTED, 1),
            (CandidateEventType.RECONNAISSANCE_COMPLETED, 2),
            (CandidateEventType.NEEDS_MORE_RECON, 3),
        ]
        for kind, n in kinds:
            new_event = _event(
                candidate_id=candidate.candidate_id, n=n, kind=kind.value
            )
            async with spi.unit_of_work() as uow:
                await uow.source_candidates.append_history(new_event)
                await uow.commit()

        async with spi.unit_of_work() as uow:
            history = await uow.source_candidates.list_history(candidate.candidate_id)
        assert [h.event_type for h in history] == [
            CandidateEventType.DISCOVERED,
            CandidateEventType.RECONNAISSANCE_STARTED,
            CandidateEventType.RECONNAISSANCE_COMPLETED,
            CandidateEventType.NEEDS_MORE_RECON,
        ]

    @pytest.mark.asyncio
    async def test_c8_rollback_transition_persists_neither(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        candidate = _candidate()
        event = _event(candidate_id=candidate.candidate_id)
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            await uow.commit()

        # Simulate a failure after a successful transition, then roll back.
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.transition(
                candidate.candidate_id,
                expected=CandidateStatus.DISCOVERED,
                new_status=CandidateStatus.UNDER_RECONNAISSANCE,
                event=_event(
                    candidate_id=candidate.candidate_id, kind="RECONNAISSANCE_STARTED"
                ),
            )
            await uow.rollback()

        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM source_candidate WHERE id = %s",
                    (str(candidate.candidate_id),),
                )
                assert row_values(cur, cur.fetchone())[0] == "DISCOVERED"
                cur.execute(
                    "SELECT count(*) FROM source_candidate_event_history "
                    "WHERE candidate_id = %s",
                    (str(candidate.candidate_id),),
                )
                assert row_values(cur, cur.fetchone())[0] == 1
        finally:
            conn.close()


class TestReconAssessments:
    """C9/C10: recon assessments are append-only and deterministic."""

    @pytest.mark.asyncio
    async def test_c9_append_and_retrieve_recon(self, spi: PostgresDarkulaSpi) -> None:
        candidate = _candidate()
        event = _event(candidate_id=candidate.candidate_id)
        assessment = ReconAssessment(
            assessment_id=ReconAssessmentId.generate(),
            candidate_id=candidate.candidate_id,
            assessed_at=_MOMENT,
            disposition=ReconDisposition.QUALIFY,
            confidence=Confidence(0.8),
            evidence_references=("session-a",),
            characteristics={"accessibility": "open"},
        )
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            await uow.source_candidates.append_recon_assessment(assessment)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            recon = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
        assert len(recon) == 1
        assert recon[0].assessment_id == assessment.assessment_id
        assert recon[0].disposition is ReconDisposition.QUALIFY

    @pytest.mark.asyncio
    async def test_c10_multiple_recon_deterministic_order(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = _candidate()
        event = _event(candidate_id=candidate.candidate_id)
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            await uow.commit()

        assessments = [
            ReconAssessment(
                assessment_id=ReconAssessmentId.generate(),
                candidate_id=candidate.candidate_id,
                assessed_at=_MOMENT + timedelta(minutes=n),
                disposition=ReconDisposition.NEEDS_MORE_RECON,
                confidence=Confidence(0.5),
            )
            for n in (2, 0, 1)
        ]
        for assessment in assessments:
            async with spi.unit_of_work() as uow:
                await uow.source_candidates.append_recon_assessment(assessment)
                await uow.commit()

        async with spi.unit_of_work() as uow:
            recon = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
        assert [a.assessment_id for a in recon] == [
            assessments[1].assessment_id,
            assessments[2].assessment_id,
            assessments[0].assessment_id,
        ]

    @pytest.mark.asyncio
    async def test_append_recon_to_missing_candidate_is_integrity(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        assessment = ReconAssessment(
            assessment_id=ReconAssessmentId.generate(),
            candidate_id=SourceCandidateId.generate(),
            assessed_at=_MOMENT,
            disposition=ReconDisposition.REJECT,
            confidence=Confidence(0.2),
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.source_candidates.append_recon_assessment(assessment)
            await uow.rollback()
