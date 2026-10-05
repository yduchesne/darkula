# SPDX-License-Identifier: AGPL-3.0-only
"""RP12-series: real-PostgreSQL semantic + geographic persistence (PR 12)."""

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
from darkula.domain.geography import (
    GeographicResolution,
    GeographicResolutionStatus,
    GeographicResolverIdentity,
)
from darkula.domain.identifiers import (
    ContentArtifactId,
    ExtractedEntityId,
    ExtractionResultId,
    GeographicResolutionId,
    NormalizedContentId,
    ObjectKey,
)

pytestmark = pytest.mark.integration

_MOMENT = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
_SEMANTIC_MANIFEST = (ExtractorIdentity(name="semantic-llm", version="v1"),)
_DETERMINISTIC_MANIFEST = (ExtractorIdentity(name="ip", version="v1"),)
_RESOLVER = GeographicResolverIdentity(name="fake-geo", version="v1")


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


def _semantic_result(
    content_id: NormalizedContentId, *, profile_version: str = "v1"
) -> ExtractionResult:
    return ExtractionResult(
        result_id=ExtractionResultId.generate(),
        content_id=content_id,
        profile_name="semantic-entities",
        profile_version=profile_version,
        extractor_manifest=_SEMANTIC_MANIFEST,
        extracted_at=_MOMENT,
        entity_count=1,
    )


def _entity(
    *,
    result: ExtractionResult,
    entity_type: EntityType = EntityType.ORGANIZATION,
    raw: str = "Mason Creek General Hospital",
    normalized: str = "Mason Creek General Hospital",
    start: int = 0,
    end: int = 28,
    confidence: float | None = 0.9,
    extractor: ExtractorIdentity | None = None,
) -> ExtractedEntity:
    return ExtractedEntity(
        entity_id=ExtractedEntityId.generate(),
        extraction_result_id=result.result_id,
        content_id=result.content_id,
        entity_type=entity_type,
        raw_value=raw,
        normalized_value=normalized,
        source_span=SourceSpan(start=start, end=end),
        extractor=extractor or ExtractorIdentity(name="semantic-llm", version="v1"),
        extraction_confidence=confidence,
    )


