# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 16 canonical v0.1 full-stack slice (E2E16A-1..E2E16A-17).

One canonical path begins with the real asynchronous collection path and
continues through every delivered downstream stage to a persisted
``SourceAssessment``:

    PostgreSQL scheduler + transactional outbox
    -> real Redpanda
    -> real CollectionWorker
    -> CrawlerController -> PodmanSandbox -> CrawlerRuntime -> Chromium
    -> Fake World HTTP
    -> normalization -> local ObjectStore -> PostgreSQL NormalizedContent
    -> DeterministicExtractionService
    -> SemanticExtractionService (only the LLM is fake)
    -> GeographicResolutionService (only the resolver provider is fake)
    -> RelationshipExtractionService (only the LLM is fake)
    -> SourceAnalysisContextBuilder -> SourceAnalyst (only the LLM is fake)
    -> SourceAnalysisService -> persisted SourceAssessment

Only the external world (Fake World) and the non-deterministic model/provider
boundaries (``FakeLlmClient`` / ``FakeGeographicResolver``) are faked. No
orchestrator, fake service, or test-seeded downstream row is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from darkula.app.artifacts import ArtifactStorageService
from darkula.app.collection import SourceCollectionService
from darkula.app.collection_scheduler import CollectionScheduler
from darkula.app.collection_worker import CollectionWorker
from darkula.app.content import ContentIngestService
from darkula.app.data_stream import (
    DataStream,
    MessageBatch,
    PublishError,
    PublishResult,
    StreamMessage,
    StreamPosition,
)
from darkula.app.extraction import (
    DeterministicExtractionService,
    deterministic_observables_profile,
)
from darkula.app.geography import GeographicResolutionService
from darkula.app.normalization import DeterministicContentNormalizer
from darkula.app.outbox import OutboxPublisher
from darkula.app.relationship_extraction import (
    RelationshipCandidate,
    RelationshipExtractionResponse,
    RelationshipExtractionService,
)
from darkula.app.semantic_extraction import (
    SemanticExtractionResponse,
    SemanticExtractionService,
    SemanticMentionCandidate,
)
from darkula.app.source_analysis import (
    SourceAnalysisContextBuilder,
    SourceAnalysisEvidenceKind,
    SourceAnalysisRequest,
    SourceAnalysisService,
)
from darkula.app.source_analyst import (
    SourceAnalysisResponse,
    SourceAnalyst,
    SourceCharacteristics,
)
from darkula.config.settings import (
    CollectionSettings,
    DatabaseSettings,
    SemanticExtractionSettings,
    SourceAnalysisSettings,
)
from darkula.crawler import CrawlerController
from darkula.domain.collection import CollectionPolicy, CollectionRunStatus
from darkula.domain.extraction import EntityType
from darkula.domain.geography import (
    GeographicResolutionStatus,
    GeographicResolverResult,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    ConsumerId,
    NormalizedContentId,
    SourceEndpointId,
    SourceId,
    StreamName,
)
from darkula.domain.relationships import RelationshipPredicate
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
from darkula.testing.fake_geographic_resolver import FakeGeographicResolver
from darkula.testing.fake_llm import FakeLlmCall, FakeLlmClient
from tests.integration.conftest import _raw_connect

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 13, 0, 0, tzinfo=UTC)
_BLACKGATE_URL = "http://darkula-fake-world-intg:8080"
#: Cross-source virtual host served by the same Fake World container.
_ACCESSBAY_URL = "http://accessbay.example.test:8080"
#: Canonical downstream target text present on the public AccessBay catalogue.
_LOCATION_RAW = "WA"
_ORGANIZATION_RAW = "Mason Creek General Hospital"

#: Cross-source truth that is never rendered by any source.
_HIDDEN_TRUTH_TOKENS = ("ghost_admin", "actor-001", "alias-004", "event-004")
#: Synthetic test passwords that must never appear in collected content/prompts.
_SYNTHETIC_PASSWORDS = (
    "blackgate-test-password",
    "accessbay-test-password",
    "nightleak-test-password",
    "shadowtalk-test-password",
)

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


