# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 12 canonical vertical slice: real crawler -> semantic extraction -> geography.

Real CrawlerController -> real PodmanSandbox -> Playwright/Chromium -> BlackGate
HTTP -> production mapper -> deterministic normalization -> real ObjectStore ->
real PostgreSQL NormalizedContent/ContentArtifact -> real
SemanticExtractionService -> FakeLlmClient -> real grounding -> real PostgreSQL
ExtractionResult/ExtractedEntity -> real GeographicResolutionService ->
FakeGeographicResolver -> real PostgreSQL GeographicResolution.

Only the external model response, external geographic resolver, and external
world (Fake World) are faked; no Darkula architecture under test is faked.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from darkula.app.artifacts import ArtifactStorageService
from darkula.app.content import ContentIngestService
from darkula.app.extraction import (
    DeterministicExtractionService,
    deterministic_observables_profile,
)
from darkula.app.geography import GeographicResolutionService
from darkula.app.normalization import DeterministicContentNormalizer
from darkula.app.object_store import ObjectStore
from darkula.app.semantic_extraction import (
    SemanticExtractionResponse,
    SemanticExtractionService,
    SemanticMentionCandidate,
)
from darkula.config.settings import DatabaseSettings
from darkula.crawler import CrawlerController, CrawlStatus
from darkula.crawler.mapping import page_observation_to_content
from darkula.domain.extraction import EntityType
from darkula.domain.geography import (
    GeographicResolutionStatus,
    GeographicResolverIdentity,
    GeographicResolverResult,
)
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_geographic_resolver import FakeGeographicResolver
from darkula.testing.fake_llm import FakeLlmClient
from tests.integration.conftest import _raw_connect
from tests.integration.crawler.helpers import crawl_request, truth_leaks

pytestmark = pytest.mark.integration


def _obs_at() -> datetime:
    return datetime(2026, 5, 1, tzinfo=UTC)


def _reset(database_settings: DatabaseSettings) -> None:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE geographic_resolution, extracted_entity, "
                "extraction_result, normalized_content, content_artifact CASCADE"
            )
        conn.commit()
    finally:
        conn.close()


async def _canonical_text(
    spi: PostgresDarkulaSpi, store: LocalFileObjectStore, content_id: object
) -> str:
    from darkula.domain.identifiers import NormalizedContentId

    assert isinstance(content_id, NormalizedContentId)
    async with spi.unit_of_work() as uow:
        content = await uow.content.get_observation(content_id)
        assert content is not None and content.artifact_id is not None
        artifact = await uow.content.get_artifact(content.artifact_id)
    assert artifact is not None
    reader = await store.get(artifact.object_key)
    payload = b"".join([chunk async for chunk in reader])
    assert artifact.content_hash is not None
    assert hashlib.sha256(payload).hexdigest() == artifact.content_hash.digest_hex
    return payload.decode("utf-8")


def _semantic_service(
    spi: PostgresDarkulaSpi, store: ObjectStore, llm: FakeLlmClient
) -> SemanticExtractionService:
    return SemanticExtractionService(
        spi=spi,
        object_store=store,
        llm=llm,
        max_entities=100,
        max_input_bytes=262144,
    )


