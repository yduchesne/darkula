# SPDX-License-Identifier: AGPL-3.0-only
"""PR 12 service slices: semantic extraction + geographic resolution.

Real PostgreSQL + ObjectStore, real application services, real grounding and
resolution orchestration. Only the external model boundary (``FakeLlmClient``)
and the external resolver boundary (``FakeGeographicResolver``) are faked.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.conftest import _raw_connect

from darkula.app.extraction import (
    DeterministicExtractionService,
    deterministic_observables_profile,
)
from darkula.app.geography import (
    GeographicResolutionInputUnavailableError,
    GeographicResolutionIntegrityError,
    GeographicResolutionResolverError,
    GeographicResolutionService,
)
from darkula.app.llm import LlmError, LlmErrorCode
from darkula.app.object_store import ContentHash, ContentType, ObjectStore
from darkula.app.persistence import DarkulaSpi
from darkula.app.semantic_extraction import (
    SemanticExtractionResponse,
    SemanticExtractionService,
    SemanticInputUnavailableError,
    SemanticIntegrityError,
    SemanticMentionCandidate,
    SemanticModelError,
)
from darkula.config.settings import DatabaseSettings
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.extraction import EntityType, ExtractedEntity
from darkula.domain.geography import (
    GeographicResolution,
    GeographicResolutionStatus,
    GeographicResolverIdentity,
    GeographicResolverResult,
)
from darkula.domain.identifiers import (
    ContentArtifactId,
    NormalizedContentId,
    ObjectKey,
)
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_geographic_resolver import FakeGeographicResolver
from darkula.testing.fake_llm import FakeLlmClient

pytestmark = pytest.mark.integration

_MOMENT = datetime(2026, 5, 1, tzinfo=UTC)
_TEXT_PLAIN = ContentType("text/plain")


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
    await store.put(key=key, data=_iter_bytes(payload), content_type=_TEXT_PLAIN)
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
        await uow.content.create_artifact(artifact)
        await uow.content.create_observation(observation)
        await uow.commit()
    return observation


def _semantic_service(
    spi: DarkulaSpi,
    store: ObjectStore,
    llm: FakeLlmClient,
    *,
    max_entities: int = 100,
    max_input_bytes: int = 262144,
) -> SemanticExtractionService:
    return SemanticExtractionService(
        spi=spi,
        object_store=store,
        llm=llm,
        max_entities=max_entities,
        max_input_bytes=max_input_bytes,
    )


def _location_response(
    raw: str, *, left: str = "", right: str = ""
) -> SemanticExtractionResponse:
    return SemanticExtractionResponse(
        mentions=[
            SemanticMentionCandidate(
                entity_type=EntityType.LOCATION,
                raw_value=raw,
                confidence=0.9,
                left_context=left or None,
                right_context=right or None,
            )
        ]
    )


def _resolved(name: str) -> GeographicResolverResult:
    return GeographicResolverResult(
        status=GeographicResolutionStatus.RESOLVED,
        canonical_name=name,
        country_code="US",
        confidence=0.8,
        latitude=47.75,
        longitude=-120.74,
    )


class TestSemanticFailureSlices:
    @pytest.mark.asyncio
    async def test_missing_object_no_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        observation = await _seed(
            spi,
            LocalFileObjectStore(tmp_path / "objects"),
            "Mason Creek, Washington",
            request_id="crawl-sem-missing",
            index=1,
        )
        service = _semantic_service(
            spi, LocalFileObjectStore(tmp_path / "other"), FakeLlmClient()
        )
        with pytest.raises(SemanticInputUnavailableError):
            await service.extract(observation.content_id)
        async with spi.unit_of_work() as uow:
            assert (
                await uow.extraction.get_result_by_profile(
                    observation.content_id, "semantic-entities", "v1"
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_hash_mismatch_no_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        text = "Mason Creek, Washington"
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(
            spi, store, text, request_id="crawl-sem-hash", index=2
        )
        payload = text.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        key = ObjectKey(f"sha256/{digest[:2]}/{digest}")
        await store.put(key=key, data=_iter_bytes(b"different bytes"))
        service = _semantic_service(spi, store, FakeLlmClient())
        with pytest.raises(SemanticIntegrityError):
            await service.extract(observation.content_id)

    @pytest.mark.asyncio
    async def test_model_failure_no_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(
            spi, store, "Mason Creek, Washington", request_id="crawl-sem-llm", index=3
        )
        llm = FakeLlmClient()
        llm.enqueue(LlmError(LlmErrorCode.PROVIDER_FAILURE))
        service = _semantic_service(spi, store, llm)
        with pytest.raises(SemanticModelError):
            await service.extract(observation.content_id)

    @pytest.mark.asyncio
    async def test_concurrent_semantic_one_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(
            spi,
            store,
            "Mason Creek General Hospital is in Mason Creek, Washington.",
            request_id="crawl-sem-race",
            index=4,
        )
        llm = FakeLlmClient(
            default_response=SemanticExtractionResponse(
                mentions=[
                    SemanticMentionCandidate(
                        entity_type=EntityType.ORGANIZATION,
                        raw_value="Mason Creek General Hospital",
                        confidence=0.9,
                    ),
                    SemanticMentionCandidate(
                        entity_type=EntityType.LOCATION,
                        raw_value="Washington",
                        confidence=0.8,
                        left_context="Mason Creek, ",
                        right_context=".",
                    ),
                ]
            )
        )
        service = _semantic_service(spi, store, llm)
        first, second = await asyncio.gather(
            service.extract(observation.content_id),
            service.extract(observation.content_id),
        )
        assert first.result.result_id == second.result.result_id
        assert {first.created_result, second.created_result} == {True, False}
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM extraction_result")
                results = cur.fetchone()
        finally:
            conn.close()
        assert results is not None and results[0] == 1


class TestGeographicFailureSlices:
    async def _semantic_location(
        self,
        spi: PostgresDarkulaSpi,
        store: ObjectStore,
        text: str,
        *,
        index: int,
        raw: str = "Washington",
        left: str = "",
        right: str = "",
    ) -> tuple[NormalizedContent, ExtractedEntity]:
        observation = await _seed(
            spi, store, text, request_id=f"crawl-geo-{index}", index=index
        )
        service = _semantic_service(
            spi,
            store,
            FakeLlmClient(
                default_response=_location_response(raw, left=left, right=right)
            ),
        )
        result = await service.extract(observation.content_id)
        location = next(
            entity
            for entity in result.entities
            if entity.entity_type is EntityType.LOCATION
        )
        return observation, location

    @pytest.mark.asyncio
    async def test_missing_object_no_resolution(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        _, location = await self._semantic_location(
            spi, store, "the state of Washington", index=10
        )
        service = GeographicResolutionService(
            spi=spi,
            object_store=LocalFileObjectStore(tmp_path / "other"),
            resolver=FakeGeographicResolver(default_result=_resolved("Washington")),
            max_input_bytes=262144,
        )
        with pytest.raises(GeographicResolutionInputUnavailableError):
            await service.resolve(location.entity_id)

    @pytest.mark.asyncio
    async def test_hash_mismatch_no_resolution(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        text = "the state of Washington"
        _, location = await self._semantic_location(spi, store, text, index=11)
        payload = text.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        key = ObjectKey(f"sha256/{digest[:2]}/{digest}")
        await store.put(key=key, data=_iter_bytes(b"tampered"))
        service = GeographicResolutionService(
            spi=spi,
            object_store=store,
            resolver=FakeGeographicResolver(default_result=_resolved("Washington")),
            max_input_bytes=262144,
        )
        with pytest.raises(GeographicResolutionIntegrityError):
            await service.resolve(location.entity_id)

    @pytest.mark.asyncio
    async def test_resolver_failure_no_resolution(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        _, location = await self._semantic_location(
            spi, store, "the state of Washington", index=12
        )
        resolver = FakeGeographicResolver()
        resolver.enqueue(RuntimeError("provider down"))
        service = GeographicResolutionService(
            spi=spi,
            object_store=store,
            resolver=resolver,
            max_input_bytes=262144,
        )
        with pytest.raises(GeographicResolutionResolverError):
            await service.resolve(location.entity_id)

    @pytest.mark.asyncio
    async def test_concurrent_resolution_one_row(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        _, location = await self._semantic_location(
            spi, store, "the state of Washington", index=13
        )
        service = GeographicResolutionService(
            spi=spi,
            object_store=store,
            resolver=FakeGeographicResolver(default_result=_resolved("Washington")),
            max_input_bytes=262144,
        )
        first, second = await asyncio.gather(
            service.resolve(location.entity_id),
            service.resolve(location.entity_id),
        )
        assert first.resolution.resolution_id == second.resolution.resolution_id
        assert {first.created_resolution, second.created_resolution} == {True, False}
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM geographic_resolution")
                rows = cur.fetchone()
        finally:
            conn.close()
        assert rows is not None and rows[0] == 1


class TestWashingtonAmbiguitySlice:
    @pytest.mark.asyncio
    async def test_state_dc_and_ambiguous_are_distinct(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")

        # Case A: Washington State clue -> resolved to a test Washington State.
        await _semantic_and_resolve(
            spi,
            store,
            "Mason Creek General Hospital is in Mason Creek, Washington.",
            index=30,
            resolver_result=_resolved("Washington (State)"),
        )

        # Case B: Washington, D.C. clue -> resolved to a distinct test place.
        await _semantic_and_resolve(
            spi,
            store,
            "The conference is in Washington, D.C. this year.",
            index=31,
            raw="Washington, D.C.",
            resolver_result=_resolved("Washington, D.C."),
        )

        # Case C: bare "Washington" with an ambiguous resolver -> AMBIGUOUS,
        # no canonical place persisted. Production code has no hard-coded
        # "Washington" -> Washington State special case.
        ambiguous = await _semantic_and_resolve(
            spi,
            store,
            "Washington",
            index=32,
            resolver_result=GeographicResolverResult(
                status=GeographicResolutionStatus.AMBIGUOUS, confidence=0.45
            ),
        )
        assert ambiguous.status is GeographicResolutionStatus.AMBIGUOUS
        assert ambiguous.canonical_name is None
        assert ambiguous.latitude is None


async def _semantic_and_resolve(
    spi: PostgresDarkulaSpi,
    store: ObjectStore,
    text: str,
    *,
    index: int,
    raw: str = "Washington",
    resolver_result: GeographicResolverResult,
) -> GeographicResolution:
    observation = await _seed(
        spi, store, text, request_id=f"crawl-amb-{index}", index=index
    )
    semantic = _semantic_service(
        spi,
        store,
        FakeLlmClient(default_response=_location_response(raw)),
    )
    extracted = await semantic.extract(observation.content_id)
    location = next(
        entity
        for entity in extracted.entities
        if entity.entity_type is EntityType.LOCATION
    )
    resolver = FakeGeographicResolver(
        identity=GeographicResolverIdentity(name="fake-geo", version="v1"),
        default_result=resolver_result,
    )
    geography = GeographicResolutionService(
        spi=spi,
        object_store=store,
        resolver=resolver,
        max_input_bytes=262144,
    )
    result = await geography.resolve(location.entity_id)
    return result.resolution


class TestPr11RemainsSeparate:
    @pytest.mark.asyncio
    async def test_deterministic_service_unchanged_alongside_semantic(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        observation = await _seed(
            spi,
            store,
            "contact bob@example.com from Mason Creek, Washington",
            request_id="crawl-pr11",
            index=40,
        )
        deterministic = await DeterministicExtractionService(
            spi=spi,
            object_store=store,
            profile=deterministic_observables_profile(max_entities=100),
        ).extract(observation.content_id)
        assert deterministic.created_result is True
        assert any(
            entity.entity_type is EntityType.EMAIL for entity in deterministic.entities
        )
        assert all(
            entity.extraction_confidence is None for entity in deterministic.entities
        )
