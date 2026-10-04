# SPDX-License-Identifier: AGPL-3.0-only
"""SourceCollectionService tests (EX1-EX14, MAP1-MAP8, PR 9).

Deterministic, offline: the real service is exercised against the in-memory
collection persistence fakes, a scripted crawler, and a scripted ingest
double. The crawler and ingest boundaries are faked; the orchestration,
admission, finalization, and failure semantics under test are the real PR 9
code.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.support.collection_fakes import (
    FakeCrawler,
    FakeIngest,
    MemSpi,
    seed_endpoint,
    seed_source,
)

from darkula.app.collection import (
    CollectionLeaseConflictError,
    CollectionNotFoundError,
    CollectionPolicyError,
    SourceCollectionService,
    deterministic_request_id,
    policy_execution_from_snapshot,
)
from darkula.app.normalization import InvalidContentObservationError
from darkula.app.persistence import PersistenceError
from darkula.config.settings import CollectionSettings
from darkula.crawler.contracts import CrawlStatus
from darkula.domain.collection import (
    CollectionFailureCode,
    CollectionPolicy,
    CollectionRun,
    CollectionRunStatus,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    SourceEndpointId,
    SourceId,
)
from darkula.domain.source import EndpointStatus, SourceStatus

_T0 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
_POLICY = CollectionPolicyId.from_str("11111111-1111-1111-1111-111111111111")
_SOURCE = SourceId.from_str("22222222-2222-2222-2222-222222222222")
_SOURCE_OTHER = SourceId.from_str("99999999-9999-9999-9999-999999999999")
_ENDPOINT = SourceEndpointId.from_str("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ENDPOINT_B = SourceEndpointId.from_str("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_URI = "http://blackgate.example.test/board/board-announcements"


class _Clock:
    """Scripted deterministic UTC clock."""

    def __init__(self, moment: datetime = _T0) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: int) -> None:
        from datetime import timedelta

        self.moment = self.moment + timedelta(seconds=seconds)


def _build_context(
    *,
    endpoint_uri: str = _URI,
    endpoint_status: str = "ACTIVE",
    endpoint_source: SourceId = _SOURCE,
    policy_active: bool = True,
    source_status: str = "ACTIVE",
    snapshot_endpoint_ids: tuple[SourceEndpointId, ...] = (_ENDPOINT,),
    max_pages: int = 20,
    max_requests: int = 60,
    max_depth: int = 4,
    timeout_seconds: float = 60.0,
    interval_seconds: int = 3600,
    include_snapshot: bool = True,
) -> tuple[MemSpi, CollectionPolicy, CollectionRun]:
    """Seed a deterministic service scenario; returns (spi, policy, run)."""
    spi = MemSpi()
    source = seed_source(spi.state, _SOURCE)
    if source_status != "ACTIVE":
        source = replace(source, status=SourceStatus(source_status))
        spi.state.sources[_SOURCE] = source
    seed_source(spi.state, _SOURCE_OTHER)
    endpoint = seed_endpoint(spi.state, _ENDPOINT, endpoint_source, endpoint_uri)
    if endpoint_status != "ACTIVE":
        endpoint = replace(
            endpoint,
            status=EndpointStatus(endpoint_status),
        )
        spi.state.endpoints[_ENDPOINT] = endpoint
    endpoint_b = seed_endpoint(
        spi.state, _ENDPOINT_B, _SOURCE, "http://blackgate.example.test/archive"
    )
    spi.state.endpoints[_ENDPOINT_B] = endpoint_b
    policy = CollectionPolicy(
        policy_id=_POLICY,
        source_id=_SOURCE,
        active=policy_active,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=interval_seconds,
        next_due_at=_T0,
        allowed_endpoint_ids=snapshot_endpoint_ids,
        max_pages=max_pages,
        max_requests=max_requests,
        max_depth=max_depth,
        timeout_seconds=timeout_seconds,
        authentication_reference="vault-entry-42",
    )
    spi.state.policies[_POLICY] = policy
    run = CollectionRun(
        run_id=CollectionRunId.generate(),
        policy_id=_POLICY,
        policy_revision=policy.revision,
        source_id=_SOURCE,
        scheduled_for=_T0,
        created_at=_T0,
        status=CollectionRunStatus.QUEUED,
        policy_snapshot=policy.execution_snapshot() if include_snapshot else None,
    )
    spi.state.runs[run.run_id] = run
    return spi, policy, run


def _service(
    spi: MemSpi,
    crawler: FakeCrawler | None = None,
    ingest: FakeIngest | None = None,
    *,
    clock: _Clock | None = None,
    settings: CollectionSettings | None = None,
) -> SourceCollectionService:
    return SourceCollectionService(
        spi=spi,
        crawler=crawler or FakeCrawler(),
        content_ingest=ingest or FakeIngest(),
        settings=settings or CollectionSettings(),
        clock=clock or _Clock(),
    )


class TestExecutionHappyPath:
    """EX1/EX6/EX10: valid queued run executes and finalizes."""

    @pytest.mark.asyncio
    async def test_ex1_execute_succeeds(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        result = await _service(spi, crawler=crawler).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.SUCCEEDED
        persisted = spi.state.runs[run.run_id]
        assert persisted.status is CollectionRunStatus.SUCCEEDED
        assert persisted.completed_at is not None
        assert persisted.attempt_count == 1
        assert len(crawler.requests) == 1

    @pytest.mark.asyncio
    async def test_ex6_crawl_success_feeds_ingest(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(2))
        ingest = FakeIngest()
        await _service(spi, crawler=crawler, ingest=ingest).execute(run.run_id)
        assert len(crawler.requests) == 1
        assert len(ingest.observations) == 2
        # Provenance identity preserved through the trusted mapper index.
        assert [obs.observation_index for obs in ingest.observations] == [1, 2]

    @pytest.mark.asyncio
    async def test_ex10_success_persists_accurate_counters(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(3))
        result = await _service(
            spi,
            crawler=crawler,
            ingest=FakeIngest(created_observation=True, deduplicated=True),
        ).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.SUCCEEDED
        assert result.crawl_requests_attempted == 1
        assert result.pages_observed == 3
        assert result.content_observations == 3
        assert result.content_created == 3
        assert result.content_deduplicated == 3
        persisted = spi.state.runs[run.run_id]
        assert persisted.content_observations == 3

    @pytest.mark.asyncio
    async def test_ex10_deduplicated_ingest_counts_created_accurately(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(2))
        result = await _service(
            spi, crawler=crawler, ingest=FakeIngest(created_observation=False)
        ).execute(run.run_id)
        assert result.content_observations == 2
        assert result.content_created == 0

    @pytest.mark.asyncio
    async def test_ex12_no_database_transaction_during_crawl_or_ingest(
        self,
    ) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        ingest = FakeIngest()

        def probe() -> None:
            # No unit of work may be open while external work runs.
            assert not spi.state.lock.locked(), "a UoW was open during external I/O"

        crawler.probe = probe
        ingest.probe = probe
        await _service(spi, crawler=crawler, ingest=ingest).execute(run.run_id)
        assert not spi.state.lock.locked()


class TestTerminalAndUnknown:
    """EX2: unknown/terminal runs never crawl."""

    @pytest.mark.asyncio
    async def test_ex2_unknown_run_is_poison_not_found(self) -> None:
        spi, _, _ = _build_context()
        with pytest.raises(CollectionNotFoundError):
            await _service(spi).execute(CollectionRunId.generate())

    @pytest.mark.asyncio
    async def test_ex2_terminal_run_no_crawl(self) -> None:
        spi, _, run = _build_context()
        terminal = replace(
            run,
            status=CollectionRunStatus.SUCCEEDED,
            started_at=_T0,
            completed_at=_T0,
            content_observations=4,
        )
        spi.state.runs[run.run_id] = terminal
        crawler = FakeCrawler()
        result = await _service(spi, crawler=crawler).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.SUCCEEDED
        assert result.content_observations == 4
        assert crawler.requests == []


class TestAuthorizationBeforeStart:
    """EX3/EX4: withdrawn authorization means no crawl."""

    @pytest.mark.asyncio
    async def test_ex3_inactive_policy_no_crawl_cancelled(self) -> None:
        spi, _, run = _build_context(policy_active=False)
        crawler = FakeCrawler()
        result = await _service(spi, crawler=crawler).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.CANCELLED
        assert result.failure_code is CollectionFailureCode.POLICY_INACTIVE
        assert crawler.requests == []
        assert spi.state.runs[run.run_id].status is CollectionRunStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_ex3_inactive_source_no_crawl_cancelled(self) -> None:
        spi, _, run = _build_context(source_status="INACTIVE")
        crawler = FakeCrawler()
        result = await _service(spi, crawler=crawler).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.CANCELLED
        assert result.failure_code is CollectionFailureCode.SOURCE_INACTIVE
        assert crawler.requests == []

    @pytest.mark.asyncio
    async def test_ex4_inactive_snapshot_endpoint_no_crawl(self) -> None:
        spi, _, run = _build_context(endpoint_status="INACTIVE")
        crawler = FakeCrawler()
        result = await _service(spi, crawler=crawler).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.CANCELLED
        assert result.failure_code is CollectionFailureCode.NO_ACTIVE_ENDPOINT
        assert crawler.requests == []


class TestEndpointMapping:
    """MAP1-MAP8: deterministic CrawlRequest mapping."""

    @pytest.mark.asyncio
    async def test_map1_active_owned_endpoint_maps_request(self) -> None:
        spi, policy, run = _build_context(
            endpoint_uri=_URI,
            max_pages=7,
            max_requests=150,
            max_depth=3,
            timeout_seconds=42.0,
        )
        crawler = FakeCrawler()
        await _service(spi, crawler=crawler).execute(run.run_id)
        request = crawler.requests[0]
        assert request.start_url == _URI
        assert request.allowed_origin.netloc() == "blackgate.example.test:80"
        assert request.max_pages == policy.max_pages == 7
        assert request.max_requests == 150
        assert request.max_depth == 3
        assert request.timeout_seconds == 42.0

    @pytest.mark.asyncio
    async def test_map2_endpoint_of_other_source_rejected(self) -> None:
        # The frozen snapshot authorizes an endpoint that (after a source
        # reassignment) no longer belongs to the run's source: no crawl.
        spi, _, run = _build_context(endpoint_source=_SOURCE_OTHER)
        crawler = FakeCrawler()
        result = await _service(spi, crawler=crawler).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.CANCELLED
        assert result.failure_code is CollectionFailureCode.NO_ACTIVE_ENDPOINT
        assert crawler.requests == []

    @pytest.mark.asyncio
    async def test_map3_inactive_endpoint_skips_run(self) -> None:
        spi, _, run = _build_context(endpoint_status="INACTIVE")
        crawler = FakeCrawler()
        result = await _service(spi, crawler=crawler).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.CANCELLED
        assert crawler.requests == []

    @pytest.mark.asyncio
    async def test_map4_paths_and_budgets_are_carried(self) -> None:
        spi, _, run = _build_context(
            max_pages=11,
            max_requests=99,
            max_depth=5,
            timeout_seconds=33.0,
        )
        crawler = FakeCrawler()
        await _service(spi, crawler=crawler).execute(run.run_id)
        request = crawler.requests[0]
        # MAP4: policy budgets map onto the PR 7 request. MAP5: the PR 7
        # controller clamps against settings maxima at the crawler boundary.
        assert request.max_pages == 11
        assert request.max_requests == 99
        assert request.max_depth == 5
        assert request.timeout_seconds == 33.0

    def test_map5_controller_remains_enforcement_boundary(self) -> None:
        # The service passes policy budgets through; the CrawlRequest
        # contract itself enforces the hard PR 7 bounds, so a hand-built
        # out-of-range request still fails at the crawler boundary.
        from darkula.crawler.contracts import (
            AllowedOrigin,
            CrawlRequest,
            InvalidCrawlRequest,
        )

        with pytest.raises(InvalidCrawlRequest):
            CrawlRequest(
                request_id="req-1",
                start_url=_URI,
                allowed_origin=AllowedOrigin.from_url(_URI),
                max_pages=99_999,
            )

    def test_map6_same_run_endpoint_attempt_stable_request_id(self) -> None:
        run = _run_id()
        a = deterministic_request_id(run_id=run, endpoint_id=_ENDPOINT, attempt=1)
        b = deterministic_request_id(run_id=run, endpoint_id=_ENDPOINT, attempt=1)
        assert a == b

    def test_map7_next_attempt_distinct_request_id(self) -> None:
        run = _run_id()
        a = deterministic_request_id(run_id=run, endpoint_id=_ENDPOINT, attempt=1)
        b = deterministic_request_id(run_id=run, endpoint_id=_ENDPOINT, attempt=2)
        assert a != b

    @pytest.mark.asyncio
    async def test_map8_credential_reference_never_a_secret_value(self) -> None:
        spi, policy, run = _build_context()
        assert policy.authentication_reference == "vault-entry-42"
        crawler = FakeCrawler()
        await _service(spi, crawler=crawler).execute(run.run_id)
        assert crawler.requests[0].credentials is None
        # The reference never leaks into the crawl request or payload.
        serialized = str(crawler.requests[0])
        assert "vault-entry-42" not in serialized
        assert "password" not in serialized.lower()


class TestFailures:
    """EX8/EX9/EX13: ordinary failures finalize FAILED boundedly."""

    @pytest.mark.asyncio
    async def test_ex8_crawl_failure_finalizes_failed(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler(
            results=[
                _crawl_result(run, CrawlStatus.TIMED_OUT, reason="sandbox wall-clock")
            ]
        )
        result = await _service(spi, crawler=crawler).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.FAILED
        assert result.failure_code is CollectionFailureCode.CRAWL_FAILED
        assert "timed_out" in (result.failure_summary or "")
        persisted = spi.state.runs[run.run_id]
        assert persisted.failure_code is CollectionFailureCode.CRAWL_FAILED
        assert "sandbox wall-clock" not in (persisted.failure_summary or "")

    @pytest.mark.asyncio
    async def test_ex9_ingest_normalization_failure_finalizes_failed(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        result = await _service(
            spi,
            crawler=crawler,
            ingest=FakeIngest(
                failure=InvalidContentObservationError("malformed observation")
            ),
        ).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.FAILED
        assert result.failure_code is CollectionFailureCode.NORMALIZATION_FAILED
        assert "malformed" not in (result.failure_summary or "")

    @pytest.mark.asyncio
    async def test_ex9_ingest_persistence_failure_finalizes_failed(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        result = await _service(
            spi,
            crawler=crawler,
            ingest=FakeIngest(failure=PersistenceError("connection dropped")),
        ).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.FAILED
        assert result.failure_code is CollectionFailureCode.CONTENT_PERSISTENCE_FAILED

    @pytest.mark.asyncio
    async def test_ex13_raw_exception_never_persisted(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        service = _service(
            spi,
            crawler=crawler,
            ingest=FakeIngest(
                failure=RuntimeError("SECRET-SENSITIVE-RAW-TEXT explosion")
            ),
        )
        with pytest.raises(RuntimeError):
            await service.execute(run.run_id)
        persisted = spi.state.runs[run.run_id]
        # Unknown failures are NOT finalizeable: the run stays RUNNING under
        # its lease so redelivery can reclaim it (WK7). Never any raw text.
        assert persisted.status is CollectionRunStatus.RUNNING
        assert persisted.failure_summary is None
        assert "SECRET" not in str(persisted)


class TestCancellation:
    """EX11: cancellation cleans up, persists best-effort, propagates."""

    @pytest.mark.asyncio
    async def test_ex11_cancellation_persists_cancelled_and_propagates(
        self,
    ) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        service = _service(
            spi,
            crawler=crawler,
            ingest=FakeIngest(failure=asyncio.CancelledError()),
        )
        with pytest.raises(asyncio.CancelledError):
            await service.execute(run.run_id)
        persisted = spi.state.runs[run.run_id]
        assert persisted.status is CollectionRunStatus.CANCELLED
        assert persisted.completed_at is not None

    @pytest.mark.asyncio
    async def test_ex11_crawl_cancellation_propagates(self) -> None:
        spi, _, run = _build_context()

        class _CancellingCrawler(FakeCrawler):
            async def crawl(self, request: Any) -> Any:
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await _service(spi, crawler=_CancellingCrawler()).execute(run.run_id)
        assert spi.state.runs[run.run_id].status is CollectionRunStatus.CANCELLED


class TestConcurrencyAndFrozenSemantics:
    """EX5/EX7/EX14: one winner; replay safe; frozen in-flight contract."""

    @pytest.mark.asyncio
    async def test_ex5_concurrent_starts_one_wins(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        service = _service(spi, crawler=crawler)
        results = await asyncio.gather(
            service.execute(run.run_id),
            service.execute(run.run_id),
            return_exceptions=True,
        )
        outcomes = [item for item in results if not isinstance(item, BaseException)]
        conflicts = [
            item for item in results if isinstance(item, CollectionLeaseConflictError)
        ]
        # Exactly one crawl ever executes: either the second caller observed
        # a lease conflict (true race) or the run was already terminal (fully
        # serialized event loop) — both are at-least-once safe. Never two
        # concurrent crawls.
        assert len(outcomes) >= 1
        assert len(conflicts) <= 1
        assert len(crawler.requests) == 1
        assert spi.state.runs[run.run_id].status is CollectionRunStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_ex7_replay_after_success_does_not_recrawl(self) -> None:
        spi, _, run = _build_context()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        service = _service(spi, crawler=crawler)
        first = await service.execute(run.run_id)
        assert first.run_status is CollectionRunStatus.SUCCEEDED
        second = await service.execute(run.run_id)
        assert second.run_status is CollectionRunStatus.SUCCEEDED
        assert len(crawler.requests) == 1  # replay no crawl

    @pytest.mark.asyncio
    async def test_ex14_policy_edit_after_running_uses_frozen_semantics(
        self,
    ) -> None:
        spi, policy, run = _build_context(max_pages=5)
        # Claim the run (QUEUED -> RUNNING) as another worker would.
        async with spi.unit_of_work() as uow:
            outcome = await uow.collection.claim_run(
                run_id=run.run_id,
                execution_id="exec-1",
                started_at=_T0,
                lease_expires_at=_T0,
            )
            assert outcome.value == "claimed"
            await uow.commit()
        # Edit the policy now that the run is RUNNING (revision bumps).
        edited = replace(
            policy,
            active=True,
            updated_at=_T0,
            max_pages=50,
            next_due_at=_T0,
        )
        async with spi.unit_of_work() as uow:
            new_revision = await uow.collection.update_policy(edited)
            await uow.commit()
        assert new_revision == policy.revision + 1
        # Reclaim + execute: the frozen snapshot (max_pages=5) governs.

        clock = _Clock()
        clock.advance(3600)  # lease expired
        crawler = FakeCrawler()
        result = await _service(spi, crawler=crawler, clock=clock).execute(run.run_id)
        assert result.run_status is CollectionRunStatus.SUCCEEDED
        assert crawler.requests[0].max_pages == 5  # frozen, not 50
        persisted = spi.state.runs[run.run_id]
        assert persisted.policy_revision == 1
        assert persisted.attempt_count == 2  # claim + reclaim

    @pytest.mark.asyncio
    async def test_policy_edit_creates_new_revision_for_future_runs(self) -> None:
        spi, policy, _ = _build_context()
        edited = replace(
            policy,
            updated_at=_T0,
            max_pages=9,
            next_due_at=_T0,
            active=False,
        )
        async with spi.unit_of_work() as uow:
            revision = await uow.collection.update_policy(edited)
            await uow.commit()
        assert revision == 2
        # Old run semantics are retained by its revision + snapshot.
        run = next(iter(spi.state.runs.values()))
        assert run.policy_revision == 1


class TestSnapshotStrictness:
    """The frozen snapshot is the authoritative execution contract."""

    @pytest.mark.asyncio
    async def test_missing_snapshot_is_invalid_state_no_crawl(self) -> None:
        spi, _, run = _build_context(include_snapshot=False)
        crawler = FakeCrawler()
        result = await _service(spi, crawler=crawler).execute(run.run_id)
        # An unreadable snapshot means no crawl; admission cancels the run.
        assert result.run_status is CollectionRunStatus.CANCELLED
        assert crawler.requests == []

    def test_snapshot_with_unknown_keys_rejected(self) -> None:
        snapshot = {
            "policy_revision": 1,
            "interval_seconds": 3600,
            "allowed_endpoint_ids": [str(_ENDPOINT)],
            "allowed_paths": [],
            "max_pages": 20,
            "max_requests": 60,
            "max_depth": 4,
            "timeout_seconds": 60.0,
            "authentication_reference": None,
            "surprise": 1,
        }
        with pytest.raises(CollectionPolicyError):
            policy_execution_from_snapshot(snapshot)

    def test_snapshot_wrong_shapes_rejected(self) -> None:
        with pytest.raises(CollectionPolicyError):
            policy_execution_from_snapshot(None)
        bad = {
            "policy_revision": 1,
            "interval_seconds": 3600,
            "allowed_endpoint_ids": "not-a-list",
            "allowed_paths": [],
            "max_pages": 20,
            "max_requests": 60,
            "max_depth": 4,
            "timeout_seconds": 60.0,
            "authentication_reference": None,
        }
        with pytest.raises(CollectionPolicyError):
            policy_execution_from_snapshot(bad)


def _run_id() -> CollectionRunId:
    """Deterministic run identity for MAP6/MAP7 stability tests."""
    return CollectionRunId.from_str("33333333-3333-3333-3333-333333333333")


def _crawl_result(run: CollectionRun, status: CrawlStatus, reason: str) -> Any:
    from darkula.crawler.contracts import CrawlResult

    return CrawlResult(
        request_id="req-1",
        status=status,
        reason=reason,
        requests=0,
        duration_seconds=0.0,
    )
