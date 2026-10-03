# SPDX-License-Identifier: AGPL-3.0-only
"""Transaction semantics against real PostgreSQL (T1-T6)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from darkula.app.persistence import ConflictError
from darkula.config.settings import DatabaseSettings
from darkula.domain.identifiers import (
    CandidateEventId,
    ReconAssessmentId,
    SourceAssessmentId,
    SourceCandidateId,
    SourceEndpointId,
    SourceId,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    Confidence,
    EndpointStatus,
    EndpointType,
    ReconAssessment,
    ReconDisposition,
    Source,
    SourceAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
    SourceEndpoint,
    SourceStatus,
)
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from tests.integration.conftest import _raw_connect, row_values

pytestmark = pytest.mark.integration


_MOMENT = datetime(2025, 2, 3, 4, 5, 6, tzinfo=UTC)


def _candidate() -> SourceCandidate:
    return SourceCandidate(
        candidate_id=SourceCandidateId.generate(),
        discovered_at=_MOMENT,
        discovery_method="seed",
        entrypoint="http://example.invalid/0",
        status=CandidateStatus.DISCOVERED,
    )


def _event(candidate_id: SourceCandidateId) -> SourceCandidateEventHistory:
    return SourceCandidateEventHistory(
        event_id=CandidateEventId.generate(),
        candidate_id=candidate_id,
        event_type=CandidateEventType.DISCOVERED,
        occurred_at=_MOMENT,
    )


def _source() -> Source:
    return Source(
        source_id=SourceId.generate(),
        status=SourceStatus.ACTIVE,
        created_at=_MOMENT,
        updated_at=_MOMENT,
    )


class TestTransactions:
    """T1-T4: one UoW commit/rollback spans every repository."""

    @pytest.mark.asyncio
    async def test_t1_candidate_history_recon_commit_all_persist(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = _candidate()
        event = _event(candidate.candidate_id)
        recon = ReconAssessment(
            assessment_id=ReconAssessmentId.generate(),
            candidate_id=candidate.candidate_id,
            assessed_at=_MOMENT,
            disposition=ReconDisposition.QUALIFY,
            confidence=Confidence(0.8),
        )
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            history_event = SourceCandidateEventHistory(
                event_id=CandidateEventId.generate(),
                candidate_id=candidate.candidate_id,
                event_type=CandidateEventType.RECONNAISSANCE_STARTED,
                occurred_at=_MOMENT + timedelta(seconds=5),
            )
            await uow.source_candidates.append_history(history_event)
            await uow.source_candidates.append_recon_assessment(recon)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            read = await uow.source_candidates.get(candidate.candidate_id)
            history = await uow.source_candidates.list_history(candidate.candidate_id)
            recon_list = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
        assert read is not None
        assert len(history) == 2
        assert len(recon_list) == 1

    @pytest.mark.asyncio
    async def test_t2_exception_commits_nothing(self, spi: PostgresDarkulaSpi) -> None:
        candidate = _candidate()
        event = _event(candidate.candidate_id)
        with pytest.raises(RuntimeError, match="boom"):
            async with spi.unit_of_work() as uow:
                await uow.source_candidates.create(candidate, event=event)
                raise RuntimeError("boom")
        async with spi.unit_of_work() as uow:
            assert await uow.source_candidates.get(candidate.candidate_id) is None

    @pytest.mark.asyncio
    async def test_t3_source_endpoint_assessment_commit_all(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source = _source()
        endpoint = SourceEndpoint(
            endpoint_id=SourceEndpointId.generate(),
            source_id=source.source_id,
            uri="http://m.example.invalid/",
            endpoint_type=EndpointType.MIRROR,
            status=EndpointStatus.ACTIVE,
            first_observed_at=_MOMENT,
            last_observed_at=_MOMENT,
        )
        assessment = SourceAssessment(
            assessment_id=SourceAssessmentId.generate(),
            source_id=source.source_id,
            assessed_at=_MOMENT,
            window_start=_MOMENT - timedelta(days=1),
            window_end=_MOMENT,
            confidence=Confidence(0.7),
        )
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.sources.add_endpoint(endpoint)
            await uow.sources.append_source_assessment(assessment)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            read = await uow.sources.get(source.source_id)
            endpoints = await uow.sources.list_endpoints(source.source_id)
            assessments = await uow.sources.list_source_assessments(source.source_id)
        assert read is not None
        assert len(endpoints) == 1
        assert len(assessments) == 1

    @pytest.mark.asyncio
    async def test_t4_rollback_commits_nothing(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        source = _source()
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.rollback()
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM source WHERE id = %s",
                    (str(source.source_id),),
                )
                assert row_values(cur, cur.fetchone())[0] == 0
        finally:
            conn.close()


class TestCancellation:
    """T5: cancellation propagates; nothing commits; connection released."""

    @pytest.mark.asyncio
    async def test_t5_cancellation_no_commit_connection_released(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = _candidate()
        event = _event(candidate.candidate_id)

        task = asyncio.create_task(
            self._worker_with_inner_cancel(spi, candidate, event)
        )
        with pytest.raises(asyncio.CancelledError):
            await task

        # Fall through: the candidate must not be committed, and a fresh
        # unit of work must succeed (the pool released the connection).
        async with spi.unit_of_work() as uow:
            assert await uow.source_candidates.get(candidate.candidate_id) is None

    @staticmethod
    async def _worker_with_inner_cancel(
        spi: PostgresDarkulaSpi,
        candidate: SourceCandidate,
        event: SourceCandidateEventHistory,
    ) -> None:
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            # simulate an operation that gets cancelled before commit
            await asyncio.sleep(0)
            raise asyncio.CancelledError()


class TestRecovery:
    """T6: a fresh unit of work succeeds after a failed one."""

    @pytest.mark.asyncio
    async def test_t6_new_uow_after_failure_succeeds(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = _candidate()
        event = _event(candidate.candidate_id)
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(candidate, event=event)
            await uow.commit()

        duplicate = _candidate()
        event2 = _event(duplicate.candidate_id)
        # Force a conflict inside one UoW, resolve it by rollback, then open
        # a completely new UoW that must work.
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.source_candidates.create(
                    candidate, event=_event(candidate.candidate_id)
                )
            await uow.rollback()

        async with spi.unit_of_work() as uow:
            await uow.source_candidates.create(duplicate, event=event2)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            assert (await uow.source_candidates.get(duplicate.candidate_id)) is not None
