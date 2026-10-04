# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic in-memory collection fakes for PR 9 unit tests.

Implements the :class:`~darkula.app.repositories.CollectionRepository`
contract plus the small parts of ``UnitOfWork`` (sources, outbox,
processed messages) the PR 9 app layer depends on, with explicit
commit/rollback semantics: pending mutations are staged inside a unit of
work and become visible only on ``commit`` (SCH8/SCH9). The fake mirrors the
real stored-function invariants that the unit tests exercise:

- occurrence uniqueness ``(policy_id, scheduled_for)`` decides duplicate
  scheduling (SCH5);
- expected-state transitions (QUEUED -> RUNNING -> terminal) reject
  out-of-order claims (CR7/EX5);
- lease expiry + attempt bound decide reclaims (WK4/WK5/WK6);
- the processed-message marker deduplicates deliveries by ``message_id``.

The fake never touches PostgreSQL, Redpanda, the crawler, or ObjectStore;
it is a test double for persistence boundaries only (the behavior under
test — scheduler/service/worker orchestration — is the real code).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from darkula.app.content import ContentIngestService, IngestResult
from darkula.app.data_stream import StreamMessage
from darkula.app.persistence import (
    ConflictError,
    DarkulaSpi,
    IntegrityError,
    NotFoundError,
    UnitOfWork,
)
from darkula.app.repositories import (
    ClaimOutcome,
    CollectionRepository,
    OutboxRepository,
    ProcessedMessageRepository,
    ReclaimOutcome,
    ScheduledOccurrence,
    ScheduleOutcome,
)
from darkula.crawler.contracts import (
    Crawler,
    CrawlRequest,
    CrawlResult,
    CrawlStatus,
)
from darkula.domain.collection import (
    TERMINAL_RUN_STATUSES,
    CollectionFailureCode,
    CollectionPolicy,
    CollectionRun,
    CollectionRunStatus,
)
from darkula.domain.content import (
    ArtifactCompleteness,
    ContentObservation,
    NormalizedContent,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    ConsumerId,
    MessageId,
    NormalizedContentId,
    SourceEndpointId,
    SourceId,
    StreamName,
)
from darkula.domain.source import (
    EndpointStatus,
    EndpointType,
    Source,
    SourceEndpoint,
    SourceStatus,
)

_UUID_STR = str


@dataclass(slots=True)
class MemState:
    """Committed fake persistence state."""

    policies: dict[CollectionPolicyId, CollectionPolicy] = field(default_factory=dict)
    endpoints: dict[SourceEndpointId, SourceEndpoint] = field(default_factory=dict)
    runs: dict[CollectionRunId, CollectionRun] = field(default_factory=dict)
    occurrences: set[tuple[CollectionPolicyId, datetime]] = field(default_factory=set)
    sources: dict[SourceId, Source] = field(default_factory=dict)
    outbox: dict[StreamName, list[StreamMessage]] = field(default_factory=dict)
    processed: set[tuple[str, str, str]] = field(default_factory=set)
    #: Serializes open transactions so a UoW's pending mutations are applied
    #: atomically vs. other UoWs (models PostgreSQL row/serialization locks).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class _PendingState:
    """Mutations staged inside one open unit of work."""

    policies: dict[CollectionPolicyId, CollectionPolicy] = field(default_factory=dict)
    runs: dict[CollectionRunId, CollectionRun] = field(default_factory=dict)
    occurrences: set[tuple[CollectionPolicyId, datetime]] = field(default_factory=set)
    outbox: dict[StreamName, list[StreamMessage]] = field(default_factory=dict)
    processed: set[tuple[str, str, str]] = field(default_factory=set)


