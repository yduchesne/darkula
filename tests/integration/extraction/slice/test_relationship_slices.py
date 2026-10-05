# SPDX-License-Identifier: AGPL-3.0-only
"""PR 13 service slices: real PostgreSQL + ObjectStore + real service.

Only the external model boundary (``FakeLlmClient``) is faked; the
relationship service, grounding, repositories, stored functions, and
ObjectStore are real.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from darkula.app.llm import LlmError, LlmErrorCode
from darkula.app.object_store import ContentHash, ContentType, ObjectStore
from darkula.app.persistence import DarkulaSpi
from darkula.app.relationship_extraction import (
    RelationshipCandidate,
    RelationshipContentNotFoundError,
    RelationshipExtractionResponse,
    RelationshipExtractionService,
    RelationshipInputUnavailableError,
    RelationshipIntegrityError,
    RelationshipModelError,
)
from darkula.config.settings import DatabaseSettings
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
from darkula.domain.relationships import (
    RELATIONSHIP_ASSERTIONS_PROFILE_NAME,
    RELATIONSHIP_ASSERTIONS_PROFILE_VERSION,
    RelationshipPredicate,
)
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_llm import FakeLlmClient

pytestmark = pytest.mark.integration

_MOMENT = datetime(2026, 5, 1, tzinfo=UTC)
_TEXT_PLAIN = ContentType("text/plain")
_TEXT = "NightRaven uses BlackFang against hospitals."
_SOURCE_VALUE = "NightRaven"
_TARGET_VALUE = "BlackFang"
_SUPPORT = "NightRaven uses BlackFang"
_SEMANTIC_MANIFEST = (ExtractorIdentity(name="semantic-llm", version="v1"),)


async def _iter_bytes(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _seed(
    spi: DarkulaSpi,
    store: ObjectStore,
    text: str,
    *,
    request_id: str,
    index: int,
) -> NormalizedContent:
    payload = text.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    key = ObjectKey(f"sha256/{digest[:2]}/{digest}")
    artifact = ContentArtifact(
        artifact_id=ContentArtifactId.generate(),
        object_key=key,
        content_type=_TEXT_PLAIN,
        size_bytes=len(payload),
        content_hash=ContentHash(algorithm="sha-256", digest_hex=digest),
        artifact_kind=ArtifactKind.NORMALIZED_TEXT,
        completeness=ArtifactCompleteness.COMPLETE,
        created_at=_MOMENT,
    )
    representation_hash = artifact.content_hash
    assert representation_hash is not None
    async with spi.unit_of_work() as uow:
        existing = await uow.content.find_artifact_by_representation(
            content_hash=representation_hash,
            kind=artifact.artifact_kind,
            completeness=artifact.completeness,
        )
        if existing is not None:
            artifact = existing
    if existing is None:
        await store.put(key=key, data=_iter_bytes(payload), content_type=_TEXT_PLAIN)
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
        content_type=_TEXT_PLAIN,
        content_hash=artifact.content_hash,
    )
    async with spi.unit_of_work() as uow:
        if existing is None:
            await uow.content.create_artifact(artifact)
        await uow.content.create_observation(observation)
        await uow.commit()
    return observation


async def _seed_entities(
    spi: DarkulaSpi, content_id: NormalizedContentId, text: str
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
        raw_value=_SOURCE_VALUE,
        normalized_value=_SOURCE_VALUE,
        source_span=SourceSpan(
            start=text.index(_SOURCE_VALUE),
            end=text.index(_SOURCE_VALUE) + len(_SOURCE_VALUE),
        ),
        extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
        extraction_confidence=0.9,
    )
    target = ExtractedEntity(
        entity_id=ExtractedEntityId.generate(),
        extraction_result_id=result.result_id,
        content_id=content_id,
        entity_type=EntityType.MALWARE,
        raw_value=_TARGET_VALUE,
        normalized_value=_TARGET_VALUE,
        source_span=SourceSpan(
            start=text.index(_TARGET_VALUE),
            end=text.index(_TARGET_VALUE) + len(_TARGET_VALUE),
        ),
        extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
        extraction_confidence=0.8,
    )
    async with spi.unit_of_work() as uow:
        await uow.extraction.create_result(result)
        await uow.extraction.create_entity(source)
        await uow.extraction.create_entity(target)
        await uow.commit()
    return source, target


def _response(
    source_ref: str = "E1", target_ref: str = "E2"
) -> RelationshipExtractionResponse:
    return RelationshipExtractionResponse(
        relationships=[
            RelationshipCandidate(
                source_ref=source_ref,
                predicate=RelationshipPredicate.USES,
                target_ref=target_ref,
                support_text=_SUPPORT,
                confidence=0.85,
            )
        ]
    )


def _service(
    spi: DarkulaSpi,
    store: ObjectStore,
    llm: FakeLlmClient,
    *,
    max_input_bytes: int = 262144,
) -> RelationshipExtractionService:
    return RelationshipExtractionService(
        spi=spi,
        object_store=store,
        llm=llm,
        max_relationships=100,
        max_support_chars=4096,
        max_context_chars=256,
        max_input_bytes=max_input_bytes,
    )


class TestRelationshipServiceSlice:
    @pytest.mark.asyncio
    async def test_service_persists_exact_provenance(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(spi, store, _TEXT, request_id="crawl-rel-1", index=1)
        source, target = await _seed_entities(spi, observation.content_id, _TEXT)
        llm = FakeLlmClient(default_response=_response())
        result = await _service(spi, store, llm).extract(observation.content_id)

        assert result.created_result is True
        assert result.result.profile_name == RELATIONSHIP_ASSERTIONS_PROFILE_NAME
        assert result.result.profile_version == RELATIONSHIP_ASSERTIONS_PROFILE_VERSION
        assertion = result.relationships[0]
        assert assertion.source_entity_id == source.entity_id
        assert assertion.target_entity_id == target.entity_id
        assert assertion.support_text == _SUPPORT
        assert (
            _TEXT[assertion.support_span.start : assertion.support_span.end] == _SUPPORT
        )
        assert assertion.extraction_confidence == 0.85

        # Replay reuses the durable result without another model call.
        replay = await _service(spi, store, llm).extract(observation.content_id)
        assert replay.created_result is False
        assert replay.result.result_id == result.result.result_id
        assert llm.call_count == 1

    @pytest.mark.asyncio
    async def test_unknown_refs_persist_durable_zero(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(spi, store, _TEXT, request_id="crawl-rel-2", index=2)
        await _seed_entities(spi, observation.content_id, _TEXT)
        llm = FakeLlmClient(default_response=_response(source_ref="E9"))
        result = await _service(spi, store, llm).extract(observation.content_id)
        assert result.result.relationship_count == 0
        assert result.rejected_candidates == 1

    @pytest.mark.asyncio
    async def test_missing_object_no_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(spi, store, _TEXT, request_id="crawl-rel-3", index=3)
        await _seed_entities(spi, observation.content_id, _TEXT)
        service = _service(
            spi, LocalFileObjectStore(tmp_path / "other"), FakeLlmClient()
        )
        with pytest.raises(RelationshipInputUnavailableError):
            await service.extract(observation.content_id)

    @pytest.mark.asyncio
    async def test_hash_mismatch_no_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(spi, store, _TEXT, request_id="crawl-rel-4", index=4)
        await _seed_entities(spi, observation.content_id, _TEXT)
        digest = hashlib.sha256(_TEXT.encode("utf-8")).hexdigest()
        key = ObjectKey(f"sha256/{digest[:2]}/{digest}")
        await store.put(key=key, data=_iter_bytes(b"tampered"))
        with pytest.raises(RelationshipIntegrityError):
            await _service(spi, store, FakeLlmClient()).extract(observation.content_id)

    @pytest.mark.asyncio
    async def test_provider_failure_no_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(spi, store, _TEXT, request_id="crawl-rel-5", index=5)
        await _seed_entities(spi, observation.content_id, _TEXT)
        llm = FakeLlmClient()
        llm.enqueue(LlmError(LlmErrorCode.PROVIDER_FAILURE))
        with pytest.raises(RelationshipModelError):
            await _service(spi, store, llm).extract(observation.content_id)

    @pytest.mark.asyncio
    async def test_cancellation_no_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(spi, store, _TEXT, request_id="crawl-rel-6", index=6)
        await _seed_entities(spi, observation.content_id, _TEXT)
        llm = FakeLlmClient()
        llm.enqueue(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _service(spi, store, llm).extract(observation.content_id)
        async with spi.unit_of_work() as uow:
            assert (
                await uow.relationships.get_result_by_profile(
                    observation.content_id,
                    RELATIONSHIP_ASSERTIONS_PROFILE_NAME,
                    RELATIONSHIP_ASSERTIONS_PROFILE_VERSION,
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_missing_content_not_found(
        self, spi: PostgresDarkulaSpi, tmp_path: Path
    ) -> None:
        service = _service(
            spi, LocalFileObjectStore(tmp_path / "objects"), FakeLlmClient()
        )
        with pytest.raises(RelationshipContentNotFoundError):
            await service.extract(NormalizedContentId.generate())


class TestRelationshipConcurrencySlice:
    @pytest.mark.asyncio
    async def test_concurrent_same_profile_one_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(
            spi, store, _TEXT, request_id="crawl-rel-race", index=7
        )
        await _seed_entities(spi, observation.content_id, _TEXT)
        llm = FakeLlmClient(default_response=_response())
        service = _service(spi, store, llm)
        first, second = await asyncio.gather(
            service.extract(observation.content_id),
            service.extract(observation.content_id),
        )
        assert first.result.result_id == second.result.result_id
        assert {first.created_result, second.created_result} == {True, False}


class TestCrossContentProvenanceSlice:
    @pytest.mark.asyncio
    async def test_same_semantics_two_contents_remain_distinct(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        store = LocalFileObjectStore(tmp_path / "objects")
        first = await _seed(spi, store, _TEXT, request_id="crawl-rel-a", index=8)
        second = await _seed(spi, store, _TEXT, request_id="crawl-rel-b", index=9)
        source_a, target_a = await _seed_entities(spi, first.content_id, _TEXT)
        source_b, target_b = await _seed_entities(spi, second.content_id, _TEXT)
        service = _service(spi, store, FakeLlmClient(default_response=_response()))
        result_a = await service.extract(first.content_id)
        result_b = await service.extract(second.content_id)

        # Distinct content, result, assertion, and endpoint occurrence ids:
        # identical bytes/semantics never merge provenance.
        assert first.content_id != second.content_id
        assert result_a.result.result_id != result_b.result.result_id
        assertion_a = result_a.relationships[0]
        assertion_b = result_b.relationships[0]
        assert assertion_a.relationship_id != assertion_b.relationship_id
        assert assertion_a.source_entity_id == source_a.entity_id
        assert assertion_b.source_entity_id == source_b.entity_id
        assert source_a.entity_id != source_b.entity_id
        assert target_a.entity_id != target_b.entity_id

        async with spi.unit_of_work() as uow:
            listed = await uow.relationships.list_for_content(first.content_id)
        assert len(listed) == 1
