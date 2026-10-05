# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 13 canonical vertical slice: real crawler -> relationship assertions.

Real CrawlerController -> real PodmanSandbox -> Playwright/Chromium -> BlackGate
HTTP -> production mapper -> deterministic normalization -> real ObjectStore ->
real PostgreSQL NormalizedContent/ContentArtifact -> real
SemanticExtractionService -> FakeLlmClient -> real grounding -> real persisted
ExtractedEntity occurrences -> real RelationshipExtractionService ->
FakeLlmClient -> trusted endpoint-ref/support grounding -> real PostgreSQL
RelationshipExtractionResult/ExtractedRelationship.

Only the external model responses and the external world (Fake World) are
faked; no Darkula architecture under test is faked.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from darkula.app.artifacts import ArtifactStorageService
from darkula.app.content import ContentIngestService
from darkula.app.normalization import DeterministicContentNormalizer
from darkula.app.object_store import ObjectStore
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
from darkula.config.settings import DatabaseSettings
from darkula.crawler import CrawlerController, CrawlStatus
from darkula.crawler.mapping import page_observation_to_content
from darkula.domain.extraction import EntityType
from darkula.domain.identifiers import NormalizedContentId
from darkula.domain.relationships import (
    RELATIONSHIP_ASSERTIONS_PROFILE_NAME,
    RELATIONSHIP_ASSERTIONS_PROFILE_VERSION,
    RelationshipPredicate,
)
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_llm import FakeLlmCall, FakeLlmClient
from tests.integration.conftest import _raw_connect
from tests.integration.crawler.helpers import crawl_request, truth_leaks

pytestmark = pytest.mark.integration

_HOSPITAL_MENTION = "Mason Creek General Hospital"
_LOCATION_MENTION = "Washington"


def _obs_at() -> datetime:
    return datetime(2026, 5, 1, tzinfo=UTC)


def _reset(database_settings: DatabaseSettings) -> None:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE extracted_relationship, relationship_extraction_result, "
                "geographic_resolution, extracted_entity, extraction_result, "
                "normalized_content, content_artifact CASCADE"
            )
        conn.commit()
    finally:
        conn.close()


async def _canonical_text(
    spi: PostgresDarkulaSpi, store: LocalFileObjectStore, content_id: object
) -> str:
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


def _semantic_response() -> SemanticExtractionResponse:
    return SemanticExtractionResponse(
        mentions=[
            SemanticMentionCandidate(
                entity_type=EntityType.ORGANIZATION,
                raw_value=_HOSPITAL_MENTION,
                confidence=0.93,
                left_context="admin for ",
                right_context=" (Mason Creek",
            ),
            SemanticMentionCandidate(
                entity_type=EntityType.LOCATION,
                raw_value=_LOCATION_MENTION,
                confidence=0.81,
                left_context="Mason Creek, ",
                right_context=")",
            ),
        ]
    )


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


def _find_entry(
    entries: list[tuple[str, str, str, int, int]], entity_type: str, value: str
) -> tuple[str, int, int] | None:
    for ref, type_part, text_value, start, end in entries:
        if type_part == entity_type and text_value == value:
            return ref, start, end
    return None


def _prompt_source_text(call: FakeLlmCall) -> str:
    remainder = call.user_prompt.split("<<<UNTRUSTED_SOURCE_DATA>>>", 1)[1]
    # The prompt wraps the canonical text as ``\n<text>\n`` between markers.
    return remainder.split("<<<END_UNTRUSTED_SOURCE_DATA>>>", 1)[0][1:-1]


def _relationship_factory(call: FakeLlmCall) -> RelationshipExtractionResponse:
    entries = _catalog_entries(call)
    organization = _find_entry(entries, "ORGANIZATION", _HOSPITAL_MENTION)
    location = _find_entry(entries, "LOCATION", _LOCATION_MENTION)
    assert organization is not None, "organization endpoint not in catalog"
    assert location is not None, "location endpoint not in catalog"
    org_ref, org_start, org_end = organization
    loc_ref, loc_start, loc_end = location
    text = _prompt_source_text(call)
    # Support spans exactly the two grounded endpoint occurrences, so it is
    # guaranteed to contain both endpoint spans.
    start = min(org_start, loc_start)
    end = max(org_end, loc_end)
    support_text = text[start:end]
    return RelationshipExtractionResponse(
        relationships=[
            RelationshipCandidate(
                source_ref=org_ref,
                predicate=RelationshipPredicate.LOCATED_IN,
                target_ref=loc_ref,
                support_text=support_text,
                confidence=0.88,
            )
        ]
    )