class MemCollectionRepository(CollectionRepository):
    """In-memory CollectionRepository over one unit of work's pending state."""

    def __init__(self, state: MemState, pending: _PendingState) -> None:
        self._state = state
        self._pending = pending

    def _policy_state(self) -> dict[CollectionPolicyId, CollectionPolicy]:
        return {**self._state.policies, **self._pending.policies}

    def _run_state(self) -> dict[CollectionRunId, CollectionRun]:
        return {**self._state.runs, **self._pending.runs}

    async def create_policy(self, policy: CollectionPolicy) -> None:
        if policy.policy_id in self._policy_state():
            raise ConflictError("a policy with this identity already exists")
        for endpoint_id in policy.allowed_endpoint_ids:
            endpoint = self._state.endpoints.get(endpoint_id)
            if endpoint is None:
                raise IntegrityError("an authorized endpoint does not exist")
            if endpoint.source_id != policy.source_id:
                raise IntegrityError("an authorized endpoint belongs to another source")
        self._pending.policies[policy.policy_id] = policy

    async def get_policy(
        self, policy_id: CollectionPolicyId
    ) -> CollectionPolicy | None:
        return self._policy_state().get(policy_id)

    async def update_policy(self, policy: CollectionPolicy) -> int:
        current = self._policy_state().get(policy.policy_id)
        if current is None:
            raise NotFoundError("the policy does not exist")
        for endpoint_id in policy.allowed_endpoint_ids:
            endpoint = self._state.endpoints.get(endpoint_id)
            if endpoint is None:
                raise IntegrityError("an authorized endpoint does not exist")
            if endpoint.source_id != policy.source_id:
                raise IntegrityError("an authorized endpoint belongs to another source")
        self._pending.policies[policy.policy_id] = replace(
            policy, revision=current.revision + 1
        )
        return current.revision + 1

    async def list_policy_endpoints(
        self, policy_id: CollectionPolicyId
    ) -> tuple[SourceEndpoint, ...]:
        policy = self._policy_state().get(policy_id)
        if policy is None:
            return ()
        return tuple(
            sorted(
                (
                    self._state.endpoints[endpoint_id]
                    for endpoint_id in policy.allowed_endpoint_ids
                    if endpoint_id in self._state.endpoints
                ),
                key=lambda endpoint: str(endpoint.endpoint_id),
            )
        )

    async def get_endpoint(
        self, endpoint_id: SourceEndpointId
    ) -> SourceEndpoint | None:
        return self._state.endpoints.get(endpoint_id)

    async def create_run(self, run: CollectionRun) -> None:
        if run.run_id in self._run_state():
            raise ConflictError("a run with this identity already exists")
        if (run.policy_id, run.scheduled_for) in (
            self._state.occurrences | self._pending.occurrences
        ):
            raise ConflictError("a run for this policy occurrence already exists")
        if run.policy_id not in self._policy_state():
            raise IntegrityError("the referenced policy does not exist")
        self._pending.runs[run.run_id] = run
        self._pending.occurrences.add((run.policy_id, run.scheduled_for))

    async def get_run(self, run_id: CollectionRunId) -> CollectionRun | None:
        return self._run_state().get(run_id)

    async def schedule_due(
        self,
        *,
        now: datetime,
        run_id: CollectionRunId,
        created_at: datetime,
    ) -> ScheduledOccurrence | None:
        due = [
            policy
            for policy in self._policy_state().values()
            if policy.active and policy.next_due_at <= now
        ]
        if not due:
            return None
        policy = sorted(due, key=lambda item: (item.next_due_at, str(item.policy_id)))[
            0
        ]
        scheduled_for = policy.next_due_at
        occurrence_id = (policy.policy_id, scheduled_for)
        if occurrence_id in (self._state.occurrences | self._pending.occurrences):
            return ScheduledOccurrence(
                run_id=run_id,
                policy_id=policy.policy_id,
                policy_revision=policy.revision,
                policy_snapshot=None,
                source_id=policy.source_id,
                scheduled_for=scheduled_for,
                outcome=ScheduleOutcome.OCCURRENCE_EXISTS,
            )
        snapshot = policy.execution_snapshot()
        run = CollectionRun(
            run_id=run_id,
            policy_id=policy.policy_id,
            policy_revision=policy.revision,
            source_id=policy.source_id,
            scheduled_for=scheduled_for,
            created_at=created_at,
            status=CollectionRunStatus.QUEUED,
            policy_snapshot=snapshot,
        )
        self._pending.runs[run.run_id] = run
        self._pending.occurrences.add(occurrence_id)
        next_due = scheduled_for
        while next_due <= now:
            next_due = _add_seconds(next_due, policy.interval_seconds)
        self._pending.policies[policy.policy_id] = replace(policy, next_due_at=next_due)
        return ScheduledOccurrence(
            run_id=run_id,
            policy_id=policy.policy_id,
            policy_revision=policy.revision,
            policy_snapshot=snapshot,
            source_id=policy.source_id,
            scheduled_for=scheduled_for,
            outcome=ScheduleOutcome.SCHEDULED,
        )

    async def claim_run(
        self,
        *,
        run_id: CollectionRunId,
        execution_id: str,
        started_at: datetime,
        lease_expires_at: datetime,
    ) -> ClaimOutcome:
        run = self._run_state().get(run_id)
        if run is None:
            return ClaimOutcome.UNKNOWN_RUN
        if run.status is not CollectionRunStatus.QUEUED:
            return ClaimOutcome.NOT_QUEUED
        self._pending.runs[run_id] = replace(
            run,
            status=CollectionRunStatus.RUNNING,
            execution_id=execution_id,
            started_at=started_at,
            lease_expires_at=lease_expires_at,
            attempt_count=run.attempt_count + 1,
        )
        return ClaimOutcome.CLAIMED

    async def reclaim_run(
        self,
        *,
        run_id: CollectionRunId,
        execution_id: str,
        now: datetime,
        lease_expires_at: datetime,
        max_attempts: int,
    ) -> ReclaimOutcome:
        run = self._run_state().get(run_id)
        if run is None:
            return ReclaimOutcome.UNKNOWN_RUN
        if run.status is not CollectionRunStatus.RUNNING:
            return ReclaimOutcome.TERMINAL
        if run.lease_expires_at is not None and run.lease_expires_at > now:
            return ReclaimOutcome.LEASE_ACTIVE
        if run.attempt_count >= max_attempts:
            return ReclaimOutcome.ATTEMPTS_EXHAUSTED
        self._pending.runs[run_id] = replace(
            run,
            execution_id=execution_id,
            lease_expires_at=lease_expires_at,
            attempt_count=run.attempt_count + 1,
        )
        return ReclaimOutcome.RECLAIMED

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
        run = self._run_state().get(run_id)
        if run is None:
            raise NotFoundError("the run does not exist")
        if run.status is not CollectionRunStatus.RUNNING:
            raise ConflictError("the run is not in the expected executing state")
        self._pending.runs[run_id] = replace(
            run,
            status=new_status,
            completed_at=completed_at,
            crawl_requests_attempted=crawl_requests_attempted,
            pages_observed=pages_observed,
            content_observations=content_observations,
            content_created=content_created,
            content_deduplicated=content_deduplicated,
            failure_code=failure_code,
            failure_summary=failure_summary,
            execution_id=None,
            lease_expires_at=None,
        )

    async def cancel_run(
        self,
        *,
        run_id: CollectionRunId,
        completed_at: datetime,
    ) -> None:
        run = self._run_state().get(run_id)
        if run is None:
            raise NotFoundError("the run does not exist")
        if run.status in TERMINAL_RUN_STATUSES:
            raise ConflictError("the run is already terminal")
        self._pending.runs[run_id] = replace(
            run,
            status=CollectionRunStatus.CANCELLED,
            completed_at=completed_at,
            execution_id=None,
            lease_expires_at=None,
        )


