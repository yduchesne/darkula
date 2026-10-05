# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 16 canonical v0.1 full-stack slice (E2E16-1..E2E16-4).

Combines the asynchronous collection path (real PostgreSQL scheduler +
transactional outbox -> real Redpanda -> real CollectionWorker ->
CrawlerController -> PodmanSandbox -> Chromium -> Fake World HTTP ->
normalization -> local ObjectStore -> PostgreSQL) with downstream extraction
stages invoked explicitly through their existing production services. No new
orchestrator is introduced.

Only the external world (Fake World) and the non-deterministic model/resolver
boundaries (FakeLlmClient / FakeGeographicResolver) are faked.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from darkula.app.artifacts import ArtifactStorageService
from darkula.app.collection import SourceCollectionService
from darkula.app.collection_scheduler import CollectionScheduler
from darkula.app.collection_worker import CollectionWorker
from darkula.app.content import ContentIngestService
from darkula.app.extraction import (
    DeterministicExtractionService,
    deterministic_observables_profile,
)
from darkula.app.normalization import DeterministicContentNormalizer
from darkula.app.outbox import OutboxPublisher
from darkula.app.semantic_extraction import (
    SemanticExtractionResponse,
    SemanticExtractionService,
    SemanticMentionCandidate,
)
from darkula.config.settings import (
    CollectionSettings,
    DatabaseSettings,
    SemanticExtractionSettings,
)
from darkula.crawler import CrawlerController
from darkula.domain.collection import CollectionPolicy
from darkula.domain.extraction import EntityType
from darkula.domain.identifiers import (
    CollectionPolicyId,
    ConsumerId,
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
from darkula.infrastructure.data_stream.redpanda import RedpandaDataStream
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_llm import FakeLlmCall, FakeLlmClient
from tests.integration.conftest import _raw_connect
from tests.integration.crawler.helpers import AUTH_PASSWORD, TRUTH_ONLY_TOKENS

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_BLACKGATE_URL = "http://darkula-fake-world-intg:8080"
#: Cross-source virtual host served by the same Fake World container.
_ACCESSBAY_URL = "http://accessbay.example.test:8080"

_RESET = (
    "source_assessment, extracted_relationship, relationship_extraction_result, "
    "geographic_resolution, extracted_entity, extraction_result, "
    "normalized_content, content_artifact, collection_run, "
    "collection_policy_endpoint, collection_policy, source_endpoint, source, "
    "message_outbox, processed_message"
)


def _reset(database_settings: DatabaseSettings) -> None:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {_RESET} CASCADE")
        conn.commit()
    finally:
        conn.close()


def _topic() -> StreamName:
    return StreamName(f"intg-v01-{uuid.uuid4().hex[:10]}")


def _content_ids(database_settings: DatabaseSettings) -> list[str]:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT content_id FROM normalized_content ORDER BY content_id")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in (rows or [])]


def _content_uris(database_settings: DatabaseSettings) -> list[str]:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source_uri FROM normalized_content ORDER BY source_uri")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in (rows or [])]


def _stored_content_leaks(database_settings: DatabaseSettings) -> list[str]:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, text_preview, source_uri FROM normalized_content"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    haystack = "\n".join(
        "\n".join(str(value or "") for value in row) for row in rows
    ).lower()
    return [
        token
        for token in (*TRUTH_ONLY_TOKENS, AUTH_PASSWORD)
        if token.lower() in haystack
    ]


async def _seed_source(
    spi: PostgresDarkulaSpi, *, base_url: str, start_path: str, name: str
) -> None:
    source = Source(
        source_id=SourceId.generate(),
        status=SourceStatus.ACTIVE,
        created_at=_T0,
        updated_at=_T0,
        name=name,
    )
    endpoint = SourceEndpoint(
        endpoint_id=SourceEndpointId.generate(),
        source_id=source.source_id,
        uri=f"{base_url}{start_path}",
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
        max_pages=6,
        max_requests=80,
        max_depth=2,
        timeout_seconds=180.0,
    )
    async with spi.unit_of_work() as uow:
        await uow.sources.create(source)
        await uow.sources.add_endpoint(endpoint)
        await uow.collection.create_policy(policy)
        await uow.commit()


def _stream(redpanda_settings: Any) -> RedpandaDataStream:
    return RedpandaDataStream(
        bootstrap_servers=redpanda_settings.bootstrap_servers,
        client_id="darkula-intg-v01",
        poll_timeout_ms=2000,
        max_poll_records=20,
    )


async def _run_one_collection(
    *,
    spi: PostgresDarkulaSpi,
    controller: CrawlerController,
    stream: RedpandaDataStream,
    topic: StreamName,
    store: LocalFileObjectStore,
    now: datetime,
) -> int:
    """Schedule -> outbox -> real Redpanda -> real worker/crawler/ingest."""
    scheduler = CollectionScheduler(spi=spi, stream_name=topic)
    scheduled = await scheduler.schedule_due(now=now, limit=5)
    publisher = OutboxPublisher(spi=spi, data_stream=stream)
    published = await publisher.publish_pending(stream_name=topic, limit=5)
    assert scheduled == published
    service = SourceCollectionService(
        spi=spi,
        crawler=controller,
        content_ingest=ContentIngestService(
            normalizer=DeterministicContentNormalizer(
                artifact_service=ArtifactStorageService(object_store=store)
            ),
            spi=spi,
        ),
        settings=CollectionSettings(),
    )
    worker = CollectionWorker(
        spi=spi,
        data_stream=stream,
        service=service,
        settings=CollectionSettings(),
        stream_name=topic,
        consumer_id=ConsumerId(f"intg-v01-wk-{uuid.uuid4().hex[:8]}"),
    )
    return await worker.process_once(max_records=5)