class TestSemanticExtractionVerticalSlice:
    @pytest.mark.asyncio
    async def test_real_crawler_semantic_and_geographic_slice(
        self,
        controller: CrawlerController,
        fake_world_url: str,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        normalizer = DeterministicContentNormalizer(
            artifact_service=ArtifactStorageService(object_store=store)
        )
        ingest = ContentIngestService(normalizer=normalizer, spi=spi)

        result = await controller.crawl(
            crawl_request(
                fake_world_url,
                start_path="/thread/thr-hospital-creds",
                max_pages=8,
                max_requests=60,
                max_depth=2,
                timeout_seconds=180.0,
            )
        )
        assert result.status is CrawlStatus.COMPLETED, result.reason
        assert truth_leaks(result) == []
        assert result.pages, "real crawler produced no page observations"

        ingested = []
        for index, page in enumerate(result.pages, start=1):
            observation = page_observation_to_content(
                page,
                crawl_request_id=result.request_id,
                observed_at=_obs_at(),
                observation_index=index,
            )
            ingested.append(await ingest.ingest(observation))

        llm = FakeLlmClient(
            default_response=SemanticExtractionResponse(
                mentions=[
                    SemanticMentionCandidate(
                        entity_type=EntityType.ORGANIZATION,
                        raw_value="Mason Creek General Hospital",
                        confidence=0.93,
                        left_context="admin for ",
                        right_context=" (Mason Creek",
                    ),
                    SemanticMentionCandidate(
                        entity_type=EntityType.LOCATION,
                        raw_value="Washington",
                        confidence=0.81,
                        left_context="Mason Creek, ",
                        right_context=")",
                    ),
                ]
            )
        )
        semantic = _semantic_service(spi, store, llm)

        target = None
        for item in ingested:
            extracted = await semantic.extract(item.normalized_content.content_id)
            types = {entity.entity_type for entity in extracted.entities}
            if EntityType.ORGANIZATION in types and EntityType.LOCATION in types:
                target = (item, extracted)
                break
        assert target is not None, "no crawled page grounded the hospital organization"
        item, extracted = target
        text = await _canonical_text(spi, store, item.normalized_content.content_id)

        # 1-2) Real content, no truth leak.
        entity_types = {entity.entity_type for entity in extracted.entities}
        assert EntityType.ORGANIZATION in entity_types
        assert EntityType.LOCATION in entity_types

        # 3) Exact spans map back to canonical text.
        for entity in extracted.entities:
            span = entity.source_span
            assert text[span.start : span.end] == entity.raw_value

        # 4) Extraction confidence is persisted.
        assert all(
            entity.extraction_confidence is not None for entity in extracted.entities
        )

        # 5) Model received observable content, never truth.
        assert llm.call_count >= 1
        matching_calls = [
            call
            for call in llm.calls
            if "Mason Creek General Hospital" in call.user_prompt
        ]
        assert matching_calls, "the model never received the hospital content"
        for call in matching_calls:
            for token in ("actor-001", "alias-002", "zfox", "relationship-001"):
                assert token not in call.user_prompt

        # 6) Semantic result coexists with deterministic extraction.
        deterministic = await DeterministicExtractionService(
            spi=spi,
            object_store=store,
            profile=deterministic_observables_profile(max_entities=100),
        ).extract(item.normalized_content.content_id)
        assert deterministic.result.profile_name == "deterministic-observables"
        assert deterministic.result.result_id != extracted.result.result_id

        # 7) LOCATION occurrence before resolution.
        location = next(
            entity
            for entity in extracted.entities
            if entity.entity_type is EntityType.LOCATION
        )
        before_raw = location.raw_value
        before_normalized = location.normalized_value
        before_span = location.source_span

        # 8) Geographic resolution with a deterministic test resolver.
        resolver = FakeGeographicResolver(
            identity=GeographicResolverIdentity(name="fake-geo", version="v1"),
            default_result=GeographicResolverResult(
                status=GeographicResolutionStatus.RESOLVED,
                canonical_name="Washington (State)",
                country_code="US",
                administrative_area="Washington",
                latitude=47.75,
                longitude=-120.74,
                confidence=0.77,
                reference="test-place-1",
            ),
        )
        geography = GeographicResolutionService(
            spi=spi,
            object_store=store,
            resolver=resolver,
            max_input_bytes=262144,
        )
        resolved = await geography.resolve(location.entity_id)
        assert resolved.created_resolution is True
        assert resolved.resolution.canonical_name == "Washington (State)"
        # Resolver received bounded context but not the whole content.
        request = resolver.calls[0].request
        assert request.mention == "Washington"
        assert "Mason Creek" in request.left_context
        assert len(request.left_context) < len(text)

        # 9) Resolution does not mutate the extracted occurrence.
        async with spi.unit_of_work() as uow:
            reloaded = await uow.extraction.get_entity(location.entity_id)
        assert reloaded is not None
        assert reloaded.raw_value == before_raw
        assert reloaded.normalized_value == before_normalized
        assert reloaded.source_span == before_span

        # 10) Replay reuses the durable resolution without a resolver call.
        replay = await geography.resolve(location.entity_id)
        assert replay.created_resolution is False
        assert resolver.call_count == 1

        # 11) Replay reuses the semantic result without another model call.
        replay_semantic = await semantic.extract(item.normalized_content.content_id)
        assert replay_semantic.created_result is False
        assert replay_semantic.result.result_id == extracted.result.result_id

        # 12) No SourceAssessment and no relationship behavior.
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM source_assessment")
                assessments = cur.fetchone()
        finally:
            conn.close()
        assert assessments is not None and assessments[0] == 0