def _unpublished_outbox_rows(database_settings: DatabaseSettings) -> int:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM message_outbox WHERE published_at IS NULL"
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _run_ids(database_settings: DatabaseSettings, policy_id: Any) -> list[str]:
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
    from darkula.domain.identifiers import CollectionRunId

    loaded: list[Any] = []
    for run_id in run_ids:
        async with spi.unit_of_work() as uow:
            run = await uow.collection.get_run(CollectionRunId.from_str(run_id))
        assert run is not None
        loaded.append(run)
    return loaded


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
        for token in (*_HIDDEN_TRUTH_TOKENS, *_SYNTHETIC_PASSWORDS)
        if token.lower() in haystack
    ]


async def _seed_source(
    spi: PostgresDarkulaSpi, *, base_url: str, start_path: str, name: str
) -> tuple[Source, SourceEndpoint, CollectionPolicy]:
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
    return source, endpoint, policy


def _stream(redpanda_settings: Any) -> RedpandaDataStream:
    return RedpandaDataStream(
        bootstrap_servers=redpanda_settings.bootstrap_servers,
        client_id="darkula-intg-v01",
        poll_timeout_ms=2000,
        max_poll_records=20,
    )


def _ingest(
    spi: PostgresDarkulaSpi, store: LocalFileObjectStore
) -> ContentIngestService:
    return ContentIngestService(
        normalizer=DeterministicContentNormalizer(
            artifact_service=ArtifactStorageService(object_store=store)
        ),
        spi=spi,
    )


