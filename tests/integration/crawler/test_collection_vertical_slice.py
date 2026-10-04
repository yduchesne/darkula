# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 9 canonical vertical slice (section 17) with the real PR 7 crawler.

Full path: ACTIVE Source + BlackGate endpoint -> due ACTIVE
CollectionPolicy -> CollectionScheduler -> PostgreSQL QUEUED run + outbox
-> OutboxPublisher -> real Redpanda -> CollectionWorker ->
SourceCollectionService -> real CrawlerController -> real PodmanSandbox ->
disposable CrawlRuntime -> real Playwright/Chromium -> Fake World HTTP ->
CrawlResult -> ContentObservation -> real ContentIngestService ->
LocalFileObjectStore + PostgreSQL -> CollectionRun SUCCEEDED -> ack.

Also: post-success redelivery never recrawls (M3/F4), and the next schedule
interval creates a new run. No live illicit content; Fake World only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from darkula.app.artifacts import ArtifactStorageService
from darkula.app.collection import (
    COLLECTION_EXECUTE_MESSAGE_TYPE,
    CollectionExecuteCommand,
    SourceCollectionService,
)
from darkula.app.collection_scheduler import CollectionScheduler
from darkula.app.collection_worker import CollectionWorker
from darkula.app.content import ContentIngestService
from darkula.app.normalization import DeterministicContentNormalizer
from darkula.app.outbox import OutboxPublisher
from darkula.config.settings import CollectionSettings, DatabaseSettings
from darkula.crawler import CrawlerController
from darkula.domain.collection import (
    CollectionPolicy,
    CollectionRunStatus,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    ConsumerId,
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
from darkula.infrastructure.data_stream.redpanda import RedpandaDataStream
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from tests.integration.conftest import _raw_connect
from tests.integration.crawler.helpers import AUTH_PASSWORD, TRUTH_ONLY_TOKENS

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 2, 5, 12, 0, 0, tzinfo=UTC)
_FAKE_WORLD_URL = "http://darkula-fake-world-intg:8080"

_RESET_TABLES = (
    "collection_run",
    "collection_policy_endpoint",
    "collection_policy",
    "source_endpoint",
    "source",
    "message_outbox",
    "processed_message",
    "normalized_content",
    "content_artifact",
)


def _reset_tables(database_settings: DatabaseSettings) -> None:
    """Test-only cleanup of the tables this slice owns."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext('darkula_intg_collection'))")
            try:
                cur.execute(f"TRUNCATE {', '.join(_RESET_TABLES)} CASCADE")
            finally:
                cur.execute(
                    "SELECT pg_advisory_unlock(hashtext('darkula_intg_collection'))"
                )
        conn.commit()
    finally:
        conn.close()


def _topic() -> StreamName:
    return StreamName(f"intg-col-{uuid.uuid4().hex[:10]}")


def _consumer() -> ConsumerId:
    return ConsumerId(f"intg-col-wk-{uuid.uuid4().hex[:8]}")


async def _seed_blackgate_source(
    spi: PostgresDarkulaSpi,
) -> tuple[Source, SourceEndpoint, CollectionPolicy]:
    source = Source(
        source_id=SourceId.generate(),
        status=SourceStatus.ACTIVE,
        created_at=_T0,
        updated_at=_T0,
        name="blackgate-canonical",
    )
    endpoint = SourceEndpoint(
        endpoint_id=SourceEndpointId.generate(),
        source_id=source.source_id,
        uri=f"{_FAKE_WORLD_URL}/board/board-announcements",
        endpoint_type=EndpointType.CLEARNET,
        status=EndpointStatus.ACTIVE,
        first_observed_at=_T0,
        last_observed_at=_T0,
    )
    policy = CollectionPolicy(
        policy_id=CollectionPolicyId.generate(),
        source_id=source.source_id,
        active=True,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=300,
        next_due_at=_T0,
        allowed_endpoint_ids=(endpoint.endpoint_id,),
        max_pages=10,
        max_requests=120,
        max_depth=2,
        timeout_seconds=180.0,
    )
    async with spi.unit_of_work() as uow:
        await uow.sources.create(source)
        await uow.sources.add_endpoint(endpoint)
        await uow.collection.create_policy(policy)
        await uow.commit()
    return source, endpoint, policy


def _stream(redpanda_settings: Any) -> RedpandaDataStream:
    return RedpandaDataStream(
        bootstrap_servers=redpanda_settings.bootstrap_servers,
        client_id="darkula-intg-collection",
        poll_timeout_ms=2000,
        max_poll_records=20,
    )


async def _schedule_and_publish(
    spi: PostgresDarkulaSpi,
    stream: RedpandaDataStream,
    topic: StreamName,
    *,
    now: datetime,
) -> int:
    scheduler = CollectionScheduler(spi=spi, stream_name=topic)
    scheduled = await scheduler.schedule_due(now=now, limit=5)
    publisher = OutboxPublisher(spi=spi, data_stream=stream)
    published = await publisher.publish_pending(stream_name=topic, limit=5)
    assert scheduled == published
    return scheduled


def _leaks_in_stored_content(database_settings: DatabaseSettings) -> list[str]:
    """Scan persisted normalized content for truth-only tokens (test SQL)."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, text_preview, source_uri FROM normalized_content"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    leaks: list[str] = []
    haystack = "\n".join("\n".join(str(v or "") for v in row) for row in rows).lower()
    for token in (*TRUTH_ONLY_TOKENS, AUTH_PASSWORD):
        if token.lower() in haystack:
            leaks.append(token)
    return leaks