class _MemOutboxRepository(OutboxRepository):
    def __init__(self, state: MemState, pending: _PendingState) -> None:
        self._state = state
        self._pending = pending

    async def append(self, message: StreamMessage, *, stream_name: StreamName) -> None:
        existing_ids = {
            item.message_id for item in self._pending.outbox.get(stream_name, ())
        }
        if message.message_id in existing_ids:
            raise ConflictError("an outbox record with this message identity exists")
        self._pending.outbox.setdefault(stream_name, []).append(message)

    async def claim(
        self,
        *,
        stream_name: StreamName,
        limit: int,
        claim_id: Any,
        lease_seconds: float,
    ) -> tuple[Any, ...]:
        raise AssertionError("scheduler unit tests never claim outbox rows")

    async def mark_published(
        self, *, claim_id: Any, outbox_ids: tuple[Any, ...]
    ) -> int:
        raise AssertionError("scheduler unit tests never mark outbox rows")


class _MemProcessedRepository(ProcessedMessageRepository):
    def __init__(self, state: MemState, pending: _PendingState) -> None:
        self._state = state
        self._pending = pending

    async def record(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        message_id: MessageId,
    ) -> bool:
        key = (stream_name.value, consumer_id.value, str(message_id))
        if key in self._state.processed or key in self._pending.processed:
            return False
        self._pending.processed.add(key)
        return True


