# SPDX-License-Identifier: AGPL-3.0-only
"""PR 10 real-PostgreSQL recon lifecycle matrix (RP1-RP12).

Runs the real ReconCoordinator + ReconAgent over the real
``PostgresDarkulaSpi`` with a deterministic FakeLlmClient and a scripted
crawler stub (the lifecycle/atomicity under test is persistence; the real
crawler vertical slice lives in test_recon_vertical_slice.py). Proves:

- the start transition (DISCOVERED/PENDING -> UNDER_RECONNAISSANCE +
  RECONNAISSANCE_STARTED) is atomic and committed before external work;
- a concurrent start has exactly one winner;
- the final ReconAssessment append and lifecycle transition commit
  atomically (and roll back together on conflict);
- multiple executions append immutable assessments without rewriting
  history;
- migrations 0001-0004 stay byte-identical and production Python adds no
  SQL.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.integration.conftest import _raw_connect, row_values
from tests.support.collection_fakes import FakeCrawler

from darkula.app.recon import (
    ReconAction,
    ReconAgentDecision,
    ReconCandidateNotEligibleError,
    ReconCompletion,
    ReconCoordinator,
    ReconExecutionError,
    ReconInspection,
)
from darkula.app.recon_agent import ReconAgent
from darkula.config.settings import DatabaseSettings, ReconSettings
from darkula.crawler.contracts import CrawlRequest, CrawlResult, CrawlStatus
from darkula.domain.identifiers import (
    CandidateEventId,
    SourceCandidateId,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    ReconDisposition,
    SourceCandidate,
    SourceCandidateEventHistory,
)
from darkula.infrastructure.observability import NoOpAgentObservability
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_llm import FakeLlmClient

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
_EXECUTION_ID = "recon-intg-exec"


class _AdvancingClock:
    """Monotonic UTC test clock: each call returns the next second.

    PostgreSQL history/assessment ordering is by ``occurred_at``/
    ``assessed_at``, so deterministic tests must never share equal
    timestamps across events of one candidate.
    """

    def __init__(self, start: datetime) -> None:
        self._next = start

    def __call__(self) -> datetime:
        value = self._next
        self._next = value + timedelta(seconds=1)
        return value


def _candidate(
    *, status: CandidateStatus = CandidateStatus.DISCOVERED
) -> SourceCandidate:
    return SourceCandidate(
        candidate_id=SourceCandidateId.generate(),
        discovered_at=_T0,
        discovery_method="integration-seed",
        entrypoint="http://blackgate.example.test/",
        status=status,
    )


def _event(candidate: SourceCandidate) -> SourceCandidateEventHistory:
    return SourceCandidateEventHistory(
        event_id=CandidateEventId.generate(),
        candidate_id=candidate.candidate_id,
        event_type=CandidateEventType.DISCOVERED,
        occurred_at=_T0,
        reason="integration-seed",
    )


async def _seed(spi: PostgresDarkulaSpi) -> SourceCandidate:
    candidate = _candidate()
    async with spi.unit_of_work() as uow:
        await uow.source_candidates.create(candidate, event=_event(candidate))
        await uow.commit()
    return candidate


def _inspect(target: str) -> ReconAgentDecision:
    return ReconAgentDecision(
        action=ReconAction.INSPECT,
        inspection=ReconInspection(target=target, purpose="observe"),
    )


def _complete(
    disposition: ReconDisposition, confidence: float = 0.8, refs: tuple[str, ...] = ()
) -> ReconAgentDecision:
    return ReconAgentDecision(
        action=ReconAction.COMPLETE,
        completion=ReconCompletion(
            disposition=disposition,
            confidence=confidence,
            evidence_references=list(refs),
            characteristics={"accessibility": "open"},
        ),
    )


def _agent(llm: FakeLlmClient) -> ReconAgent:
    return ReconAgent(
        llm=llm,
        observability=NoOpAgentObservability(),
        settings=ReconSettings(),
    )


def _coordinator(
    spi: PostgresDarkulaSpi,
    *,
    llm: FakeLlmClient,
    crawler: Any = None,
    clock: Any = None,
) -> ReconCoordinator:
    return ReconCoordinator(
        spi=spi,
        agent=_agent(llm),
        crawler=crawler or FakeCrawler(),
        settings=ReconSettings(),
        clock=clock or _AdvancingClock(_T0 + timedelta(seconds=1)),
        execution_id_factory=lambda: _EXECUTION_ID,
    )


class TestStartTransition:
    """RP1/RP2: the start transition is atomic and committed before any
    external work."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [CandidateStatus.DISCOVERED, CandidateStatus.RECONNAISSANCE_PENDING],
    )
    async def test_rp1_2_start_is_atomic_before_external_work(
        self, spi: PostgresDarkulaSpi, status: CandidateStatus
    ) -> None:
        candidate = await _seed(spi)
        # Put the candidate into the desired start status explicitly, with a
        # strictly increasing event timestamp (PostgreSQL history orders by
        # ``occurred_at``).
        expected_before_start = [CandidateEventType.DISCOVERED]
        if status is CandidateStatus.RECONNAISSANCE_PENDING:
            expected_before_start.append(CandidateEventType.NEEDS_MORE_RECON)
            async with spi.unit_of_work() as uow:
                await uow.source_candidates.transition(
                    candidate.candidate_id,
                    expected=CandidateStatus.DISCOVERED,
                    new_status=CandidateStatus.RECONNAISSANCE_PENDING,
                    event=SourceCandidateEventHistory(
                        event_id=CandidateEventId.generate(),
                        candidate_id=candidate.candidate_id,
                        event_type=CandidateEventType.NEEDS_MORE_RECON,
                        occurred_at=_T0 + timedelta(seconds=1),
                    ),
                )
                await uow.commit()
        clock = _AdvancingClock(_T0 + timedelta(seconds=2))

        observed: dict[str, Any] = {}

        class _ProbeCrawler(FakeCrawler):
            """Reads the candidate through a second real UoW inside the
            crawl: the start transaction must already be committed."""

            async def crawl(self, request: CrawlRequest) -> CrawlResult:
                async with spi.unit_of_work() as uow:
                    fresh = await uow.source_candidates.get(candidate.candidate_id)
                    history = await uow.source_candidates.list_history(
                        candidate.candidate_id
                    )
                assert fresh is not None
                observed["status"] = fresh.status
                observed["events"] = [h.event_type for h in history]
                return CrawlResult(
                    request_id=request.request_id,
                    status=CrawlStatus.RESOURCE_LIMITED,
                    requests=0,
                )

        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_inspect("/"))
        crawler = _ProbeCrawler()
        coordinator = _coordinator(spi, llm=llm, crawler=crawler, clock=clock)
        with pytest.raises(ReconExecutionError, match="inspection crawl"):
            await coordinator.reconnoiter(candidate.candidate_id)

        assert observed["status"] is CandidateStatus.UNDER_RECONNAISSANCE
        assert observed["events"] == [
            *expected_before_start,
            CandidateEventType.RECONNAISSANCE_STARTED,
        ]

        # The recoverable failure moved the candidate back to pending with
        # exactly one retry event; nothing is lost or duplicated.
        async with spi.unit_of_work() as uow:
            fresh = await uow.source_candidates.get(candidate.candidate_id)
            history = await uow.source_candidates.list_history(candidate.candidate_id)
        assert (
            fresh is not None and fresh.status is CandidateStatus.RECONNAISSANCE_PENDING
        )
        assert [h.event_type for h in history] == [
            *expected_before_start,
            CandidateEventType.RECONNAISSANCE_STARTED,
            CandidateEventType.NEEDS_MORE_RECON,
        ]