def _run_count(database_settings: DatabaseSettings, policy_id: Any) -> int:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM collection_run WHERE policy_id = %s",
                (str(policy_id),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _published_commands(database_settings: DatabaseSettings) -> list[StreamNameAndCmd]:
    """Read every outbox collection.execute command (test-only SQL)."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT message_id, occurred_at, payload FROM message_outbox "
                "WHERE message_type = %s ORDER BY created_at",
                (COLLECTION_EXECUTE_MESSAGE_TYPE,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    from darkula.app.data_stream import StreamMessage
    from darkula.domain.identifiers import MessageId

    result: list[StreamNameAndCmd] = []
    for row in rows or []:
        command = CollectionExecuteCommand.from_message(
            StreamMessage(
                message_id=MessageId.from_str(str(row[0])),
                message_type=COLLECTION_EXECUTE_MESSAGE_TYPE,
                schema_version=1,
                occurred_at=row[1].astimezone(UTC),
                payload=row[2],
            )
        )
        result.append(command)
    return result


#: Underscore alias keeps mypy happy for the tuple-of-commands list.
StreamNameAndCmd = CollectionExecuteCommand


class TestCanonicalCollectionVerticalSlice:
    """One managed source collected end-to-end through real infrastructure."""

    @pytest.mark.asyncio
    async def test_blackgate_collection_to_succeeded_and_ack(
        self,
        controller: CrawlerController,
        fake_world_url: str,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        redpanda_settings: Any,
        tmp_path: Path,
    ) -> None:
        assert fake_world_url == _FAKE_WORLD_URL
        _reset_tables(database_settings)
        source, _endpoint, policy = await _seed_blackgate_source(spi)
        topic = _topic()
        consumer_id = _consumer()
        stream = _stream(redpanda_settings)
        await stream.start()
        try:
            # 1) Scheduler admits the due occurrence and publishes.
            assert await _schedule_and_publish(
                spi, stream, topic, now=_T0 + timedelta(seconds=1)
            )
            # 2) The real worker executes the run with the real crawler,
            #    real PR 8 ingest, and real ObjectStore.
            store = LocalFileObjectStore(tmp_path / "objects")
            ingest = ContentIngestService(
                normalizer=DeterministicContentNormalizer(
                    artifact_service=ArtifactStorageService(object_store=store)
                ),
                spi=spi,
            )
            service = SourceCollectionService(
                spi=spi,
                crawler=controller,
                content_ingest=ingest,
                settings=CollectionSettings(),
            )
            worker = CollectionWorker(
                spi=spi,
                data_stream=stream,
                service=service,
                settings=CollectionSettings(),
                stream_name=topic,
                consumer_id=consumer_id,
            )
            assert await worker.process_once(max_records=5) == 1

            # 3) The run is durably SUCCEEDED with accurate counters.
            run_ids = _collect_run_ids(database_settings, policy.policy_id)
            assert len(run_ids) == 1
            runs = await _load_runs(spi, run_ids)
            assert len(runs) == 1
            run = runs[0]
            assert run.status is CollectionRunStatus.SUCCEEDED
            assert run.crawl_requests_attempted == 1
            assert run.pages_observed >= 1
            assert run.content_observations == run.pages_observed
            assert run.content_created >= 1
            assert run.policy_revision == 1

            # 4) PR 8 normalization was used: bounded excerpts persisted, and
            #    no truth-only/password leakage in stored content.
            observations = await _load_observations(
                spi, _observation_ids(database_settings)
            )
            assert observations
            assert all(
                obs.source_uri.startswith(_FAKE_WORLD_URL) for obs in observations
            )
            assert all(str(obs.completeness.value) == "SAMPLE" for obs in observations)
            assert _leaks_in_stored_content(database_settings) == []
            # The endpoint URI was used as the locator (never identity).
            assert run.policy_snapshot is not None
            assert str(source.source_id) == str(run.source_id)

            # 5) Post-success redelivery does not recrawl (M3/F4): republish
            #    the identical command; the worker acknowledges a no-op.
            commands = _published_commands(database_settings)
            assert commands
            first_command = commands[0]
            from darkula.app.data_stream import StreamMessage
            from darkula.domain.identifiers import MessageId

            # Republish the identical command with a fresh delivery identity:
            # redelivery must acknowledge a no-op without recrawling.
            await stream.publish(
                stream_name=topic,
                messages=[
                    StreamMessage(
                        message_id=MessageId.generate(),
                        message_type=COLLECTION_EXECUTE_MESSAGE_TYPE,
                        schema_version=1,
                        occurred_at=datetime.now(UTC),
                        payload={
                            "collection_run_id": str(first_command.run_id),
                            "source_id": str(first_command.source_id),
                            "policy_id": str(first_command.policy_id),
                        },
                    )
                ],
            )
            assert await worker.process_once(max_records=5) == 0
            runs_after = await _load_runs(
                spi, _collect_run_ids(database_settings, policy.policy_id)
            )
            assert len(runs_after) == 1
            assert runs_after[0].content_observations == run.content_observations

            # 6) The next interval creates a NEW run (new occurrence).
            interval = policy.interval_seconds
            assert await _schedule_and_publish(
                spi, stream, topic, now=_T0 + timedelta(seconds=interval + 1)
            )
            assert await worker.process_once(max_records=5) == 1
            assert _run_count(database_settings, policy.policy_id) == 2
        finally:
            await stream.stop()


def _collect_run_ids(database_settings: DatabaseSettings, policy_id: Any) -> list[str]:
    """Test-only SQL: every run id of a policy in creation order."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_id FROM collection_run WHERE policy_id = %s "
                "ORDER BY created_at",
                (str(policy_id),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in (rows or [])]


async def _load_runs(spi: PostgresDarkulaSpi, run_ids: list[str]) -> list[Any]:
    """Load runs through the real repository in deterministic order."""
    from darkula.domain.identifiers import CollectionRunId

    loaded: list[Any] = []
    for run_id in run_ids:
        async with spi.unit_of_work() as uow:
            run = await uow.collection.get_run(CollectionRunId.from_str(run_id))
        assert run is not None
        loaded.append(run)
    return loaded


def _observation_ids(database_settings: DatabaseSettings) -> list[str]:
    """Test-only SQL: every persisted normalized observation id."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT content_id FROM normalized_content ORDER BY content_id")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in (rows or [])]


async def _load_observations(
    spi: PostgresDarkulaSpi, content_ids: list[str]
) -> list[Any]:
    """Load persisted normalized observations through the repository."""
    from darkula.domain.identifiers import NormalizedContentId

    loaded: list[Any] = []
    for content_id in content_ids:
        async with spi.unit_of_work() as uow:
            obs = await uow.content.get_observation(
                NormalizedContentId.from_str(content_id)
            )
        assert obs is not None
        loaded.append(obs)
    return loaded