class _MemSourceRepository:
    """Tiny sources view used by the service authorization gate."""

    def __init__(self, state: MemState) -> None:
        self._state = state

    async def get(self, source_id: SourceId) -> Source | None:
        return self._state.sources.get(source_id)


class MemUnitOfWork(UnitOfWork):
    """One fake transaction over :class:`MemState` with commit/rollback."""

    def __init__(self, state: MemState) -> None:
        self._state = state
        self._pending = _PendingState()
        self.commits = 0
        self.rollbacks = 0
        self._collection: MemCollectionRepository | None = None
        self._lock_held = False

    async def __aenter__(self) -> Self:
        # Serialize whole transactions (models PostgreSQL locking): pending
        # mutations become visible to other units only atomically on commit.
        await self._state.lock.acquire()
        self._lock_held = True
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
        self._state.policies.update(self._pending.policies)
        self._state.runs.update(self._pending.runs)
        self._state.occurrences.update(self._pending.occurrences)
        for stream_name, messages in self._pending.outbox.items():
            self._state.outbox.setdefault(stream_name, []).extend(messages)
        self._state.processed.update(self._pending.processed)
        self._pending = _PendingState()
        self.commits += 1
        await self._release_lock()

    async def rollback(self) -> None:
        self._pending = _PendingState()
        self.rollbacks += 1
        await self._release_lock()

    async def _release_lock(self) -> None:
        if self._lock_held:
            self._lock_held = False
            self._state.lock.release()

    @property
    def source_candidates(self) -> Any:
        raise AssertionError("collection fake exposes no candidate repository")

    @property
    def sources(self) -> Any:
        return _MemSourceRepository(self._state)

    @property
    def outbox(self) -> _MemOutboxRepository:
        return _MemOutboxRepository(self._state, self._pending)

    @property
    def processed_messages(self) -> _MemProcessedRepository:
        return _MemProcessedRepository(self._state, self._pending)

    @property
    def content(self) -> Any:
        raise AssertionError("collection fake exposes no content repository")

    @property
    def collection(self) -> MemCollectionRepository:
        if self._collection is None:
            self._collection = MemCollectionRepository(self._state, self._pending)
        return self._collection


class MemSpi(DarkulaSpi):
    """DarkulaSpi handing out :class:`MemUnitOfWork` instances."""

    def __init__(self, state: MemState | None = None) -> None:
        self.state = state or MemState()
        self.open_uows = 0

    def unit_of_work(self) -> UnitOfWork:
        return MemUnitOfWork(self.state)


def _add_seconds(value: datetime, seconds: int) -> datetime:
    """Return ``value`` shifted by ``seconds`` (UTC-preserving)."""
    from datetime import timedelta

    return value + timedelta(seconds=seconds)


def seed_source(state: MemState, source_id: SourceId) -> Source:
    """Seed one ACTIVE managed source into fake state."""
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    source = Source(
        source_id=source_id,
        status=SourceStatus.ACTIVE,
        created_at=moment,
        updated_at=moment,
    )
    state.sources[source_id] = source
    return source


