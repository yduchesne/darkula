# SPDX-License-Identifier: AGPL-3.0-only
"""Managed-source collection application layer (PR 9).

Owns the deterministic, policy-authorized collection lifecycle that connects
the PR 7 crawler and the PR 8 content-ingestion boundary:

.. code-block:: text

    CollectionPolicy -> CollectionScheduler -> outbox -> DataStream
        -> CollectionWorker -> SourceCollectionService -> Crawler
        -> ContentIngestService

Dominant invariant (PR 9): PostgreSQL owns policy/run state, DataStream
carries small references only, and **no PostgreSQL transaction spans the
broker, crawler, sandbox, HTTP, or ObjectStore**:

.. code-block:: text

    short UoW: authorize/create/claim -> commit
    Crawler.crawl() outside any UoW
    ContentIngestService.ingest() outside the caller UoW
    short UoW: finalize -> commit

and the outbox record for a scheduled run is appended **inside** the same
transaction that creates the run (transactional outbox).

The collection-specific worker deliberately does not run long collection
work inside the PR 5 ``ReliableConsumer`` handler (PR 9 invariant 1.7):
``SourceCollectionService.execute`` claims the run, closes the transaction,
performs crawler/ingest work, and only then finalizes durable terminal
state.

Failure summaries are the bounded sanitized category vocabulary defined by
:mod:`darkula.domain.collection`; raw exception text, URIs, content, and
credentials are never persisted.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from darkula.app.content import ContentIngestService
from darkula.app.data_stream import StreamMessage
from darkula.app.normalization import NormalizationError
from darkula.app.persistence import (
    ConflictError,
    DarkulaSpi,
    PersistenceError,
    UnitOfWork,
)
from darkula.config.settings import CollectionSettings
from darkula.crawler.contracts import (
    AllowedOrigin,
    Crawler,
    CrawlRequest,
    CrawlStatus,
    InvalidCrawlRequest,
)
from darkula.crawler.mapping import page_observation_to_content
from darkula.domain.collection import (
    TERMINAL_RUN_STATUSES,
    CollectionFailureCode,
    CollectionRun,
    CollectionRunStatus,
    validate_counter_deltas,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    ConsumerId,
    MessageId,
    SourceEndpointId,
    SourceId,
    StreamName,
)
from darkula.domain.source import SourceEndpoint, SourceStatus
from darkula.telemetry.metrics import get_counter, get_histogram
from darkula.telemetry.tracing import get_tracer

#: Logical stream carrying collection work references (never content/URIs).
COLLECTION_WORK_STREAM = StreamName("collection-work")

#: Durable consumer identity of the collection worker.
COLLECTION_WORKER_CONSUMER = ConsumerId("collection-worker")

#: Wire type/schema of the v1 collection.execute command.
COLLECTION_EXECUTE_MESSAGE_TYPE = "collection.execute"
COLLECTION_EXECUTE_SCHEMA_VERSION = 1

#: Metric names (bounded; no run/source/policy IDs, no URIs, no content).
RUN_CREATED = "darkula.collection.run.created"
RUN_STARTED = "darkula.collection.run.started"
RUN_SUCCEEDED = "darkula.collection.run.succeeded"
RUN_FAILED = "darkula.collection.run.failed"
RUN_CANCELLED = "darkula.collection.run.cancelled"
RUN_RECLAIMED = "darkula.collection.run.reclaimed"
RUN_DURATION = "darkula.collection.run.duration"
PAGES_OBSERVED = "darkula.collection.pages"
OBSERVATIONS = "darkula.collection.observations"
CONTENT_CREATED = "darkula.collection.content.created"
CONTENT_DEDUPLICATED = "darkula.collection.content.deduplicated"
INVALID_COMMAND = "darkula.collection.worker.command.invalid"
UNKNOWN_RUN = "darkula.collection.worker.run.unknown"
WORKER_PROCESS_COUNT = "darkula.collection.worker.process.count"

#: Span names for the collection lifecycle (bounded attributes only).
SCHEDULE_SPAN = "collection.schedule_due"
EXECUTE_SPAN = "collection.execute"
CRAWL_SPAN = "collection.crawl"
INGEST_SPAN = "collection.ingest"
FINALIZE_SPAN = "collection.finalize"

_OUTCOME_ATTR = "darkula.outcome"
_SUCCESS_OUTCOME = "success"
_ERROR_OUTCOME = "error"


class CollectionError(RuntimeError):
    """Base class for bounded collection failures (never raw details)."""


class CollectionNotFoundError(CollectionError):
    """The referenced run/policy does not exist.

    The worker treats this as a bounded poison outcome: no run is invented
    and the message is acknowledged so the lane cannot wedge forever.
    """


class CollectionLeaseConflictError(CollectionError):
    """The run is RUNNING under another worker's unexpired lease.

    The caller must not crawl and must not acknowledge: redelivery will
    re-inspect the run once the lease expires or the run becomes terminal.
    """


class CollectionNotExecutableError(CollectionError):
    """The run cannot be executed in its current state.

    Raised only when admission produced neither a plan nor a terminal
    result (an impossible state); callers re-read the persisted run.
    """


class CollectionExecutionError(CollectionError):
    """An ordinary collection execution failure.

    ``failure_code``/``failure_summary`` are bounded sanitized categories;
    the raw exception, URIs, and content are never persisted.
    """

    def __init__(
        self,
        *,
        failure_code: CollectionFailureCode,
        failure_summary: str,
    ) -> None:
        super().__init__(failure_summary)
        self.failure_code = failure_code
        self.failure_summary = failure_summary


class CollectionPolicyError(CollectionError):
    """The run's frozen policy snapshot is invalid or unbounded."""


