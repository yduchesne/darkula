# SPDX-License-Identifier: AGPL-3.0-only
"""RDB-series: real-PostgreSQL relationship persistence (PR 13)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from darkula.app.object_store import ContentHash, ContentType
from darkula.app.persistence import ConflictError, DarkulaSpi, IntegrityError
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.extraction import (
    EntityType,
    ExtractedEntity,
    ExtractionResult,
    ExtractorIdentity,
    SourceSpan,
)
from darkula.domain.identifiers import (
    ContentArtifactId,
    ExtractedEntityId,
    ExtractedRelationshipId,
    ExtractionResultId,
    NormalizedContentId,
    ObjectKey,
    RelationshipExtractionResultId,
)
from darkula.domain.relationships import (
    RELATIONSHIP_ASSERTIONS_PROFILE_NAME,
    RELATIONSHIP_ASSERTIONS_PROFILE_VERSION,
    RELATIONSHIP_LLM_EXTRACTOR_NAME,
    RELATIONSHIP_LLM_EXTRACTOR_VERSION,
    ExtractedRelationship,
    RelationshipExtractionResult,
    RelationshipPredicate,
)

pytestmark = pytest.mark.integration

_MOMENT = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
_SEMANTIC_MANIFEST = (ExtractorIdentity(name="semantic-llm", version="v1"),)
_EXTRACTOR = ExtractorIdentity(
    name=RELATIONSHIP_LLM_EXTRACTOR_NAME, version=RELATIONSHIP_LLM_EXTRACTOR_VERSION
)


async def _seed_content(
    spi: DarkulaSpi, *, request_id: str, index: int
) -> NormalizedContent:
    digest = f"{index:064x}"
    artifact = ContentArtifact(
        artifact_id=ContentArtifactId.generate(),
        object_key=ObjectKey(f"sha256/{digest[:2]}/{digest}"),
        content_type=ContentType("text/plain"),
        size_bytes=16,
        content_hash=ContentHash(algorithm="sha-256", digest_hex=digest),
        artifact_kind=ArtifactKind.NORMALIZED_TEXT,
        completeness=ArtifactCompleteness.COMPLETE,
        created_at=_MOMENT,
    )
    observation = NormalizedContent(
        content_id=NormalizedContentId.generate(),
        artifact_id=artifact.artifact_id,
        source_uri=f"http://blackgate.example.test/thread/{index}",
        observed_at=_MOMENT,
        crawl_request_id=request_id,
        observation_index=index,
        normalization_version="text-v1",
        completeness=ArtifactCompleteness.SAMPLE,
        title="Thread",
        text="bounded preview",
        content_type=ContentType("text/plain"),
        content_hash=artifact.content_hash,
    )
    async with spi.unit_of_work() as uow:
        await uow.content.create_artifact(artifact)
        await uow.content.create_observation(observation)
        await uow.commit()
    return observation


async def _seed_entities(
    spi: DarkulaSpi, content_id: NormalizedContentId
) -> tuple[ExtractedEntity, ExtractedEntity]:
    result = ExtractionResult(
        result_id=ExtractionResultId.generate(),
        content_id=content_id,
        profile_name="semantic-entities",
        profile_version="v1",
        extractor_manifest=_SEMANTIC_MANIFEST,
        extracted_at=_MOMENT,
        entity_count=2,
    )
    source = ExtractedEntity(
        entity_id=ExtractedEntityId.generate(),
        extraction_result_id=result.result_id,
        content_id=content_id,
        entity_type=EntityType.THREAT_ACTOR,
        raw_value="NightRaven",
        normalized_value="NightRaven",
        source_span=SourceSpan(start=0, end=10),
        extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
        extraction_confidence=0.9,
    )
    target = ExtractedEntity(
        entity_id=ExtractedEntityId.generate(),
        extraction_result_id=result.result_id,
        content_id=content_id,
        entity_type=EntityType.MALWARE,
        raw_value="BlackFang",
        normalized_value="BlackFang",
        source_span=SourceSpan(start=15, end=24),
        extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
        extraction_confidence=0.8,
    )
    async with spi.unit_of_work() as uow:
        await uow.extraction.create_result(result)
        await uow.extraction.create_entity(source)
        await uow.extraction.create_entity(target)
        await uow.commit()
    return source, target


def _result(
    content_id: NormalizedContentId, *, profile_version: str = "v1"
) -> RelationshipExtractionResult:
    return RelationshipExtractionResult(
        result_id=RelationshipExtractionResultId.generate(),
        content_id=content_id,
        profile_name=RELATIONSHIP_ASSERTIONS_PROFILE_NAME,
        profile_version=profile_version,
        extractor_manifest=(_EXTRACTOR,),
        extracted_at=_MOMENT,
        relationship_count=0,
    )


def _assertion(
    result: RelationshipExtractionResult,
    source: ExtractedEntity,
    target: ExtractedEntity,
    *,
    predicate: RelationshipPredicate = RelationshipPredicate.USES,
    support_text: str = "support",
    start: int = 0,
    confidence: float = 0.9,
    content_id: NormalizedContentId | None = None,
) -> ExtractedRelationship:
    return ExtractedRelationship(
        relationship_id=ExtractedRelationshipId.generate(),
        extraction_result_id=result.result_id,
        content_id=content_id or result.content_id,
        source_entity_id=source.entity_id,
        predicate=predicate,
        target_entity_id=target.entity_id,
        support_span=SourceSpan(start=start, end=start + len(support_text)),
        support_text=support_text,
        extractor=_EXTRACTOR,
        extraction_confidence=confidence,
    )


class TestRelationshipResultPersistence:
    @pytest.mark.asyncio
    async def test_rdb1_result_round_trip(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=1)
        result = _result(observation.content_id)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.relationships.get_result(result.result_id)
            by_profile = await uow.relationships.get_result_by_profile(
                observation.content_id,
                RELATIONSHIP_ASSERTIONS_PROFILE_NAME,
                RELATIONSHIP_ASSERTIONS_PROFILE_VERSION,
            )
        assert loaded == result
        assert by_profile == result

    @pytest.mark.asyncio
    async def test_rdb2_duplicate_semantic_key_conflicts(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=2)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(_result(observation.content_id))
            await uow.commit()
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.relationships.create_result(_result(observation.content_id))
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rdb3_profile_versions_coexist(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=3)
        v1 = _result(observation.content_id, profile_version="v1")
        v2 = _result(observation.content_id, profile_version="v2")
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(v1)
            await uow.relationships.create_result(v2)
            await uow.commit()
        assert v1.result_id != v2.result_id

    @pytest.mark.asyncio
    async def test_rdb20_zero_count_result_valid(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=4)
        result = _result(observation.content_id)
        assert result.relationship_count == 0
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            relationships = await uow.relationships.list_for_result(result.result_id)
        assert relationships == ()

    @pytest.mark.asyncio
    async def test_rdb5_unknown_content_integrity_error(self, spi: DarkulaSpi) -> None:
        result = _result(NormalizedContentId.generate())
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.relationships.create_result(result)
            await uow.rollback()


class TestRelationshipAssertionPersistence:
    @pytest.mark.asyncio
    async def test_rdb4_assertion_round_trip(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=10)
        source, target = await _seed_entities(spi, observation.content_id)
        result = _result(observation.content_id)
        assertion = _assertion(result, source, target, confidence=0.77)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            await uow.relationships.create_relationship(assertion)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.relationships.get_relationship(assertion.relationship_id)
            listed = await uow.relationships.list_for_result(result.result_id)
        assert loaded == assertion
        assert listed == (assertion,)

    @pytest.mark.asyncio
    async def test_rdb6_unknown_source_integrity_error(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=11)
        _, target = await _seed_entities(spi, observation.content_id)
        result = _result(observation.content_id)
        missing = ExtractedEntity(
            entity_id=ExtractedEntityId.generate(),
            extraction_result_id=ExtractionResultId.generate(),
            content_id=observation.content_id,
            entity_type=EntityType.THREAT_ACTOR,
            raw_value="Ghost",
            normalized_value="Ghost",
            source_span=SourceSpan(start=0, end=5),
            extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
            extraction_confidence=0.9,
        )
        assertion = _assertion(result, missing, target)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            with pytest.raises(IntegrityError):
                await uow.relationships.create_relationship(assertion)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rdb7_unknown_target_integrity_error(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=12)
        source, _ = await _seed_entities(spi, observation.content_id)
        result = _result(observation.content_id)
        missing = ExtractedEntity(
            entity_id=ExtractedEntityId.generate(),
            extraction_result_id=ExtractionResultId.generate(),
            content_id=observation.content_id,
            entity_type=EntityType.MALWARE,
            raw_value="Ghost",
            normalized_value="Ghost",
            source_span=SourceSpan(start=0, end=5),
            extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
            extraction_confidence=0.9,
        )
        assertion = _assertion(result, source, missing)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            with pytest.raises(IntegrityError):
                await uow.relationships.create_relationship(assertion)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rdb8_source_wrong_content_integrity_error(
        self, spi: DarkulaSpi
    ) -> None:
        first = await _seed_content(spi, request_id="crawl-a", index=13)
        second = await _seed_content(spi, request_id="crawl-b", index=14)
        source, target = await _seed_entities(spi, first.content_id)
        foreign_result = _result(second.content_id)
        assertion = _assertion(foreign_result, source, target)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(foreign_result)
            with pytest.raises(IntegrityError):
                await uow.relationships.create_relationship(assertion)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rdb9_target_wrong_content_integrity_error(
        self, spi: DarkulaSpi
    ) -> None:
        first = await _seed_content(spi, request_id="crawl-a", index=15)
        second = await _seed_content(spi, request_id="crawl-b", index=16)
        source, target = await _seed_entities(spi, first.content_id)
        foreign_result = _result(second.content_id)
        assertion = _assertion(foreign_result, source, target)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(foreign_result)
            with pytest.raises(IntegrityError):
                await uow.relationships.create_relationship(assertion)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rdb10_result_content_mismatch(self, spi: DarkulaSpi) -> None:
        first = await _seed_content(spi, request_id="crawl-a", index=17)
        second = await _seed_content(spi, request_id="crawl-b", index=18)
        source, target = await _seed_entities(spi, first.content_id)
        result = _result(second.content_id)
        # Explicitly wrong relationship content (not the result content).
        assertion = _assertion(result, source, target, content_id=first.content_id)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            with pytest.raises(IntegrityError):
                await uow.relationships.create_relationship(assertion)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rdb11_exact_duplicate_conflicts(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=19)
        source, target = await _seed_entities(spi, observation.content_id)
        result = _result(observation.content_id)
        assertion = _assertion(result, source, target)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            await uow.relationships.create_relationship(assertion)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            duplicate = _assertion(result, source, target)
            with pytest.raises(ConflictError):
                await uow.relationships.create_relationship(duplicate)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rdb12_different_support_spans_coexist(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=20)
        source, target = await _seed_entities(spi, observation.content_id)
        result = _result(observation.content_id)
        first = _assertion(result, source, target, start=0)
        second = _assertion(result, source, target, start=3)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            await uow.relationships.create_relationship(first)
            await uow.relationships.create_relationship(second)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            listed = await uow.relationships.list_for_result(result.result_id)
        assert len(listed) == 2

    @pytest.mark.asyncio
    async def test_rdb13_same_semantics_different_occurrences_distinct(
        self, spi: DarkulaSpi
    ) -> None:
        first = await _seed_content(spi, request_id="crawl-a", index=21)
        second = await _seed_content(spi, request_id="crawl-b", index=22)
        source_a, target_a = await _seed_entities(spi, first.content_id)
        source_b, target_b = await _seed_entities(spi, second.content_id)
        result_a = _result(first.content_id)
        result_b = _result(second.content_id)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result_a)
            await uow.relationships.create_result(result_b)
            await uow.relationships.create_relationship(
                _assertion(result_a, source_a, target_a)
            )
            await uow.relationships.create_relationship(
                _assertion(result_b, source_b, target_b)
            )
            await uow.commit()
        assert source_a.entity_id != source_b.entity_id
        assert result_a.result_id != result_b.result_id

    @pytest.mark.asyncio
    async def test_rdb14_deterministic_list_by_result(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=23)
        source, target = await _seed_entities(spi, observation.content_id)
        result = _result(observation.content_id)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            await uow.relationships.create_relationship(
                _assertion(result, source, target, start=5)
            )
            await uow.relationships.create_relationship(
                _assertion(result, source, target, start=1)
            )
            await uow.commit()
        async with spi.unit_of_work() as uow:
            listed = await uow.relationships.list_for_result(result.result_id)
        assert [item.support_span.start for item in listed] == [1, 5]

    @pytest.mark.asyncio
    async def test_rdb15_deterministic_list_by_content(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=24)
        source, target = await _seed_entities(spi, observation.content_id)
        result = _result(observation.content_id)
        assertion = _assertion(result, source, target)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            await uow.relationships.create_relationship(assertion)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            listed = await uow.relationships.list_for_content(observation.content_id)
        assert listed == (assertion,)

    @pytest.mark.asyncio
    async def test_rdb16_rollback_discards_partial_write(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=25)
        source, target = await _seed_entities(spi, observation.content_id)
        result = _result(observation.content_id)
        assertion = _assertion(result, source, target)
        async with spi.unit_of_work() as uow:
            await uow.relationships.create_result(result)
            await uow.relationships.create_relationship(assertion)
            await uow.rollback()
        async with spi.unit_of_work() as uow:
            assert await uow.relationships.get_result(result.result_id) is None
            assert (
                await uow.relationships.get_relationship(assertion.relationship_id)
                is None
            )

    @pytest.mark.asyncio
    async def test_rdb17_concurrent_one_winner(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=26)
        results = [_result(observation.content_id), _result(observation.content_id)]

        async def _create(result: RelationshipExtractionResult) -> str:
            async with spi.unit_of_work() as uow:
                try:
                    await uow.relationships.create_result(result)
                except ConflictError:
                    await uow.rollback()
                    return "conflict"
                await uow.commit()
                return "created"

        outcomes = await asyncio.gather(*(_create(item) for item in results))
        assert sorted(outcomes) == ["conflict", "created"]