async def _run_one_collection(
    *,
    spi: PostgresDarkulaSpi,
    controller: CrawlerController,
    stream: DataStream,
    topic: StreamName,
    store: LocalFileObjectStore,
    now: datetime,
    service_clock: datetime | None = None,
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
        content_ingest=_ingest(spi, store),
        settings=CollectionSettings(),
        clock=(lambda: service_clock) if service_clock is not None else None,
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


def _semantic_responder(call: FakeLlmCall) -> SemanticExtractionResponse:
    """Return only exact-grounded mentions from the real untrusted block."""
    text = _untrusted_block(call)
    mentions: list[SemanticMentionCandidate] = []
    for raw, entity_type in (
        (_ORGANIZATION_RAW, EntityType.ORGANIZATION),
        (_LOCATION_RAW, EntityType.LOCATION),
    ):
        grounded = _grounded_mention(text, raw)
        if grounded is None:
            continue
        _, left, right = grounded
        mentions.append(
            SemanticMentionCandidate(
                entity_type=entity_type,
                raw_value=raw,
                normalized_value=raw,
                left_context=left,
                right_context=right,
                confidence=0.9,
            )
        )
    return SemanticExtractionResponse(mentions=mentions)


def _catalog_entries(call: FakeLlmCall) -> list[tuple[str, str, str, int, int]]:
    block = call.user_prompt.split("<<<ENDPOINT_CATALOG>>>", 1)[1].split(
        "<<<END_ENDPOINT_CATALOG>>>", 1
    )[0]
    entries: list[tuple[str, str, str, int, int]] = []
    for line in block.strip().splitlines():
        if ": type=" not in line:
            continue
        ref, rest = line.split(": type=", 1)
        type_part = rest.split(', text="', 1)[0]
        quoted = rest.split(', text="', 1)[1]
        value, span_part = quoted.rsplit('", span=', 1)
        start_text, end_text = span_part.split("..", 1)
        entries.append((ref.strip(), type_part, value, int(start_text), int(end_text)))
    return entries


def _relationship_factory(call: FakeLlmCall) -> RelationshipExtractionResponse:
    entries = _catalog_entries(call)
    assert len(entries) >= 2, "relationship stage requires >= 2 persisted occurrences"
    first, second = entries[0], entries[1]
    assert first[0] != second[0], "relationship endpoints must be distinct occurrences"
    text = _untrusted_block(call)
    start = min(first[3], second[3])
    end = max(first[4], second[4])
    return RelationshipExtractionResponse(
        relationships=[
            RelationshipCandidate(
                source_ref=first[0],
                predicate=RelationshipPredicate.AFFILIATED_WITH,
                target_ref=second[0],
                support_text=text[start:end],
                confidence=0.8,
            )
        ]
    )


def _analysis_response() -> SourceAnalysisResponse:
    return SourceAnalysisResponse(
        confidence=0.8,
        relevance=0.7,
        activity=0.6,
        novelty=None,
        characteristics=SourceCharacteristics(
            source_type="marketplace",
            content_focus=["access listings"],
            summary="A synthetic marketplace observed during the window.",
        ),
        evidence_refs=["A1"],
    )


class _FailOncePublishStream(DataStream):
    """Transport-boundary test seam: the first publish fails, later calls delegate.

    This crosses a real system boundary (the DataStream/Redpanda transport)
    without touching production code or adding a production fault switch.
    """

    def __init__(self, wrapped: RedpandaDataStream) -> None:
        self._wrapped = wrapped
        self.failed = False

    async def start(self) -> None:
        await self._wrapped.start()

    async def stop(self) -> None:
        await self._wrapped.stop()

    async def _publish(
        self, *, stream_name: StreamName, messages: Sequence[StreamMessage]
    ) -> PublishResult:
        if not self.failed:
            self.failed = True
            raise PublishError("injected publication failure before broker accept")
        return await self._wrapped.publish(stream_name=stream_name, messages=messages)

    async def _poll(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        max_records: int,
    ) -> MessageBatch:
        return await self._wrapped.poll(
            stream_name=stream_name, consumer_id=consumer_id, max_records=max_records
        )

    async def _acknowledge(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        positions: Sequence[StreamPosition],
    ) -> None:
        await self._wrapped.acknowledge(
            stream_name=stream_name, consumer_id=consumer_id, positions=positions
        )


class TestV01FullStack:
    @pytest.mark.asyncio
    async def test_async_collection_through_source_assessment(
        self,
        controller: CrawlerController,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        redpanda_settings: Any,
        tmp_path: Path,
    ) -> None:
        """E2E16A-1..E2E16A-17: async collection -> durable SourceAssessment."""
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        source, endpoint, _policy = await _seed_source(
            spi,
            base_url=_ACCESSBAY_URL,
            start_path="/listings",
            name="v01-accessbay",
        )
        topic = _topic()
        stream = _stream(redpanda_settings)
        await stream.start()
        try:
            # -- E2E16A-1: real async path persists source-linked content.
            assert (
                await _run_one_collection(
                    spi=spi,
                    controller=controller,
                    stream=stream,
                    topic=topic,
                    store=store,
                    now=_T0 + timedelta(seconds=1),
                    service_clock=_T0 + timedelta(seconds=2),
                )
                == 1
            )
            run_ids = _run_ids(database_settings, _policy.policy_id)
            assert len(run_ids) == 1
            runs = await _load_runs(spi, run_ids)
            assert runs[0].status is CollectionRunStatus.SUCCEEDED

            content_ids = sorted(_content_ids(database_settings))
            assert content_ids, "async collection persisted no normalized content"
            assert _stored_content_leaks(database_settings) == []
            assert all(
                uri.startswith(_ACCESSBAY_URL)
                for uri in _content_uris(database_settings)
            )

            # -- E2E16A-2: canonical bytes match the persisted SHA-256.
            target: str | None = None
            target_text = ""
            for content_id in content_ids:
                text = await _canonical_text(spi, store, content_id)
                if _ORGANIZATION_RAW in text and _LOCATION_RAW in text:
                    target = content_id
                    target_text = text
                    break
            assert target is not None, (
                "no collected AccessBay content contained the canonical "
                "organization/location mentions"
            )

            # -- E2E16A-3/4: deterministic extraction + replay convergence.
            extraction = DeterministicExtractionService(
                spi=spi,
                object_store=store,
                profile=deterministic_observables_profile(max_entities=5000),
            )
            first_deterministic = await extraction.extract(
                NormalizedContentId.from_str(target)
            )
            replay_deterministic = await extraction.extract(
                NormalizedContentId.from_str(target)
            )
            assert first_deterministic.entities, (
                "deterministic extraction persisted no grounded occurrences"
            )
            for entity in first_deterministic.entities:
                assert (
                    target_text[entity.source_span.start : entity.source_span.end]
                    == entity.raw_value
                )
            assert (
                replay_deterministic.result.result_id
                == first_deterministic.result.result_id
            )
            assert replay_deterministic.created_result is False

            # -- E2E16A-5/6: real semantic extraction + replay (no second call).
            semantic_calls: list[FakeLlmCall] = []

            def semantic_factory(call: FakeLlmCall) -> SemanticExtractionResponse:
                semantic_calls.append(call)
                return _semantic_responder(call)

            semantic = SemanticExtractionService(
                spi=spi,
                object_store=store,
                llm=FakeLlmClient(
                    response_factory=semantic_factory,
                    expected_response_model=SemanticExtractionResponse,
                ),
                max_entities=SemanticExtractionSettings().max_entities_per_content,
                max_input_bytes=SemanticExtractionSettings().max_input_bytes,
            )
            semantic_first = await semantic.extract(
                NormalizedContentId.from_str(target)
            )
            semantic_calls_after_first = len(semantic_calls)
            semantic_replay = await semantic.extract(
                NormalizedContentId.from_str(target)
            )
            assert semantic_replay.result.result_id == semantic_first.result.result_id
            assert semantic_replay.created_result is False
            assert len(semantic_calls) == semantic_calls_after_first

            # -- E2E16A-7: the LOCATION is produced by the real semantic stage.
            async with spi.unit_of_work() as uow:
                occurrences = await uow.extraction.list_entities_for_content(
                    NormalizedContentId.from_str(target)
                )
            locations = tuple(
                item
                for item in occurrences
                if item.entity_type is EntityType.LOCATION
                and item.normalized_value == _LOCATION_RAW
            )
            assert locations, "semantic extraction produced no grounded LOCATION"
            location = locations[0]
            assert (
                target_text[location.source_span.start : location.source_span.end]
                == location.raw_value
            )

            # -- E2E16A-8/9: real geographic resolution + replay.
            resolver = FakeGeographicResolver(
                default_result=GeographicResolverResult(
                    status=GeographicResolutionStatus.RESOLVED,
                    canonical_name="Mason Creek, Washington, United States",
                    country_code="US",
                    confidence=0.9,
                )
            )
            geography = GeographicResolutionService(
                spi=spi,
                object_store=store,
                resolver=resolver,
                context_chars=200,
                max_input_bytes=1048576,
            )
            resolved = await geography.resolve(location.entity_id)
            assert resolved.resolution.status is GeographicResolutionStatus.RESOLVED
            assert resolved.resolution.extracted_entity_id == location.entity_id
            resolved_replay = await geography.resolve(location.entity_id)
            assert (
                resolved_replay.resolution.resolution_id
                == resolved.resolution.resolution_id
            )
            assert resolved_replay.created_resolution is False
            assert resolver.call_count == 1

            # -- E2E16A-10/11: real relationship extraction + replay.
            relationship_calls: list[FakeLlmCall] = []

            def relationship_factory(
                call: FakeLlmCall,
            ) -> RelationshipExtractionResponse:
                relationship_calls.append(call)
                return _relationship_factory(call)

            relationship = RelationshipExtractionService(
                spi=spi,
                object_store=store,
                llm=FakeLlmClient(
                    response_factory=relationship_factory,
                    expected_response_model=RelationshipExtractionResponse,
                ),
                max_relationships=100,
                max_support_chars=4096,
                max_context_chars=256,
                max_input_bytes=262144,
            )
            relationship_first = await relationship.extract(
                NormalizedContentId.from_str(target)
            )
            relationship_calls_after_first = len(relationship_calls)
            relationship_replay = await relationship.extract(
                NormalizedContentId.from_str(target)
            )
            assert relationship_first.relationships
            assert (
                relationship_replay.result.result_id
                == relationship_first.result.result_id
            )
            assert relationship_replay.created_result is False
            assert len(relationship_calls) == relationship_calls_after_first
            async with spi.unit_of_work() as uow:
                persisted_relationships = await uow.relationships.list_for_content(
                    NormalizedContentId.from_str(target)
                )
            assert persisted_relationships
            persisted_entity_ids = {item.entity_id for item in occurrences}
            for assertion in persisted_relationships:
                assert assertion.content_id == NormalizedContentId.from_str(target)
                assert assertion.source_entity_id in persisted_entity_ids
                assert assertion.target_entity_id in persisted_entity_ids
                assert (
                    target_text[
                        assertion.support_span.start : assertion.support_span.end
                    ]
                    == assertion.support_text
                )

            # -- E2E16A-12/16: real bounded context with all five kinds.
            analysis_llm = FakeLlmClient(
                default_response=_analysis_response(),
                expected_response_model=SourceAnalysisResponse,
            )
            analysis_settings = SourceAnalysisSettings()
            builder = SourceAnalysisContextBuilder(spi=spi, settings=analysis_settings)
            request = SourceAnalysisRequest(source.source_id, _T0, _T1)
            context = await builder.build(request)
            kinds = {item.kind for item in context.evidence}
            for required in (
                SourceAnalysisEvidenceKind.CONTENT_OBSERVATION,
                SourceAnalysisEvidenceKind.ENTITY_OCCURRENCE,
                SourceAnalysisEvidenceKind.GEOGRAPHIC_RESOLUTION,
                SourceAnalysisEvidenceKind.RELATIONSHIP_ASSERTION,
                SourceAnalysisEvidenceKind.COLLECTION_RUN,
            ):
                assert required in kinds, f"missing evidence kind {required}"

            # -- E2E16A-13/14: real SourceAnalysisService + replay.
            service = SourceAnalysisService(
                spi=spi,
                context_builder=builder,
                analyst=SourceAnalyst(llm=analysis_llm),
                settings=analysis_settings,
                clock=lambda: _T1,
            )
            outcome = await service.analyze(request)
            assert outcome.created is True
            assert outcome.assessment.source_id == source.source_id
            assert outcome.assessment.window_start == _T0
            assert outcome.assessment.profile_name == "source-analysis"
            assert outcome.assessment.evidence_references
            replay = await service.analyze(request)
            assert replay.created is False
            assert replay.assessment.assessment_id == outcome.assessment.assessment_id
            assert analysis_llm.call_count == 1

            # E2E16A-13/16: the assessment and its evidence refs are durable.
            async with spi.unit_of_work() as uow:
                stored_assessment = await uow.sources.get_source_assessment_by_profile(
                    source.source_id, _T0, _T1, "source-analysis", "v1"
                )
            assert stored_assessment is not None
            assert stored_assessment.assessment_id == outcome.assessment.assessment_id
            assert stored_assessment.evidence_references
            assert stored_assessment.source_id == source.source_id

            # -- E2E16A-15: truth/secret isolation across prompts/persistence.
            prompts = [
                call.system_prompt + call.user_prompt
                for call in (*semantic_calls, *relationship_calls, *analysis_llm.calls)
            ]
            for sentinel in (*_HIDDEN_TRUTH_TOKENS, *_SYNTHETIC_PASSWORDS):
                for prompt in prompts:
                    assert sentinel.lower() not in prompt.lower()
            for call in relationship_calls:
                assert "actor-001" not in call.system_prompt
            assert _stored_content_leaks(database_settings) == []

            # -- E2E16A-17: analysis never mutates Source lifecycle.
            async with spi.unit_of_work() as uow:
                reloaded_source = await uow.sources.get(source.source_id)
                endpoints = await uow.sources.list_endpoints(source.source_id)
            assert reloaded_source == source
            assert endpoints == (endpoint,)
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
        """A second logical source stays content-local (no hidden truth)."""
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        await _seed_source(
            spi,
            base_url=_BLACKGATE_URL,
            start_path="/thread/thr-collector-samples",
            name="v01-blackgate-isolation",
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
                    service_clock=_T0 + timedelta(seconds=2),
                )
                == 1
            )
            uris = _content_uris(database_settings)
            assert uris
            assert all(uri.startswith(_BLACKGATE_URL) for uri in uris)
            assert _stored_content_leaks(database_settings) == []
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_publication_failure_then_retry_converges(
        self,
        controller: CrawlerController,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        redpanda_settings: Any,
        tmp_path: Path,
    ) -> None:
        """FR16A-1..FR16A-8: real boundary failure -> retry -> one effect.

        The failure is injected at the transport boundary (the first
        ``DataStream.publish`` fails before broker acceptance), so the
        transactional outbox row stays retryable. Recovery uses the existing
        ``OutboxPublisher`` retry path and the existing worker; no production
        retry policy is changed and no production fault switch is added.
        """
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        _source, _endpoint, policy = await _seed_source(
            spi,
            base_url=_BLACKGATE_URL,
            start_path="/thread/thr-collector-samples",
            name="v01-recovery",
        )
        topic = _topic()
        real_stream = _stream(redpanda_settings)
        stream = _FailOncePublishStream(real_stream)
        await stream.start()
        try:
            # FR16A-1: real admission commits run + outbox atomically.
            scheduler = CollectionScheduler(spi=spi, stream_name=topic)
            assert await scheduler.schedule_due(now=_T0 + timedelta(seconds=1), limit=5)
            assert _unpublished_outbox_rows(database_settings) == 1

            # FR16A-2/3: the first publication fails safely before any ack.
            publisher = OutboxPublisher(spi=spi, data_stream=stream)
            with pytest.raises(PublishError):
                await publisher.publish_pending(
                    stream_name=topic, limit=5, lease_seconds=0.2
                )
            assert _unpublished_outbox_rows(database_settings) == 1
            assert _content_ids(database_settings) == []
            assert _run_ids(database_settings, policy.policy_id)  # still queued

            # FR16A-4/5: the existing outbox retry path republishes.
            await asyncio.sleep(0.3)
            assert (
                await publisher.publish_pending(
                    stream_name=topic, limit=5, lease_seconds=30.0
                )
                == 1
            )
            assert _unpublished_outbox_rows(database_settings) == 0
            assert stream.failed is True

            # FR16A-5/6: the real worker converges to one authoritative effect.
            service = SourceCollectionService(
                spi=spi,
                crawler=controller,
                content_ingest=_ingest(spi, store),
                settings=CollectionSettings(),
                clock=lambda: _T0 + timedelta(seconds=2),
            )
            worker = CollectionWorker(
                spi=spi,
                data_stream=stream,
                service=service,
                settings=CollectionSettings(),
                stream_name=topic,
                consumer_id=ConsumerId(f"intg-v01-recover-{uuid.uuid4().hex[:8]}"),
            )
            assert await worker.process_once(max_records=5) == 1
            run_ids = _run_ids(database_settings, policy.policy_id)
            assert len(run_ids) == 1
            runs = await _load_runs(spi, run_ids)
            assert runs[0].status is CollectionRunStatus.SUCCEEDED
            content_after = _content_ids(database_settings)
            assert content_after

            # FR16A-7: duplicate delivery is a no-op (no duplicate state).
            assert await worker.process_once(max_records=5) == 0
            assert _run_ids(database_settings, policy.policy_id) == run_ids
            assert _content_ids(database_settings) == content_after
        finally:
            await stream.stop()