class CollectionWorkError(CollectionError):
    """A work message is malformed or carries a disallowed payload."""


@dataclass(frozen=True, slots=True)
class CollectionExecuteCommand:
    """The small IDs-only v1 ``collection.execute`` command.

    Carries exactly the three domain identities the worker needs to reload
    authoritative state; never a URI, policy blob, content, or credential.
    """

    run_id: CollectionRunId
    source_id: SourceId
    policy_id: CollectionPolicyId

    @classmethod
    def from_message(cls, message: StreamMessage) -> CollectionExecuteCommand:
        """Strictly decode one v1 command; malformed input raises
        :class:`CollectionWorkError`."""
        if message.message_type != COLLECTION_EXECUTE_MESSAGE_TYPE:
            raise CollectionWorkError("unexpected collection message type")
        if message.schema_version != COLLECTION_EXECUTE_SCHEMA_VERSION:
            raise CollectionWorkError("unsupported collection command version")
        payload = message.payload
        if set(payload) != {
            "collection_run_id",
            "source_id",
            "policy_id",
        }:
            raise CollectionWorkError("unexpected collection command payload keys")
        try:
            return cls(
                run_id=CollectionRunId.from_str(str(payload["collection_run_id"])),
                source_id=SourceId.from_str(str(payload["source_id"])),
                policy_id=CollectionPolicyId.from_str(str(payload["policy_id"])),
            )
        except (TypeError, ValueError) as exc:
            raise CollectionWorkError(
                "collection command carried a non-identity value"
            ) from exc

    def to_message(self, *, occurred_at: datetime) -> StreamMessage:
        """Encode the command as a small IDs-only stream message."""
        return StreamMessage(
            message_id=MessageId.generate(),
            message_type=COLLECTION_EXECUTE_MESSAGE_TYPE,
            schema_version=COLLECTION_EXECUTE_SCHEMA_VERSION,
            occurred_at=occurred_at,
            payload={
                "collection_run_id": str(self.run_id),
                "source_id": str(self.source_id),
                "policy_id": str(self.policy_id),
            },
            routing_key=str(self.source_id),
        )


