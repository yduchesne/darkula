# SPDX-License-Identifier: AGPL-3.0-only
"""RP-series: real-PostgreSQL deterministic extraction persistence (PR 11).

Exercises ``PostgresExtractionRepository -> stored function -> PostgreSQL``
for immutable/versioned results and provenance-bearing occurrences, including
semantic idempotency, occurrence uniqueness, atomicity, and a real
concurrent semantic-key race.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from tests.integration.conftest import _raw_connect

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
    ExtractionResultId,
    NormalizedContentId,
    ObjectKey,
)

pytestmark = pytest.mark.integration

_MOMENT = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
_PROFILE = "deterministic-observables"
_VERSION = "v1"
_MANIFEST = (
    ExtractorIdentity(name="domain", version="v1"),
    ExtractorIdentity(name="email", version="v1"),
    ExtractorIdentity(name="hash", version="v1"),
    ExtractorIdentity(name="ip", version="v1"),
    ExtractorIdentity(name="url", version="v1"),
)


async def _seed_content(
    spi: DarkulaSpi, *, request_id: str, index: int
) -> NormalizedContent:
    """Persist one artifact + observation through the real repository."""
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
        title=f"Thread {index}",
        text="bounded preview",
        content_type=ContentType("text/plain"),
        content_hash=artifact.content_hash,
    )
    async with spi.unit_of_work() as uow:
        await uow.content.create_artifact(artifact)
        await uow.content.create_observation(observation)
        await uow.commit()
    return observation


def _result(
    content_id: NormalizedContentId,
    *,
    profile_version: str = _VERSION,
) -> ExtractionResult:
    return ExtractionResult(
        result_id=ExtractionResultId.generate(),
        content_id=content_id,
        profile_name=_PROFILE,
        profile_version=profile_version,
        extractor_manifest=_MANIFEST,
        extracted_at=_MOMENT,
        entity_count=1,
    )


def _entity(
    *,
    result: ExtractionResult,
    raw: str,
    normalized: str,
    start: int,
    end: int,
    entity_type: EntityType = EntityType.DOMAIN,
) -> ExtractedEntity:
    return ExtractedEntity(
        entity_id=ExtractedEntityId.generate(),
        extraction_result_id=result.result_id,
        content_id=result.content_id,
        entity_type=entity_type,
        raw_value=raw,
        normalized_value=normalized,
        source_span=SourceSpan(start=start, end=end),
        extractor=ExtractorIdentity(name="domain", version="v1"),
    )


class TestResultPersistence:
    @pytest.mark.asyncio
    async def test_rp1_result_and_entities_commit_atomically(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=1)
        result = _result(observation.content_id)
        entity = _entity(
            result=result, raw="example.com", normalized="example.com", start=0, end=11
        )
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(result)
            await uow.extraction.create_entity(entity)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.extraction.get_result(result.result_id)
            entities = await uow.extraction.list_entities_for_result(result.result_id)
        assert loaded is not None
        assert loaded.extractor_manifest == _MANIFEST
        assert [e.entity_id for e in entities] == [entity.entity_id]

    @pytest.mark.asyncio
    async def test_rp2_same_content_profile_one_result(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=2)
        first = _result(observation.content_id)
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(first)
            await uow.commit()
        second = _result(observation.content_id)
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.extraction.create_result(second)
            await uow.rollback()
        async with spi.unit_of_work() as uow:
            found = await uow.extraction.get_result_by_profile(
                observation.content_id, _PROFILE, _VERSION
            )
        assert found is not None
        assert found.result_id == first.result_id

    @pytest.mark.asyncio
    async def test_rp3_new_profile_version_coexists(self, spi: DarkulaSpi) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=3)
        v1 = _result(observation.content_id, profile_version="v1")
        v2 = _result(observation.content_id, profile_version="v2")
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(v1)
            await uow.extraction.create_result(v2)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            a = await uow.extraction.get_result_by_profile(
                observation.content_id, _PROFILE, "v1"
            )
            b = await uow.extraction.get_result_by_profile(
                observation.content_id, _PROFILE, "v2"
            )
        assert a is not None and b is not None
        assert a.result_id != b.result_id

    @pytest.mark.asyncio
    async def test_rp4_identical_bytes_two_observations_distinct_provenance(
        self, spi: DarkulaSpi
    ) -> None:
        # Same digest/representation, two distinct normalized observations.
        digest = "a" * 64
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
        observations = []
        for index in (10, 11):
            observations.append(
                NormalizedContent(
                    content_id=NormalizedContentId.generate(),
                    artifact_id=artifact.artifact_id,
                    source_uri=f"http://blackgate.example.test/thread/{index}",
                    observed_at=_MOMENT,
                    crawl_request_id="crawl-1",
                    observation_index=index,
                    normalization_version="text-v1",
                    completeness=ArtifactCompleteness.SAMPLE,
                    title="Thread",
                    text="identical bytes",
                    content_type=ContentType("text/plain"),
                    content_hash=artifact.content_hash,
                )
            )
        async with spi.unit_of_work() as uow:
            await uow.content.create_artifact(artifact)
            for observation in observations:
                await uow.content.create_observation(observation)
            await uow.commit()
        results = [_result(o.content_id) for o in observations]
        async with spi.unit_of_work() as uow:
            for result in results:
                await uow.extraction.create_result(result)
            await uow.commit()
        assert results[0].result_id != results[1].result_id
        async with spi.unit_of_work() as uow:
            assert (await uow.extraction.get_result(results[0].result_id)) is not None
            assert (await uow.extraction.get_result(results[1].result_id)) is not None


class TestEntityOccurrence:
    @pytest.mark.asyncio
    async def test_rp5_exact_duplicate_occurrence_conflicts(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=4)
        result = _result(observation.content_id)
        entity = _entity(
            result=result, raw="example.com", normalized="example.com", start=0, end=11
        )
        duplicate = ExtractedEntity(
            entity_id=ExtractedEntityId.generate(),
            extraction_result_id=result.result_id,
            content_id=result.content_id,
            entity_type=EntityType.DOMAIN,
            raw_value="example.com",
            normalized_value="example.com",
            source_span=SourceSpan(start=0, end=11),
            extractor=ExtractorIdentity(name="domain", version="v1"),
        )
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(result)
            await uow.extraction.create_entity(entity)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.extraction.create_entity(duplicate)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rp6_same_value_two_spans_persists_twice(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=5)
        result = _result(observation.content_id)
        first = _entity(
            result=result, raw="example.com", normalized="example.com", start=0, end=11
        )
        second = _entity(
            result=result, raw="example.com", normalized="example.com", start=20, end=31
        )
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(result)
            await uow.extraction.create_entity(first)
            await uow.extraction.create_entity(second)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            entities = await uow.extraction.list_entities_for_result(result.result_id)
        assert len(entities) == 2
        assert {e.source_span.start for e in entities} == {0, 20}


class TestAtomicityAndErrors:
    @pytest.mark.asyncio
    async def test_rp7_fault_before_commit_rolls_back_everything(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=6)
        result = _result(observation.content_id)
        entity = _entity(
            result=result, raw="example.com", normalized="example.com", start=0, end=11
        )
        sentinel = RuntimeError("fault after result creation")
        with pytest.raises(RuntimeError):
            async with spi.unit_of_work() as uow:
                await uow.extraction.create_result(result)
                await uow.extraction.create_entity(entity)
                raise sentinel
        async with spi.unit_of_work() as uow:
            loaded = await uow.extraction.get_result(result.result_id)
            entities = await uow.extraction.list_entities_for_result(result.result_id)
        assert loaded is None
        assert entities == ()

    @pytest.mark.asyncio
    async def test_rp8_unknown_result_maps_to_integrity_error(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=7)
        orphan = ExtractedEntity(
            entity_id=ExtractedEntityId.generate(),
            extraction_result_id=ExtractionResultId.generate(),
            content_id=observation.content_id,
            entity_type=EntityType.DOMAIN,
            raw_value="example.com",
            normalized_value="example.com",
            source_span=SourceSpan(start=0, end=11),
            extractor=ExtractorIdentity(name="domain", version="v1"),
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.extraction.create_entity(orphan)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_rp8_unknown_content_maps_to_integrity_error(
        self, spi: DarkulaSpi
    ) -> None:
        orphan = _result(NormalizedContentId.generate())
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.extraction.create_result(orphan)
            await uow.rollback()


class TestOrdering:
    @pytest.mark.asyncio
    async def test_rp9_listing_order_independent_of_insert_order(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=8)
        result = _result(observation.content_id)
        late = _entity(result=result, raw="b.com", normalized="b.com", start=30, end=35)
        early = _entity(result=result, raw="a.com", normalized="a.com", start=5, end=10)
        async with spi.unit_of_work() as uow:
            await uow.extraction.create_result(result)
            await uow.extraction.create_entity(late)
            await uow.extraction.create_entity(early)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            entities = await uow.extraction.list_entities_for_result(result.result_id)
        assert [e.source_span.start for e in entities] == [5, 30]


class TestConcurrentRace:
    @pytest.mark.asyncio
    async def test_rp12_concurrent_semantic_key_yields_one_result(
        self, spi: DarkulaSpi
    ) -> None:
        observation = await _seed_content(spi, request_id="crawl-1", index=9)
        first = _result(observation.content_id)
        second = _result(observation.content_id)

        async def _attempt(result: ExtractionResult) -> str:
            try:
                async with spi.unit_of_work() as uow:
                    await uow.extraction.create_result(result)
                    await uow.commit()
                return "won"
            except ConflictError:
                return "lost"

        outcomes = await asyncio.gather(_attempt(first), _attempt(second))
        assert sorted(outcomes) == ["lost", "won"]
        async with spi.unit_of_work() as uow:
            winner = await uow.extraction.get_result_by_profile(
                observation.content_id, _PROFILE, _VERSION
            )
        assert winner is not None


class TestMigrationContinuity:
    def test_rp11_historical_migrations_and_new_head_recorded(
        self, database_settings: object
    ) -> None:
        from darkula.config.settings import DatabaseSettings

        assert isinstance(database_settings, DatabaseSettings)
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM darkula_schema_migrations")
                ledger = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()
        assert {
            "0001_initial.sql",
            "0002_message_outbox.sql",
            "0003_content.sql",
            "0004_collection.sql",
            "0005_extraction.sql",
        } <= ledger
