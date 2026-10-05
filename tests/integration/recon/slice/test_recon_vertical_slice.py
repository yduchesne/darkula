# SPDX-License-Identifier: AGPL-3.0-only
"""PR 10 canonical reconnaissance vertical slices (sections 15.1-15.5).

Full path: real PostgreSQL candidate -> real ReconCoordinator -> real
ReconAgent -> FakeLlmClient -> real CrawlerController -> real PodmanSandbox
-> disposable CrawlerRuntime -> real Playwright/Chromium -> real HTTP ->
BlackGate Fake World -> bounded observations -> ReconAgent ->
ReconAssessment -> real PostgreSQL final transition.

Also: cross-origin rejection (no crawl), needs-more retry with an appended
second assessment, reject with no Source creation, and concurrent start with
exactly one winner. FakeLlmClient remains the deterministic automated-test
boundary; the Coordinator, ReconAgent, crawler, sandbox, runtime, browser,
HTTP, Fake World, and persistence are all real.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.integration.conftest import _raw_connect
from tests.integration.crawler.helpers import AUTH_PASSWORD, TRUTH_ONLY_TOKENS

from darkula.app.recon import (
    ReconAction,
    ReconAgentDecision,
    ReconCandidateNotEligibleError,
    ReconCompletion,
    ReconCoordinator,
    ReconInspection,
    ReconInspectionRejectedError,
)
from darkula.app.recon_agent import ReconAgent
from darkula.config.settings import DatabaseSettings, ReconSettings
from darkula.crawler import Crawler, CrawlerController
from darkula.crawler.contracts import CrawlRequest, CrawlResult
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
_EXECUTION_ID = "recon-slice-exec"
_FAKE_WORLD_URL = "http://darkula-fake-world-intg:8080"


class _AdvancingClock:
    """Monotonic UTC test clock (events/assessments order by occurred_at)."""

    def __init__(self, start: datetime) -> None:
        self._next = start

    def __call__(self) -> datetime:
        value = self._next
        self._next = value + timedelta(seconds=1)
        return value


class _CountingCrawler(Crawler):
    """Transparent wrapper counting real crawler invocations.

    Every delegated call reaches the real CrawlerController — nothing is
    faked — the wrapper only proves whether a crawl was attempted.
    """

    def __init__(self, inner: CrawlerController) -> None:
        self._inner = inner
        self.count = 0

    async def crawl(self, request: CrawlRequest) -> CrawlResult:
        self.count += 1
        return await self._inner.crawl(request)


def _inspect(target: str) -> ReconAgentDecision:
    return ReconAgentDecision(
        action=ReconAction.INSPECT,
        inspection=ReconInspection(target=target, purpose="observe surface"),
    )


def _complete(
    disposition: ReconDisposition, refs: tuple[str, ...] = ()
) -> ReconAgentDecision:
    return ReconAgentDecision(
        action=ReconAction.COMPLETE,
        completion=ReconCompletion(
            disposition=disposition,
            confidence=0.85,
            evidence_references=list(refs),
            characteristics={
                "source_type": "forum",
                "accessibility": "open",
                "content": "commerce listings",
            },
        ),
    )


async def _seed_candidate(spi: PostgresDarkulaSpi) -> SourceCandidate:
    candidate = SourceCandidate(
        candidate_id=SourceCandidateId.generate(),
        discovered_at=_T0,
        discovery_method="blackgate-seed",
        entrypoint=f"{_FAKE_WORLD_URL}/",
        status=CandidateStatus.DISCOVERED,
    )
    async with spi.unit_of_work() as uow:
        await uow.source_candidates.create(
            candidate,
            event=SourceCandidateEventHistory(
                event_id=CandidateEventId.generate(),
                candidate_id=candidate.candidate_id,
                event_type=CandidateEventType.DISCOVERED,
                occurred_at=_T0,
                reason="blackgate-seed",
            ),
        )
        await uow.commit()
    return candidate


def _coordinator(
    spi: PostgresDarkulaSpi,
    *,
    llm: FakeLlmClient,
    crawler: _CountingCrawler,
    clock: Any = None,
) -> ReconCoordinator:
    return ReconCoordinator(
        spi=spi,
        agent=ReconAgent(
            llm=llm,
            observability=NoOpAgentObservability(),
            settings=ReconSettings(),
        ),
        crawler=crawler,
        settings=ReconSettings(),
        clock=clock or _AdvancingClock(_T0 + timedelta(seconds=1)),
        execution_id_factory=lambda: _EXECUTION_ID,
    )


def _leak_tokens_in_text(text: str) -> list[str]:
    haystack = text.lower()
    return [
        token
        for token in (*TRUTH_ONLY_TOKENS, AUTH_PASSWORD)
        if token.lower() in haystack
    ]


class TestCanonicalReconVerticalSlice:
    """Real everything except the model boundary."""

    @pytest.mark.asyncio
    async def test_151_blackgate_qualify_with_inspections(
        self,
        controller: CrawlerController,
        fake_world_url: str,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
    ) -> None:
        assert fake_world_url == _FAKE_WORLD_URL
        candidate = await _seed_candidate(spi)
        crawler = _CountingCrawler(controller)

        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_inspect("/index"))
        llm.enqueue(_inspect("/thread/thr-intros"))
        # The completion cites the first observed page of each inspection:
        # `<execution>:<turn>:1` are exactly the trusted refs of pages seen.
        llm.enqueue(
            _complete(
                ReconDisposition.QUALIFY,
                refs=(f"{_EXECUTION_ID}:1:1", f"{_EXECUTION_ID}:2:1"),
            )
        )
        coordinator = _coordinator(spi, llm=llm, crawler=crawler)

        result = await coordinator.reconnoiter(candidate.candidate_id)
        assert result.final_status is CandidateStatus.QUALIFIED
        assert result.disposition is ReconDisposition.QUALIFY
        assert result.inspections == 2
        assert result.evidence_items >= 2
        assert result.assessment_id is not None
        assert crawler.count == 2  # both inspections used the real crawler

        # Real PR 7 crawler path was used: bounded observations carried URLs
        # only from the Fake World origin.
        # (The coordinator consumed crawls internally; we assert the durable
        # assessment references trusted in-execution pages.)
        async with spi.unit_of_work() as uow:
            assessments = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
            history = await uow.source_candidates.list_history(candidate.candidate_id)
        assert len(assessments) == 1
        assert assessments[0].disposition is ReconDisposition.QUALIFY
        assert set(assessments[0].evidence_references) <= {
            f"{_EXECUTION_ID}:1:1",
            f"{_EXECUTION_ID}:2:1",
        }
        assert [h.event_type for h in history] == [
            CandidateEventType.DISCOVERED,
            CandidateEventType.RECONNAISSANCE_STARTED,
            CandidateEventType.QUALIFIED,
        ]

        # No truth-only/password tokens in prompts (FakeLlm recorded them)
        # or in persisted assessment/history rows.
        prompts = "\n".join(
            f"{call.system_prompt}\n{call.user_prompt}" for call in llm.calls
        )
        assert _leak_tokens_in_text(prompts) == []
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT characteristics, evidence_references "
                    "FROM recon_assessment WHERE candidate_id = %s",
                    (str(candidate.candidate_id),),
                )
                rows = cur.fetchall()
                cur.execute(
                    "SELECT context, reason FROM source_candidate_event_history "
                    "WHERE candidate_id = %s",
                    (str(candidate.candidate_id),),
                )
                rows_history = cur.fetchall()
                cur.execute("SELECT count(*) FROM source")
                sources = cur.fetchone()
                cur.execute("SELECT count(*) FROM collection_policy")
                policies = cur.fetchone()
        finally:
            conn.close()
        persisted = " ".join(
            str(value) for row in [*rows, *rows_history] for value in row if value
        )
        assert _leak_tokens_in_text(persisted) == []
        assert sources is not None and sources[0] == 0  # no Source created
        assert policies is not None and policies[0] == 0  # no policy created

    @pytest.mark.asyncio
    async def test_152_cross_origin_rejection_no_crawl(
        self,
        controller: CrawlerController,
        spi: PostgresDarkulaSpi,
    ) -> None:
        candidate = await _seed_candidate(spi)
        crawler = _CountingCrawler(controller)
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_inspect("http://evil.example.test/steal"))
        coordinator = _coordinator(spi, llm=llm, crawler=crawler)

        with pytest.raises(ReconInspectionRejectedError):
            await coordinator.reconnoiter(candidate.candidate_id)

        # No crawler/sandbox execution happened for the rejected request.
        assert crawler.count == 0
        # The recoverable protocol failure left a retryable pending state and
        # no fabricated analytical REJECT assessment.
        async with spi.unit_of_work() as uow:
            fresh = await uow.source_candidates.get(candidate.candidate_id)
            assessments = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
            history = await uow.source_candidates.list_history(candidate.candidate_id)
        assert (
            fresh is not None and fresh.status is CandidateStatus.RECONNAISSANCE_PENDING
        )
        assert assessments == ()
        assert [h.event_type for h in history] == [
            CandidateEventType.DISCOVERED,
            CandidateEventType.RECONNAISSANCE_STARTED,
            CandidateEventType.NEEDS_MORE_RECON,
        ]

    @pytest.mark.asyncio
    async def test_153_needs_more_then_second_execution_appends(
        self,
        controller: CrawlerController,
        spi: PostgresDarkulaSpi,
    ) -> None:
        candidate = await _seed_candidate(spi)
        crawler = _CountingCrawler(controller)
        clock = _AdvancingClock(_T0 + timedelta(seconds=1))

        # Execution 1: analytical NEEDS_MORE_RECON (no crawl needed).
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_complete(ReconDisposition.NEEDS_MORE_RECON))
        coordinator = _coordinator(spi, llm=llm, crawler=crawler, clock=clock)
        first = await coordinator.reconnoiter(candidate.candidate_id)
        assert first.final_status is CandidateStatus.RECONNAISSANCE_PENDING

        # Execution 2: a new bounded loop appends an independent assessment
        # without rewriting history.
        llm2 = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm2.enqueue(_inspect("/board/board-announcements"))
        llm2.enqueue(
            _complete(ReconDisposition.QUALIFY, refs=(f"{_EXECUTION_ID}:1:1",))
        )
        coordinator2 = _coordinator(spi, llm=llm2, crawler=crawler, clock=clock)
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

    @pytest.mark.asyncio
    async def test_154_reject_no_source_and_terminal_replay_no_work(
        self,
        controller: CrawlerController,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
    ) -> None:
        candidate = await _seed_candidate(spi)
        crawler = _CountingCrawler(controller)
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_complete(ReconDisposition.REJECT))
        coordinator = _coordinator(spi, llm=llm, crawler=crawler)
        result = await coordinator.reconnoiter(candidate.candidate_id)
        assert result.final_status is CandidateStatus.REJECTED

        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM source")
                row = cur.fetchone()
            assert row is not None and row[0] == 0  # no Source created
        finally:
            conn.close()

        # Terminal replay invokes no LLM/crawler and appends nothing.
        llm2 = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm2.enqueue(_complete(ReconDisposition.QUALIFY))
        coordinator2 = _coordinator(spi, llm=llm2, crawler=crawler)
        with pytest.raises(ReconCandidateNotEligibleError):
            await coordinator2.reconnoiter(candidate.candidate_id)
        assert llm2.call_count == 0
        assert crawler.count == 0
        async with spi.unit_of_work() as uow:
            assessments = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
        assert len(assessments) == 1  # only the analytical REJECT assessment

    @pytest.mark.asyncio
    async def test_155_concurrent_start_one_winner(
        self,
        controller: CrawlerController,
        spi: PostgresDarkulaSpi,
    ) -> None:
        candidate = await _seed_candidate(spi)
        crawler = _CountingCrawler(controller)
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_inspect("/index"))
        llm.enqueue(_complete(ReconDisposition.QUALIFY, refs=(f"{_EXECUTION_ID}:1:1",)))
        coordinator = _coordinator(spi, llm=llm, crawler=crawler)

        async def run() -> Any:
            return await coordinator.reconnoiter(candidate.candidate_id)

        outcomes = await asyncio.gather(run(), run(), return_exceptions=True)
        results = [item for item in outcomes if not isinstance(item, BaseException)]
        errors = [item for item in outcomes if isinstance(item, BaseException)]
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ReconCandidateNotEligibleError)
        assert llm.call_count == 2  # exactly one execution reached the model
        assert crawler.count == 1  # exactly one crawl

        async with spi.unit_of_work() as uow:
            assessments = await uow.source_candidates.list_recon_assessments(
                candidate.candidate_id
            )
            history = await uow.source_candidates.list_history(candidate.candidate_id)
        assert len(assessments) == 1
        started = [
            h
            for h in history
            if h.event_type is CandidateEventType.RECONNAISSANCE_STARTED
        ]
        assert len(started) == 1  # no duplicate STARTED event