class TestFinalAtomicity:
    """RP4-RP7/RP10: assessment append + final transition are one
    transaction; conflicts roll back everything."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("disposition", "status", "event"),
        [
            (
                ReconDisposition.QUALIFY,
                CandidateStatus.QUALIFIED,
                CandidateEventType.QUALIFIED,
            ),
            (
                ReconDisposition.NEEDS_MORE_RECON,
                CandidateStatus.RECONNAISSANCE_PENDING,
                CandidateEventType.NEEDS_MORE_RECON,
            ),
            (
                ReconDisposition.REJECT,
                CandidateStatus.REJECTED,
                CandidateEventType.REJECTED,
            ),
        ],
    )
    async def test_rp4_6_assessment_and_transition_commit_atomically(
        self,
        spi: PostgresDarkulaSpi,
        disposition: ReconDisposition,
        status: CandidateStatus,
        event: CandidateEventType,
    ) -> None:
        candidate = await _seed(spi)
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_complete(disposition))
        coordinator = _coordinator(spi, llm=llm)
        result = await coordinator.reconnoiter(candidate.candidate_id)
        assert result.final_status is status

        async with spi.unit_of_work() as uow:
            assessments = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
            history = await uow.source_candidates.list_history(candidate.candidate_id)
            fresh = await uow.source_candidates.get(candidate.candidate_id)
        assert len(assessments) == 1
        assert assessments[0].disposition is disposition
        assert fresh is not None and fresh.status is status
        assert [h.event_type for h in history] == [
            CandidateEventType.DISCOVERED,
            CandidateEventType.RECONNAISSANCE_STARTED,
            event,
        ]

    @pytest.mark.asyncio
    async def test_rp7_final_conflict_rolls_back_assessment(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        candidate = await _seed(spi)

        async def _flip() -> None:
            async with spi.unit_of_work() as uow:
                await uow.source_candidates.transition(
                    candidate.candidate_id,
                    expected=CandidateStatus.UNDER_RECONNAISSANCE,
                    new_status=CandidateStatus.QUALIFIED,
                    event=SourceCandidateEventHistory(
                        event_id=CandidateEventId.generate(),
                        candidate_id=candidate.candidate_id,
                        event_type=CandidateEventType.QUALIFIED,
                        occurred_at=_T0,
                    ),
                )
                await uow.commit()

        class _FlipCrawler(FakeCrawler):
            async def crawl(self, request: CrawlRequest) -> CrawlResult:
                await _flip()
                return CrawlResult(
                    request_id=request.request_id,
                    status=CrawlStatus.COMPLETED,
                    requests=1,
                )

        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_inspect("/"))
        llm.enqueue(_complete(ReconDisposition.QUALIFY))
        coordinator = _coordinator(spi, llm=llm, crawler=_FlipCrawler())
        with pytest.raises(ReconExecutionError, match="rolled back"):
            await coordinator.reconnoiter(candidate.candidate_id)

        # The loser committed neither the assessment nor its transition.
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM recon_assessment WHERE candidate_id = %s",
                    (str(candidate.candidate_id),),
                )
                assert row_values(cur, cur.fetchone())[0] == 0
                cur.execute(
                    "SELECT count(*) FROM source_candidate_event_history "
                    "WHERE candidate_id = %s",
                    (str(candidate.candidate_id),),
                )
                # DISCOVERED + the coordinator's STARTED + the concurrent
                # actor's QUALIFIED; the loser's final event is not present.
                assert row_values(cur, cur.fetchone())[0] == 3
        finally:
            conn.close()


class TestAppendOnlyHistory:
    """RP8/RP9: pending retries append new immutable assessments."""

    @pytest.mark.asyncio
    async def test_rp8_9_multiple_attempts_append_only_deterministic_order(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = await _seed(spi)

        # One shared monotonic clock keeps both executions' assessments and
        # events strictly ordered (the stored functions order by time, then
        # random id).
        clock = _AdvancingClock(_T0 + timedelta(seconds=1))

        # First execution: NEEDS_MORE_RECON -> PENDING + assessment #1.
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_complete(ReconDisposition.NEEDS_MORE_RECON))
        coordinator = _coordinator(spi, llm=llm, clock=clock)
        first = await coordinator.reconnoiter(candidate.candidate_id)
        assert first.final_status is CandidateStatus.RECONNAISSANCE_PENDING

        # Second execution: QUALIFY -> QUALIFIED + assessment #2.
        llm2 = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm2.enqueue(_complete(ReconDisposition.QUALIFY))
        coordinator2 = _coordinator(spi, llm=llm2, clock=clock)
        second = await coordinator2.reconnoiter(candidate.candidate_id)
        assert second.final_status is CandidateStatus.QUALIFIED

        async with spi.unit_of_work() as uow:
            assessments = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
        assert len(assessments) == 2
        assert [a.disposition for a in assessments] == [
            ReconDisposition.NEEDS_MORE_RECON,
            ReconDisposition.QUALIFY,
        ]
        # Old history is untouched: exactly one STARTED (first execution's
        # pending transition is the NEEDS_MORE_RECON event).
        async with spi.unit_of_work() as uow:
            history = await uow.source_candidates.list_history(candidate.candidate_id)
        assert [h.event_type for h in history] == [
            CandidateEventType.DISCOVERED,
            CandidateEventType.RECONNAISSANCE_STARTED,
            CandidateEventType.NEEDS_MORE_RECON,
            CandidateEventType.RECONNAISSANCE_STARTED,
            CandidateEventType.QUALIFIED,
        ]


class TestConcurrencyAndGuards:
    """RP3/RP11/RP12: one winner, no production SQL, migrations unchanged."""

    @pytest.mark.asyncio
    async def test_rp3_concurrent_start_one_winner(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate = await _seed(spi)
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_inspect("/"))
        llm.enqueue(_complete(ReconDisposition.QUALIFY))
        crawler = FakeCrawler()
        crawler.results.append(
            CrawlResult(request_id="req", status=CrawlStatus.COMPLETED, requests=1)
        )
        coordinator = _coordinator(spi, llm=llm, crawler=crawler)

        async def run() -> Any:
            return await coordinator.reconnoiter(candidate.candidate_id)

        outcomes = await asyncio.gather(run(), run(), return_exceptions=True)
        results = [item for item in outcomes if not isinstance(item, BaseException)]
        errors = [item for item in outcomes if isinstance(item, BaseException)]
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ReconCandidateNotEligibleError)
        assert llm.call_count == 2  # exactly one execution invoked the model
        assert len(crawler.requests) == 1

        async with spi.unit_of_work() as uow:
            history = await uow.source_candidates.list_history(candidate.candidate_id)
            assessments = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
        started = [
            h
            for h in history
            if h.event_type is CandidateEventType.RECONNAISSANCE_STARTED
        ]
        assert len(started) == 1  # no duplicate STARTED event
        assert len(assessments) == 1

    def test_rp11_no_production_python_sql_in_recon_modules(self) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[4]
        scanned = [
            repo / "src" / "darkula" / "app" / "recon.py",
            repo / "src" / "darkula" / "app" / "recon_agent.py",
            repo / "src" / "darkula" / "infrastructure" / "llm" / "openai.py",
        ]
        for path in scanned:
            text = path.read_text(encoding="utf-8")
            assert "SELECT" not in text, f"SQL leak in {path.name}"
            assert "INSERT" not in text and "UPDATE" not in text

    def test_rp12_migrations_0001_0004_unchanged(self) -> None:
        migrations = [
            "migrations/0001_initial.sql",
            "migrations/0002_message_outbox.sql",
            "migrations/0003_content.sql",
            "migrations/0004_collection.sql",
        ]
        result = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", *migrations],
            capture_output=True,
            text=True,
            cwd=__import__("pathlib").Path(__file__).resolve().parents[3],
        )
        assert result.returncode == 0, (
            "historical migrations 0001-0004 must remain byte-identical"
        )