async def _canonical_text(
    spi: PostgresDarkulaSpi, store: LocalFileObjectStore, content_id: str
) -> str:
    async with spi.unit_of_work() as uow:
        content = await uow.content.get_observation(
            NormalizedContentId.from_str(content_id)
        )
        assert content is not None and content.artifact_id is not None
        artifact = await uow.content.get_artifact(content.artifact_id)
    assert artifact is not None
    reader = await store.get(artifact.object_key)
    payload = b"".join([chunk async for chunk in reader])
    assert artifact.content_hash is not None
    assert hashlib.sha256(payload).hexdigest() == artifact.content_hash.digest_hex
    return payload.decode("utf-8")


def _untrusted_block(call: FakeLlmCall) -> str:
    block = call.user_prompt.split("<<<UNTRUSTED_SOURCE_DATA>>>", 1)[1].split(
        "<<<END_UNTRUSTED_SOURCE_DATA>>>", 1
    )[0]
    return block.strip("\n")


def _grounded_mention(text: str, raw: str) -> tuple[str, str, str] | None:
    """Return (raw, left_context, right_context) for a uniquely-identifiable
    exact occurrence of ``raw`` (deterministic exact-context disambiguation)."""
    window = 40
    indices = [index for index in range(len(text)) if text.startswith(raw, index)]
    for index in indices:
        left = text[max(0, index - window) : index]
        right = text[index + len(raw) : index + len(raw) + window]
        matches = [
            other
            for other in indices
            if text[max(0, other - window) : other] == left
            and text[other + len(raw) : other + len(raw) + window] == right
        ]
        if len(matches) == 1:
            return raw, left, right
    return None


class TestV01FullStack:
    @pytest.mark.asyncio
    async def test_async_collection_then_extraction_and_replay(
        self,
        controller: CrawlerController,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        redpanda_settings: Any,
        tmp_path: Path,
    ) -> None:
        """E2E16-1/E2E16-2/E2E16-4: async collection, extraction, replay."""
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        await _seed_source(
            spi,
            base_url=_BLACKGATE_URL,
            start_path="/thread/thr-collector-samples",
            name="v01-blackgate",
        )
        topic = _topic()
        stream = _stream(redpanda_settings)
        await stream.start()
        try:
            assert (
                await _run_one_collection(
                    spi=spi,
                    controller=controller,
                    stream=stream,
                    topic=topic,
                    store=store,
                    now=_T0 + timedelta(seconds=1),
                )
                == 1
            )
            content_ids = _content_ids(database_settings)
            assert content_ids, "async collection persisted no normalized content"
            assert _stored_content_leaks(database_settings) == []

            extraction = DeterministicExtractionService(
                spi=spi,
                object_store=store,
                profile=deterministic_observables_profile(max_entities=5000),
            )
            first_results = []
            for content_id in content_ids:
                first_results.append(
                    await extraction.extract(NormalizedContentId.from_str(content_id))
                )
            assert any(result.entities for result in first_results)

            # Deterministic replay converges on the same durable result.
            replay = await extraction.extract(
                NormalizedContentId.from_str(content_ids[0])
            )
            assert replay.result.result_id == first_results[0].result.result_id
            assert replay.created_result is False

            # Model-backed semantic extraction through the existing LlmClient.
            calls: list[FakeLlmCall] = []

            def responder(call: FakeLlmCall) -> SemanticExtractionResponse:
                calls.append(call)
                text = _untrusted_block(call)
                grounded = _grounded_mention(text, "BlackGate")
                if grounded is None:
                    return SemanticExtractionResponse(mentions=[])
                raw, left, right = grounded
                return SemanticExtractionResponse(
                    mentions=[
                        SemanticMentionCandidate(
                            entity_type=EntityType.ORGANIZATION,
                            raw_value=raw,
                            normalized_value=raw,
                            left_context=left,
                            right_context=right,
                            confidence=0.9,
                        )
                    ]
                )

            semantic = SemanticExtractionService(
                spi=spi,
                object_store=store,
                llm=FakeLlmClient(
                    response_factory=responder,
                    expected_response_model=SemanticExtractionResponse,
                ),
                max_entities=SemanticExtractionSettings().max_entities_per_content,
                max_input_bytes=SemanticExtractionSettings().max_input_bytes,
            )
            target: str | None = None
            for cid in content_ids:
                if "BlackGate" in await _canonical_text(spi, store, cid):
                    target = cid
                    break
            assert target is not None, "no canonical content contained BlackGate"
            first = await semantic.extract(NormalizedContentId.from_str(target))
            calls_after_first = len(calls)
            assert first.entities
            # Replay must not issue a second model call.
            second = await semantic.extract(NormalizedContentId.from_str(target))
            assert second.result.result_id == first.result.result_id
            assert second.created_result is False
            assert len(calls) == calls_after_first
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_cross_source_isolation(
        self,
        controller: CrawlerController,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        redpanda_settings: Any,
        tmp_path: Path,
    ) -> None:
        """E2E16-3: a second source stays content-local (no global merge)."""
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        await _seed_source(
            spi,
            base_url=_ACCESSBAY_URL,
            start_path="/listings",
            name="v01-accessbay",
        )
        topic = _topic()
        stream = _stream(redpanda_settings)
        await stream.start()
        try:
            assert (
                await _run_one_collection(
                    spi=spi,
                    controller=controller,
                    stream=stream,
                    topic=topic,
                    store=store,
                    now=_T0 + timedelta(seconds=1),
                )
                == 1
            )
            uris = _content_uris(database_settings)
            assert uris
            assert all(uri.startswith(_ACCESSBAY_URL) for uri in uris)
            assert _stored_content_leaks(database_settings) == []
        finally:
            await stream.stop()