def seed_endpoint(
    state: MemState,
    endpoint_id: SourceEndpointId,
    source_id: SourceId,
    uri: str,
) -> SourceEndpoint:
    """Seed one ACTIVE endpoint belonging to ``source_id``."""
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    endpoint = SourceEndpoint(
        endpoint_id=endpoint_id,
        source_id=source_id,
        uri=uri,
        endpoint_type=EndpointType.CLEARNET,
        status=EndpointStatus.ACTIVE,
        first_observed_at=moment,
        last_observed_at=moment,
    )
    state.endpoints[endpoint_id] = endpoint
    return endpoint


class FakeCrawler(Crawler):
    """Scripted deterministic crawler test double.

    Records every request; serves FIFO results or a default completed result;
    may raise a scripted exception. The behavior under test (collection
    orchestration) is real — only the sandbox/browser boundary is faked.
    """

    def __init__(
        self,
        results: list[CrawlResult] | None = None,
        *,
        failure: BaseException | None = None,
        probe: Callable[[], None] | None = None,
    ) -> None:
        self.results = list(results or [])
        self.failure = failure
        self.requests: list[CrawlRequest] = []
        self.probe = probe

    async def crawl(self, request: CrawlRequest) -> CrawlResult:
        if self.probe is not None:
            self.probe()
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        if self.results:
            return self.results.pop(0)
        return CrawlResult(
            request_id=request.request_id,
            status=CrawlStatus.COMPLETED,
            requests=1,
            duration_seconds=0.0,
        )

    def completed_with_pages(self, count: int) -> CrawlResult:
        from darkula.crawler.contracts import CrawlPageObservation

        pages = tuple(
            CrawlPageObservation(
                url=f"http://example.test/p/{index}",
                depth=1,
                http_status=200,
                rendered=True,
                title="title",
                text_excerpt="excerpt",
            )
            for index in range(count)
        )
        return CrawlResult(
            request_id=self.requests[-1].request_id if self.requests else "req",
            status=CrawlStatus.COMPLETED,
            pages=pages,
            requests=count,
            duration_seconds=0.0,
        )


class FakeIngest(ContentIngestService):
    """Scripted ContentIngestService double (PR 8 boundary faked).

    Records every observation; returns configurable ingest outcomes or
    raises a scripted failure. ``normalizer``/``spi`` are never constructed.
    """

    def __init__(
        self,
        *,
        created_observation: bool = True,
        deduplicated: bool = False,
        failure: BaseException | None = None,
        probe: Callable[[], None] | None = None,
    ) -> None:
        self._created_observation = created_observation
        self._deduplicated = deduplicated
        self._failure = failure
        self.observations: list[ContentObservation] = []
        self.probe = probe

    async def ingest(self, observation: ContentObservation) -> IngestResult:
        self.observations.append(observation)
        if self.probe is not None:
            self.probe()
        if self._failure is not None:
            raise self._failure
        from darkula.app.content import IngestResult

        return IngestResult(
            normalized_content=_dummy_normalized(observation),
            artifact=None,
            deduplicated=self._deduplicated,
            created_observation=self._created_observation,
        )


def _dummy_normalized(observation: ContentObservation) -> NormalizedContent:
    """Build a minimal NormalizedContent for a fake ingest result."""
    return NormalizedContent(
        content_id=NormalizedContentId.generate(),
        source_uri=observation.source_uri,
        observed_at=observation.observed_at,
        crawl_request_id=observation.crawl_request_id,
        observation_index=observation.observation_index,
        normalization_version="text-v1",
        completeness=ArtifactCompleteness.SAMPLE,
    )


__all__ = [
    "FakeCrawler",
    "FakeIngest",
    "MemCollectionRepository",
    "MemSpi",
    "MemState",
    "MemUnitOfWork",
    "seed_endpoint",
    "seed_source",
]