class TestSemanticPersistence:
    @pytest.mark.asyncio
    async def test_rp12_1_result_and_entities_atomic(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=1)
        result = _semantic_result(observation.content_id)
        entity = _entity(result=result, raw="Acme", normalized="Acme", start=0, end=4)
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(result)
            await uow.extraction.create_entity(entity)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.extraction.get_result(result.result_id)
            entities = await uow.extraction.list_entities_for_result(result.result_id)
        assert loaded is not None
        assert entities[0].extraction_confidence == 0.9

    @pytest.mark.asyncio
    async def test_rp12_2_semantic_and_deterministic_coexist(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=2)
        semantic = _semantic_result(observation.content_id)
        deterministic = ExtractionResult(
            result_id=ExtractionResultId.generate(),
            content_id=observation.content_id,
            profile_name="deterministic-observables",
            profile_version="v1",
            extractor_manifest=_DETERMINISTIC_MANIFEST,
            extracted_at=_MOMENT,
            entity_count=1,
        )
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(semantic)
            await uow.extraction.create_result(deterministic)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            a = await uow.extraction.get_result_by_profile(
                observation.content_id, "semantic-entities", "v1"
            )
            b = await uow.extraction.get_result_by_profile(
                observation.content_id, "deterministic-observables", "v1"
            )
        assert a is not None and b is not None
        assert a.result_id != b.result_id

    @pytest.mark.asyncio
    async def test_rp12_3_duplicate_semantic_result_conflicts(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=3)
        first = _semantic_result(observation.content_id)
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(first)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.extraction.create_result(
                    _semantic_result(observation.content_id)
                )
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rp12_4_semantic_versions_coexist(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=4)
        v1 = _semantic_result(observation.content_id, profile_version="v1")
        v2 = _semantic_result(observation.content_id, profile_version="v2")
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(v1)
            await uow.extraction.create_result(v2)
            await uow.commit()
        assert v1.result_id != v2.result_id

    @pytest.mark.asyncio
    async def test_rp12_7_confidence_round_trip(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=5)
        result = _semantic_result(observation.content_id)
        entity = _entity(
            result=result,
            raw="Acme",
            normalized="Acme",
            start=0,
            end=4,
            confidence=0.25,
        )
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(result)
            await uow.extraction.create_entity(entity)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.extraction.get_entity(entity.entity_id)
        assert loaded is not None
        assert loaded.extraction_confidence == 0.25

    @pytest.mark.asyncio
    async def test_rp12_8_deterministic_rows_keep_null_confidence(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=6)
        result = ExtractionResult(
            result_id=ExtractionResultId.generate(),
            content_id=observation.content_id,
            profile_name="deterministic-observables",
            profile_version="v1",
            extractor_manifest=_DETERMINISTIC_MANIFEST,
            extracted_at=_MOMENT,
            entity_count=1,
        )
        entity = _entity(
            result=result,
            entity_type=EntityType.IP_ADDRESS,
            raw="10.0.0.1",
            normalized="10.0.0.1",
            start=0,
            end=8,
            confidence=None,
            extractor=ExtractorIdentity(name="ip", version="v1"),
        )
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(result)
            await uow.extraction.create_entity(entity)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.extraction.get_entity(entity.entity_id)
        assert loaded is not None
        assert loaded.extraction_confidence is None

    @pytest.mark.asyncio
    async def test_rp12_9_unknown_content_integrity_error(
        self, spi: DarkulaSpi
    ) -> None:
        orphan = _semantic_result(NormalizedContentId.generate())
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.extraction.create_result(orphan)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rp12_10_fault_rolls_back(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=7)
        result = _semantic_result(observation.content_id)
        entity = _entity(result=result, raw="Acme", normalized="Acme", start=0, end=4)
        with pytest.raises(RuntimeError):
            async with spi.unit_of_work() as uow:
                await uow.extraction.create_result(result)
                await uow.extraction.create_entity(entity)
                raise RuntimeError("fault")
        async with spi.unit_of_work() as uow:
            assert await uow.extraction.get_result(result.result_id) is None
            assert (
                await uow.extraction.list_entities_for_result(result.result_id)
            ) == ()

    @pytest.mark.asyncio
    async def test_rp12_20_concurrent_semantic_one_result(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=8)
        first = _semantic_result(observation.content_id)
        second = _semantic_result(observation.content_id)

        async def _attempt(result: ExtractionResult) -> str:
            try:
                async with spi.unit_of_work() as uow:
                    await uow.extraction.create_result(result)
                    await uow.commit()
                return "won"
            except ConflictError:
                return "lost"

        assert sorted(await asyncio.gather(_attempt(first), _attempt(second))) == [
            "lost",
            "won",
        ]