def deterministic_request_id(
    *,
    run_id: CollectionRunId,
    endpoint_id: SourceEndpointId,
    attempt: int,
) -> str:
    """Return the stable deterministic CrawlRequest identity for one attempt.

    Uses a namespace UUID seeded from the run/endpoint/attempt identities:
    MAP6 (same run/endpoint/attempt -> stable id) and MAP7 (next attempt ->
    distinct deterministic id). Never Python ``hash()``.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    name = f"{run_id}:{endpoint_id}:{attempt}"
    return str(uuid.uuid5(uuid.NAMESPACE_OID, name))


@dataclass(frozen=True, slots=True)
class PolicyExecution:
    """Bounded execution view derived from a run's frozen policy snapshot.

    All fields mirror the PR 7 ``CrawlRequest`` budgets; the PR 7 controller
    remains the enforcement boundary (MAP5).
    """

    interval_seconds: int
    allowed_endpoint_ids: tuple[SourceEndpointId, ...]
    allowed_paths: tuple[str, ...]
    max_pages: int
    max_requests: int
    max_depth: int
    timeout_seconds: float
    authentication_reference: str | None


def policy_execution_from_snapshot(
    snapshot: Mapping[str, object] | None,
) -> PolicyExecution:
    """Strictly decode one frozen execution snapshot into a bounded view.

    Unknown keys and out-of-shape values raise :class:`CollectionPolicyError`
    (the run's snapshot is authoritative and must stay explainable).
    """
    if snapshot is None:
        raise CollectionPolicyError("the run has no policy snapshot")
    expected = {
        "policy_revision",
        "interval_seconds",
        "allowed_endpoint_ids",
        "allowed_paths",
        "max_pages",
        "max_requests",
        "max_depth",
        "timeout_seconds",
        "authentication_reference",
    }
    if set(snapshot) != expected:
        raise CollectionPolicyError("the policy snapshot is not the v1 shape")
    try:
        endpoint_ids: tuple[SourceEndpointId, ...] = tuple(
            SourceEndpointId.from_str(str(item))
            for item in _string_list(snapshot["allowed_endpoint_ids"])
        )
        allowed_paths: tuple[str, ...] = _string_list(snapshot["allowed_paths"])
        return PolicyExecution(
            interval_seconds=_positive_int(snapshot["interval_seconds"]),
            allowed_endpoint_ids=endpoint_ids,
            allowed_paths=allowed_paths,
            max_pages=_positive_int(snapshot["max_pages"]),
            max_requests=_positive_int(snapshot["max_requests"]),
            max_depth=_positive_int(snapshot["max_depth"]),
            timeout_seconds=_positive_number(snapshot["timeout_seconds"]),
            authentication_reference=(
                None
                if snapshot["authentication_reference"] is None
                else str(snapshot["authentication_reference"])
            ),
        )
    except (TypeError, ValueError) as exc:
        raise CollectionPolicyError("the policy snapshot is not the v1 shape") from exc


def _string_list(value: object) -> tuple[str, ...]:
    """Boundedly decode a JSON string array snapshot value."""
    if not isinstance(value, list):
        raise TypeError("snapshot list value is not a list")
    return tuple(str(item) for item in value)


def _positive_int(value: object) -> int:
    """Boundedly decode a positive integer snapshot value."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("snapshot integer value is invalid")
    if value < 1:
        raise ValueError("snapshot integer value is not positive")
    return value


def _positive_number(value: object) -> float:
    """Boundedly decode a positive number snapshot value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("snapshot number value is invalid")
    if value <= 0:
        raise ValueError("snapshot number value is not positive")
    return float(value)


@dataclass(frozen=True, slots=True)
class CollectionExecutionResult:
    """Outcome of one :meth:`SourceCollectionService.execute` call.

    ``run_status`` is always terminal: either the run was already terminal
    (no crawl) or execution progressed to durable finalization.
    """

    run_id: CollectionRunId
    run_status: CollectionRunStatus
    crawl_requests_attempted: int
    pages_observed: int
    content_observations: int
    content_created: int
    content_deduplicated: int
    failure_code: CollectionFailureCode | None
    failure_summary: str | None


@dataclass(frozen=True, slots=True)
class _ExecutionPlan:
    """Frozen in-flight contract produced by the admission UoW."""

    run_id: CollectionRunId
    execution_id: str
    attempt_number: int
    execution: PolicyExecution
    endpoints: tuple[SourceEndpoint, ...]
    started_at: datetime


@dataclass(frozen=True, slots=True)
class _Counters:
    """Accumulated execution counters (monotonic within one attempt)."""

    crawl_requests_attempted: int = 0
    pages_observed: int = 0
    content_observations: int = 0
    content_created: int = 0
    content_deduplicated: int = 0


def _result_from_run(run: CollectionRun) -> CollectionExecutionResult:
    """Project one persisted run onto the app-level result DTO."""
    return CollectionExecutionResult(
        run_id=run.run_id,
        run_status=run.status,
        crawl_requests_attempted=run.crawl_requests_attempted,
        pages_observed=run.pages_observed,
        content_observations=run.content_observations,
        content_created=run.content_created,
        content_deduplicated=run.content_deduplicated,
        failure_code=run.failure_code,
        failure_summary=run.failure_summary,
    )


def _cancelled_result(
    run: CollectionRun, *, failure_code: CollectionFailureCode, now: datetime
) -> CollectionExecutionResult:
    """Project an authorization-withdrawn run onto a CANCELLED result."""
    return CollectionExecutionResult(
        run_id=run.run_id,
        run_status=CollectionRunStatus.CANCELLED,
        crawl_requests_attempted=run.crawl_requests_attempted,
        pages_observed=run.pages_observed,
        content_observations=run.content_observations,
        content_created=run.content_created,
        content_deduplicated=run.content_deduplicated,
        failure_code=failure_code,
        failure_summary=("source or policy authorization withdrawn before execution"),
    )


class SourceCollectionService:
    """Deterministic, policy-authorized run executor over the PR 7 crawler
    and PR 8 content ingestion.

    Holds no PostgreSQL transaction across crawler/HTTP/ObjectStore I/O and
    never builds the crawler sandbox itself (the composed
    :class:`~darkula.crawler.contracts.Crawler` owns the sandbox boundary).
    """

    def __init__(
        self,
        *,
        spi: DarkulaSpi,
        crawler: Crawler,
        content_ingest: ContentIngestService,
        settings: CollectionSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._spi = spi
        self._crawler = crawler
        self._content_ingest = content_ingest
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- public API -------------------------------------------------------

    async def execute(self, run_id: CollectionRunId) -> CollectionExecutionResult:
        """Execute one run to its durable terminal state.

        :raises CollectionNotFoundError: the run does not exist (poison).
        :raises CollectionLeaseConflictError: another worker holds an
            unexpired lease; never crawl concurrently.
        :raises asyncio.CancelledError: cancellation is persisted best-effort
            (``CANCELLED``) and always re-raised.
        """
        start_ns = time.perf_counter_ns()
        tracer = get_tracer()
        with tracer.start_as_current_span(EXECUTE_SPAN):
            result = await self._execute(run_id)
        get_histogram(RUN_DURATION).record((time.perf_counter_ns() - start_ns) / 1e9)
        return result

    async def _execute(self, run_id: CollectionRunId) -> CollectionExecutionResult:
        now = self._clock()

        # -- short admission UoW: authorize/claim, then commit ------------
        async with self._spi.unit_of_work() as uow:
            run = await uow.collection.get_run(run_id)
            if run is None:
                raise CollectionNotFoundError(
                    "the collection run does not exist; no run is invented"
                )
            if run.status in TERMINAL_RUN_STATUSES:
                # Historical terminal run: no crawl (WK2/WK9).
                await uow.commit()
                return _result_from_run(run)
            if run.status is CollectionRunStatus.RUNNING:
                admission = await self._admit_running(uow, run, now)
            else:
                admission = await self._admit_queued(uow, run, now)
            if admission.plan is not None:
                get_counter(RUN_STARTED).add(1, {_OUTCOME_ATTR: _SUCCESS_OUTCOME})
            await uow.commit()

        if admission.plan is None:
            # Authorization withdrawn before start, terminal race, or
            # exhausted attempts: a durable terminal result was produced (or
            # the caller should re-read the run). Never crawl.
            if admission.result is None:
                raise CollectionNotExecutableError(
                    "the run is not executable (no crawl); "
                    "re-read the persisted run state"
                )
            return admission.result

        plan = admission.plan

        # -- external work strictly outside any open UoW ------------------
        counters = _Counters()
        try:
            counters = await self._run_crawl_and_ingest(plan)
        except asyncio.CancelledError:
            await self._best_effort_cancel(run_id)
            raise
        except CollectionExecutionError as exc:
            return await self._finalize(
                run_id,
                new_status=CollectionRunStatus.FAILED,
                counters=counters,
                failure_code=exc.failure_code,
                failure_summary=exc.failure_summary,
            )
        return await self._finalize(
            run_id,
            new_status=CollectionRunStatus.SUCCEEDED,
            counters=counters,
        )

    # -- admission --------------------------------------------------------

    async def _admit_queued(
        self, uow: UnitOfWork, run: CollectionRun, now: datetime
    ) -> _Admission:
        """Gate a QUEUED run: current Source/policy must be ACTIVE and the
        frozen endpoints must be owned and ACTIVE; then claim RUNNING."""
        if not await self._authorization_active(uow, run):
            await uow.collection.cancel_run(run_id=run.run_id, completed_at=now)
            get_counter(RUN_CANCELLED).add(1, {_OUTCOME_ATTR: _ERROR_OUTCOME})
            code = await self._withdrawn_code(uow, run)
            return _Admission(
                plan=None,
                result=_cancelled_result(run, failure_code=code, now=now),
            )
        endpoints = await self._resolve_snapshot_endpoints(uow, run)
        if endpoints is None:
            await uow.collection.cancel_run(run_id=run.run_id, completed_at=now)
            get_counter(RUN_CANCELLED).add(1, {_OUTCOME_ATTR: _ERROR_OUTCOME})
            return _Admission(
                plan=None,
                result=_cancelled_result(
                    run,
                    failure_code=CollectionFailureCode.NO_ACTIVE_ENDPOINT,
                    now=now,
                ),
            )
        execution_id = str(uuid.uuid4())
        outcome = await uow.collection.claim_run(
            run_id=run.run_id,
            execution_id=execution_id,
            started_at=now,
            lease_expires_at=_lease_delta(now, self._lease_seconds()),
        )
        if outcome.value == "unknown_run":
            raise CollectionNotFoundError("the collection run disappeared")
        if outcome.value == "not_queued":
            fresh = await uow.collection.get_run(run.run_id)
            if fresh is not None and fresh.status in TERMINAL_RUN_STATUSES:
                return _Admission(plan=None, result=_result_from_run(fresh))
            raise CollectionLeaseConflictError(
                "the run is already claimed by another worker"
            )
        return _Admission(
            plan=_ExecutionPlan(
                run_id=run.run_id,
                execution_id=execution_id,
                attempt_number=run.attempt_count + 1,
                execution=self._execution_from_run(run),
                endpoints=endpoints,
                started_at=now,
            ),
            result=None,
        )

    async def _admit_running(
        self, uow: UnitOfWork, run: CollectionRun, now: datetime
    ) -> _Admission:
        """Recover a RUNNING run: unexpired lease - conflict; expired lease
        - reclaim the same run; attempts exhausted - durable FAILED."""
        if run.lease_expires_at is not None and run.lease_expires_at > now:
            raise CollectionLeaseConflictError(
                "the run is executing under another worker's unexpired lease"
            )
        execution_id = str(uuid.uuid4())
        outcome = await uow.collection.reclaim_run(
            run_id=run.run_id,
            execution_id=execution_id,
            now=now,
            lease_expires_at=_lease_delta(now, self._lease_seconds()),
            max_attempts=self._settings.max_run_attempts,
        )
        if outcome.value == "unknown_run":
            raise CollectionNotFoundError("the collection run disappeared")
        if outcome.value == "lease_active":
            raise CollectionLeaseConflictError(
                "the run is executing under another worker's unexpired lease"
            )
        if outcome.value == "terminal":
            fresh = await uow.collection.get_run(run.run_id)
            if fresh is not None and fresh.status in TERMINAL_RUN_STATUSES:
                return _Admission(plan=None, result=_result_from_run(fresh))
            raise CollectionLeaseConflictError(
                "the run transitioned unexpectedly during reclaim"
            )
        if outcome.value == "attempts_exhausted":
            await uow.collection.complete_run(
                run_id=run.run_id,
                new_status=CollectionRunStatus.FAILED,
                completed_at=now,
                crawl_requests_attempted=run.crawl_requests_attempted,
                pages_observed=run.pages_observed,
                content_observations=run.content_observations,
                content_created=run.content_created,
                content_deduplicated=run.content_deduplicated,
                failure_code=CollectionFailureCode.ATTEMPTS_EXHAUSTED,
                failure_summary="maximum execution attempts reached",
            )
            get_counter(RUN_FAILED).add(1, {_OUTCOME_ATTR: _ERROR_OUTCOME})
            return _Admission(
                plan=None,
                result=_result_from_run(
                    replace(
                        run,
                        status=CollectionRunStatus.FAILED,
                        completed_at=now,
                        failure_code=CollectionFailureCode.ATTEMPTS_EXHAUSTED,
                        failure_summary="maximum execution attempts reached",
                    )
                ),
            )
        get_counter(RUN_RECLAIMED).add(1)
        endpoints = await self._resolve_snapshot_endpoints(uow, run)
        if endpoints is None:
            await uow.collection.cancel_run(run_id=run.run_id, completed_at=now)
            get_counter(RUN_CANCELLED).add(1, {_OUTCOME_ATTR: _ERROR_OUTCOME})
            return _Admission(
                plan=None,
                result=_cancelled_result(
                    run,
                    failure_code=CollectionFailureCode.NO_ACTIVE_ENDPOINT,
                    now=now,
                ),
            )
        return _Admission(
            plan=_ExecutionPlan(
                run_id=run.run_id,
                execution_id=execution_id,
                attempt_number=run.attempt_count + 1,
                execution=self._execution_from_run(run),
                endpoints=endpoints,
                started_at=now,
            ),
            result=None,
        )

    async def _authorization_active(self, uow: UnitOfWork, run: CollectionRun) -> bool:
        policy = await uow.collection.get_policy(run.policy_id)
        if policy is None or not policy.active:
            return False
        source = await uow.sources.get(run.source_id)
        return source is not None and source.status is SourceStatus.ACTIVE

    async def _withdrawn_code(
        self, uow: UnitOfWork, run: CollectionRun
    ) -> CollectionFailureCode:
        policy = await uow.collection.get_policy(run.policy_id)
        source = await uow.sources.get(run.source_id)
        if source is None or source.status is not SourceStatus.ACTIVE:
            return CollectionFailureCode.SOURCE_INACTIVE
        if policy is None or not policy.active:
            return CollectionFailureCode.POLICY_INACTIVE
        return CollectionFailureCode.SOURCE_INACTIVE

    async def _resolve_snapshot_endpoints(
        self, uow: UnitOfWork, run: CollectionRun
    ) -> tuple[SourceEndpoint, ...] | None:
        """Resolve the run's frozen endpoint identities against current
        endpoint state; ``None`` when any is missing, foreign, or inactive
        (no crawl - MAP2/MAP3 deterministic rule)."""
        try:
            execution = policy_execution_from_snapshot(run.policy_snapshot)
        except CollectionPolicyError:
            return None
        resolved: list[SourceEndpoint] = []
        for endpoint_id in execution.allowed_endpoint_ids:
            endpoint = await uow.collection.get_endpoint(endpoint_id)
            if (
                endpoint is None
                or endpoint.source_id != run.source_id
                or endpoint.status.value != "ACTIVE"
            ):
                return None
            resolved.append(endpoint)
        return tuple(sorted(resolved, key=lambda endpoint: str(endpoint.endpoint_id)))

    def _execution_from_run(self, run: CollectionRun) -> PolicyExecution:
        try:
            return policy_execution_from_snapshot(run.policy_snapshot)
        except CollectionPolicyError as exc:
            raise CollectionExecutionError(
                failure_code=CollectionFailureCode.INVALID_STATE,
                failure_summary="the run's frozen policy snapshot is invalid",
            ) from exc

    # -- execution --------------------------------------------------------

    async def _run_crawl_and_ingest(self, plan: _ExecutionPlan) -> _Counters:
        counters = _Counters()
        tracer = get_tracer()
        for endpoint in plan.endpoints:
            request_id = deterministic_request_id(
                run_id=plan.run_id,
                endpoint_id=endpoint.endpoint_id,
                attempt=plan.attempt_number,
            )
            try:
                origin = AllowedOrigin.from_url(endpoint.uri)
                request = CrawlRequest(
                    request_id=request_id,
                    start_url=endpoint.uri,
                    allowed_origin=origin,
                    credentials=None,
                    max_pages=plan.execution.max_pages,
                    max_requests=plan.execution.max_requests,
                    max_depth=plan.execution.max_depth,
                    timeout_seconds=plan.execution.timeout_seconds,
                )
            except InvalidCrawlRequest as exc:
                raise CollectionExecutionError(
                    failure_code=CollectionFailureCode.CRAWL_FAILED,
                    failure_summary="an authorized endpoint is not crawlable",
                ) from exc
            counters = _Counters(
                crawl_requests_attempted=counters.crawl_requests_attempted + 1,
                pages_observed=counters.pages_observed,
                content_observations=counters.content_observations,
                content_created=counters.content_created,
                content_deduplicated=counters.content_deduplicated,
            )
            with tracer.start_as_current_span(CRAWL_SPAN):
                result = await self._crawler.crawl(request)
            if result.status is not CrawlStatus.COMPLETED:
                raise CollectionExecutionError(
                    failure_code=CollectionFailureCode.CRAWL_FAILED,
                    failure_summary=f"crawl ended in state {result.status.value}",
                )
            counters = _Counters(
                crawl_requests_attempted=counters.crawl_requests_attempted,
                pages_observed=counters.pages_observed + len(result.pages),
                content_observations=counters.content_observations,
                content_created=counters.content_created,
                content_deduplicated=counters.content_deduplicated,
            )
            get_counter(PAGES_OBSERVED).add(len(result.pages))
            for index, page in enumerate(result.pages, start=1):
                observation = page_observation_to_content(
                    page,
                    crawl_request_id=result.request_id,
                    observed_at=self._clock(),
                    observation_index=index,
                )
                with tracer.start_as_current_span(INGEST_SPAN):
                    try:
                        ingest_result = await self._content_ingest.ingest(observation)
                    except NormalizationError as exc:
                        raise CollectionExecutionError(
                            failure_code=CollectionFailureCode.NORMALIZATION_FAILED,
                            failure_summary="content normalization failed",
                        ) from exc
                    except PersistenceError as exc:
                        raise CollectionExecutionError(
                            failure_code=CollectionFailureCode.CONTENT_PERSISTENCE_FAILED,
                            failure_summary="content persistence failed",
                        ) from exc
                counters = _Counters(
                    crawl_requests_attempted=counters.crawl_requests_attempted,
                    pages_observed=counters.pages_observed,
                    content_observations=counters.content_observations + 1,
                    content_created=counters.content_created
                    + (1 if ingest_result.created_observation else 0),
                    content_deduplicated=counters.content_deduplicated
                    + (1 if ingest_result.deduplicated else 0),
                )
        get_counter(OBSERVATIONS).add(counters.content_observations)
        get_counter(CONTENT_CREATED).add(counters.content_created)
        get_counter(CONTENT_DEDUPLICATED).add(counters.content_deduplicated)
        return counters

    # -- finalization -----------------------------------------------------

    async def _finalize(
        self,
        run_id: CollectionRunId,
        *,
        new_status: CollectionRunStatus,
        counters: _Counters,
        failure_code: CollectionFailureCode | None = None,
        failure_summary: str | None = None,
    ) -> CollectionExecutionResult:
        """Short UoW: expected RUNNING -> terminal, then commit."""
        validate_counter_deltas(
            crawl_requests_attempted=counters.crawl_requests_attempted,
            pages_observed=counters.pages_observed,
            content_observations=counters.content_observations,
            content_created=counters.content_created,
            content_deduplicated=counters.content_deduplicated,
        )
        completed_at = self._clock()
        if new_status is CollectionRunStatus.FAILED:
            failure_summary = validate_failure_summary_or_default(failure_summary)
        tracer = get_tracer()
        with tracer.start_as_current_span(FINALIZE_SPAN):
            async with self._spi.unit_of_work() as uow:
                try:
                    await uow.collection.complete_run(
                        run_id=run_id,
                        new_status=new_status,
                        completed_at=completed_at,
                        crawl_requests_attempted=counters.crawl_requests_attempted,
                        pages_observed=counters.pages_observed,
                        content_observations=counters.content_observations,
                        content_created=counters.content_created,
                        content_deduplicated=counters.content_deduplicated,
                        failure_code=failure_code,
                        failure_summary=failure_summary,
                    )
                except ConflictError:
                    # A concurrent actor finalized first; reload its state.
                    fresh = await uow.collection.get_run(run_id)
                    await uow.commit()
                    if fresh is None:
                        raise CollectionNotFoundError(
                            "the collection run disappeared during finalization"
                        ) from None
                    return _result_from_run(fresh)
                await uow.commit()
        if new_status is CollectionRunStatus.SUCCEEDED:
            get_counter(RUN_SUCCEEDED).add(1, {_OUTCOME_ATTR: _SUCCESS_OUTCOME})
        else:
            get_counter(RUN_FAILED).add(1, {_OUTCOME_ATTR: _ERROR_OUTCOME})
        return CollectionExecutionResult(
            run_id=run_id,
            run_status=new_status,
            crawl_requests_attempted=counters.crawl_requests_attempted,
            pages_observed=counters.pages_observed,
            content_observations=counters.content_observations,
            content_created=counters.content_created,
            content_deduplicated=counters.content_deduplicated,
            failure_code=failure_code,
            failure_summary=failure_summary,
        )

    async def _best_effort_cancel(self, run_id: CollectionRunId) -> None:
        """Best-effort RUNNING -> CANCELLED in a short UoW; never masks the
        original cancellation."""
        try:
            async with self._spi.unit_of_work() as uow:
                await uow.collection.cancel_run(
                    run_id=run_id, completed_at=self._clock()
                )
                await uow.commit()
            get_counter(RUN_CANCELLED).add(1, {_OUTCOME_ATTR: _ERROR_OUTCOME})
        except asyncio.CancelledError:
            raise
        except PersistenceError:
            # Best effort only: the worker does not acknowledge without a
            # durable terminal state, so a later redelivery re-inspects.
            return

    def _lease_seconds(self) -> float:
        return self._settings.execution_lease_seconds


@dataclass(frozen=True, slots=True)
class _Admission:
    """One admission decision: either an execution plan or a terminal result.

    A ``plan`` means external crawler/ingest work is authorized. ``result``
    is produced when the run reached a durable terminal state during
    admission (authorization withdrawn, exhausted attempts, terminal race).
    Exactly one of the two is set.
    """

    plan: _ExecutionPlan | None
    result: CollectionExecutionResult | None


def _lease_delta(now: datetime, seconds: float) -> datetime:
    """Return the lease-expiry instant for a positive lease duration."""
    from datetime import timedelta

    return now + timedelta(seconds=seconds)


def validate_failure_summary_or_default(value: str | None) -> str | None:
    """Bound a finalization failure summary; defaults when blank."""
    from darkula.domain.collection import validate_failure_summary

    if value is None:
        return value
    try:
        return validate_failure_summary(value)
    except ValueError:
        return "collection execution failed"


__all__ = [
    "COLLECTION_EXECUTE_MESSAGE_TYPE",
    "COLLECTION_EXECUTE_SCHEMA_VERSION",
    "COLLECTION_WORKER_CONSUMER",
    "COLLECTION_WORK_STREAM",
    "CONTENT_CREATED",
    "CONTENT_DEDUPLICATED",
    "CRAWL_SPAN",
    "EXECUTE_SPAN",
    "FINALIZE_SPAN",
    "INGEST_SPAN",
    "INVALID_COMMAND",
    "OBSERVATIONS",
    "PAGES_OBSERVED",
    "RUN_CANCELLED",
    "RUN_CREATED",
    "RUN_DURATION",
    "RUN_FAILED",
    "RUN_RECLAIMED",
    "RUN_STARTED",
    "RUN_SUCCEEDED",
    "SCHEDULE_SPAN",
    "UNKNOWN_RUN",
    "WORKER_PROCESS_COUNT",
    "CollectionError",
    "CollectionExecuteCommand",
    "CollectionExecutionError",
    "CollectionExecutionResult",
    "CollectionLeaseConflictError",
    "CollectionNotFoundError",
    "CollectionPolicyError",
    "CollectionWorkError",
    "PolicyExecution",
    "SourceCollectionService",
    "deterministic_request_id",
    "policy_execution_from_snapshot",
]