def _relationship_service(
    spi: PostgresDarkulaSpi, store: ObjectStore, llm: FakeLlmClient
) -> RelationshipExtractionService:
    return RelationshipExtractionService(
        spi=spi,
        object_store=store,
        llm=llm,
        max_relationships=100,
        max_support_chars=4096,
        max_context_chars=256,
        max_input_bytes=262144,
    )


class TestRelationshipVerticalSlice:
    @pytest.mark.asyncio
    async def test_real_crawler_relationship_assertion_slice(
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

        crawl = await controller.crawl(
            crawl_request(
                fake_world_url,
                start_path="/thread/thr-hospital-creds",
                max_pages=8,
                max_requests=60,
                max_depth=2,
                timeout_seconds=180.0,
            )
        )
        assert crawl.status is CrawlStatus.COMPLETED, crawl.reason
        assert truth_leaks(crawl) == []

        ingested = []
        for index, page in enumerate(crawl.pages, start=1):
            observation = page_observation_to_content(
                page,
                crawl_request_id=crawl.request_id,
                observed_at=_obs_at(),
                observation_index=index,
            )
            ingested.append(await ingest.ingest(observation))

        semantic_llm = FakeLlmClient(default_response=_semantic_response())
        semantic = SemanticExtractionService(
            spi=spi,
            object_store=store,
            llm=semantic_llm,
            max_entities=100,
            max_input_bytes=262144,
        )

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

        organization = next(
            entity
            for entity in extracted.entities
            if entity.entity_type is EntityType.ORGANIZATION
        )
        location = next(
            entity
            for entity in extracted.entities
            if entity.entity_type is EntityType.LOCATION
        )

        relationship_llm = FakeLlmClient(response_factory=_relationship_factory)
        relationship_service = _relationship_service(spi, store, relationship_llm)
        result = await relationship_service.extract(item.normalized_content.content_id)

        # 8) Result is the versioned relationship profile.
        assert result.result.profile_name == RELATIONSHIP_ASSERTIONS_PROFILE_NAME
        assert result.result.profile_version == RELATIONSHIP_ASSERTIONS_PROFILE_VERSION
        assert result.created_result is True

        # 6) Endpoint ids equal the persisted occurrences.
        assertion = result.relationships[0]
        assert assertion.source_entity_id == organization.entity_id
        assert assertion.target_entity_id == location.entity_id
        assert assertion.predicate is RelationshipPredicate.LOCATED_IN

        # 7) Support maps exactly to canonical text.
        span = assertion.support_span
        assert text[span.start : span.end] == assertion.support_text
        assert _HOSPITAL_MENTION in assertion.support_text
        assert _LOCATION_MENTION in assertion.support_text

        # 9) Confidence persists.
        assert assertion.extraction_confidence == 0.88

        # 4/5) Prompt receives bounded observable refs; no truth token leaks.
        assert relationship_llm.call_count == 1
        prompt = (
            relationship_llm.calls[0].system_prompt
            + relationship_llm.calls[0].user_prompt
        )
        assert "<<<ENDPOINT_CATALOG>>>" in prompt
        for token in ("actor-001", "alias-002", "zfox", "relationship-001"):
            assert token not in prompt

        # 10) Entities and geography are not mutated by relationship extraction.
        async with spi.unit_of_work() as uow:
            reloaded_org = await uow.extraction.get_entity(organization.entity_id)
            reloaded_loc = await uow.extraction.get_entity(location.entity_id)
        assert reloaded_org == organization
        assert reloaded_loc == location

        # 11) PR 11/12 results coexist.
        async with spi.unit_of_work() as uow:
            semantic_result = await uow.extraction.get_result_by_profile(
                item.normalized_content.content_id, "semantic-entities", "v1"
            )
            relationship_result = await uow.relationships.get_result_by_profile(
                item.normalized_content.content_id,
                RELATIONSHIP_ASSERTIONS_PROFILE_NAME,
                RELATIONSHIP_ASSERTIONS_PROFILE_VERSION,
            )
        assert semantic_result is not None
        assert relationship_result is not None
        assert semantic_result.result_id != relationship_result.result_id  # type: ignore[comparison-overlap]

        # 12) Replay performs no second model call.
        replay = await relationship_service.extract(item.normalized_content.content_id)
        assert replay.created_result is False
        assert replay.result.result_id == result.result.result_id
        assert relationship_llm.call_count == 1

        # 13/14) No SourceAssessment and no global graph/truth relationship.
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM source_assessment")
                assessments = cur.fetchone()
        finally:
            conn.close()
        assert assessments is not None and assessments[0] == 0