class TestGeographicPersistence:
    async def _seed_location_entity(
        self, spi: DarkulaSpi, *, index: int
    ) -> ExtractedEntity:
        observation = await _seed_content(spi, request_id="crawl-1", index=index)
        result = _semantic_result(observation.content_id)
        entity = _entity(
            result=result,
            entity_type=EntityType.LOCATION,
            raw="Washington",
            normalized="Washington",
            start=0,
            end=10,
        )
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(result)
            await uow.extraction.create_entity(entity)
            await uow.commit()
        return entity

    def _resolution(
        self,
        entity: ExtractedEntity,
        *,
        status: GeographicResolutionStatus = GeographicResolutionStatus.RESOLVED,
        resolver_version: str = "v1",
        canonical_name: str | None = "Washington",
        confidence: float | None = 0.8,
    ) -> GeographicResolution:
        return GeographicResolution(
            resolution_id=GeographicResolutionId.generate(),
            extracted_entity_id=entity.entity_id,
            status=status,
            resolver=GeographicResolverIdentity(
                name="fake-geo", version=resolver_version
            ),
            resolved_at=_MOMENT,
            canonical_name=canonical_name,
            country_code="US" if canonical_name else None,
            latitude=47.75 if canonical_name else None,
            longitude=-120.74 if canonical_name else None,
            confidence=confidence,
        )

    @pytest.mark.asyncio
    async def test_rp12_14_resolved_round_trip(self, spi: DarkulaSpi) -> None:
        entity = await self._seed_location_entity(spi, index=20)
        resolution = self._resolution(entity)
        async with spi.unit_of_work() as uow:
            await uow.geography.create(resolution)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.geography.get_for_entity(
                entity.entity_id, _RESOLVER.name, _RESOLVER.version
            )
        assert loaded is not None
        assert loaded.status is GeographicResolutionStatus.RESOLVED
        assert loaded.canonical_name == "Washington"
        assert loaded.confidence == 0.8
        assert loaded.latitude == 47.75

    @pytest.mark.asyncio
    async def test_rp12_15_ambiguous_no_canonical_lie(self, spi: DarkulaSpi) -> None:
        entity = await self._seed_location_entity(spi, index=21)
        resolution = self._resolution(
            entity,
            status=GeographicResolutionStatus.AMBIGUOUS,
            canonical_name=None,
            confidence=0.45,
        )
        async with spi.unit_of_work() as uow:
            await uow.geography.create(resolution)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.geography.get(resolution.resolution_id)
        assert loaded is not None
        assert loaded.canonical_name is None
        assert loaded.latitude is None

    @pytest.mark.asyncio
    async def test_rp12_16_unresolved_null_fields(self, spi: DarkulaSpi) -> None:
        entity = await self._seed_location_entity(spi, index=22)
        resolution = self._resolution(
            entity,
            status=GeographicResolutionStatus.UNRESOLVED,
            canonical_name=None,
            confidence=None,
        )
        async with spi.unit_of_work() as uow:
            await uow.geography.create(resolution)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.geography.get(resolution.resolution_id)
        assert loaded is not None
        assert loaded.canonical_name is None
        assert loaded.country_code is None

    @pytest.mark.asyncio
    async def test_rp12_17_duplicate_resolution_conflicts(
        self, spi: DarkulaSpi
    ) -> None:
        entity = await self._seed_location_entity(spi, index=23)
        first = self._resolution(entity)
        async with spi.unit_of_work() as uow:
            await uow.geography.create(first)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.geography.create(self._resolution(entity))
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rp12_18_new_resolver_version_coexists(self, spi: DarkulaSpi) -> None:
        entity = await self._seed_location_entity(spi, index=24)
        v1 = self._resolution(entity, resolver_version="v1")
        v2 = self._resolution(entity, resolver_version="v2")
        async with spi.unit_of_work() as uow:
            await uow.geography.create(v1)
            await uow.geography.create(v2)
            await uow.commit()
        assert v1.resolution_id != v2.resolution_id

    @pytest.mark.asyncio
    async def test_rp12_19_unknown_entity_integrity_error(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=25)
        orphan = _entity(
            result=_semantic_result(observation.content_id),
            entity_type=EntityType.LOCATION,
        )
        resolution = self._resolution(orphan)
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.geography.create(resolution)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rp12_21_concurrent_resolution_one_row(self, spi: DarkulaSpi) -> None:
        entity = await self._seed_location_entity(spi, index=26)
        first = self._resolution(entity)
        second = self._resolution(entity)

        async def _attempt(resolution: GeographicResolution) -> str:
            try:
                async with spi.unit_of_work() as uow:
                    await uow.geography.create(resolution)
                    await uow.commit()
                return "won"
            except ConflictError:
                return "lost"

        assert sorted(await asyncio.gather(_attempt(first), _attempt(second))) == [
            "lost",
            "won",
        ]
