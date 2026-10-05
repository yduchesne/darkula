# SPDX-License-Identifier: AGPL-3.0-only
"""GeographicResolver / GeographicResolutionService matrix (PR 12 GS)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from tests.support.extraction_fakes import MemExtractionSpi, MemExtractionState

from darkula.app.geography import (
    GeographicResolutionContractError,
    GeographicResolutionInputUnavailableError,
    GeographicResolutionIntegrityError,
    GeographicResolutionNotApplicableError,
    GeographicResolutionNotFoundError,
    GeographicResolutionResolverError,
    GeographicResolutionService,
    GeographicResolver,
)
from darkula.app.object_store import ContentHash, ContentType, ObjectStore
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
    GeographicResolutionRequest,
    GeographicResolutionStatus,
    GeographicResolverIdentity,
    GeographicResolverResult,
)
from darkula.domain.identifiers import (
    ContentArtifactId,
    ExtractedEntityId,
    ExtractionResultId,
    NormalizedContentId,
    ObjectKey,
)
from darkula.infrastructure.object_store.in_memory import InMemoryObjectStore
from darkula.testing.fake_geographic_resolver import FakeGeographicResolver

_MOMENT = datetime(2026, 5, 1, tzinfo=UTC)
_TEXT_PLAIN = ContentType("text/plain")


async def _iter_bytes(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _resolved_result(
    *,
    name: str = "Washington",
    confidence: float = 0.8,
    reference: str | None = "ref-1",
) -> GeographicResolverResult:
    return GeographicResolverResult(
        status=GeographicResolutionStatus.RESOLVED,
        canonical_name=name,
        country_code="US",
        administrative_area="Washington",
        latitude=47.75,
        longitude=-120.74,
        confidence=confidence,
        reference=reference,
    )


async def _seed_location(
    state: MemExtractionState,
    store: ObjectStore,
    text: str,
    *,
    raw: str | None = None,
    entity_type: EntityType = EntityType.LOCATION,
    span_override: SourceSpan | None = None,
) -> tuple[ExtractedEntity, ContentArtifact, NormalizedContent]:
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    key = ObjectKey(f"sha256/{digest[:2]}/{digest}")
    await store.put(key=key, data=_iter_bytes(data), content_type=_TEXT_PLAIN)
    artifact = ContentArtifact(
        artifact_id=ContentArtifactId.generate(),
        object_key=key,
        content_type=_TEXT_PLAIN,
        size_bytes=len(data),
        content_hash=ContentHash(algorithm="sha-256", digest_hex=digest),
        artifact_kind=ArtifactKind.NORMALIZED_TEXT,
        completeness=ArtifactCompleteness.COMPLETE,
        created_at=_MOMENT,
    )
    observation = NormalizedContent(
        content_id=NormalizedContentId.generate(),
        artifact_id=artifact.artifact_id,
        source_uri="http://blackgate.example.test/thread/1",
        observed_at=_MOMENT,
        crawl_request_id="crawl-1",
        observation_index=1,
        normalization_version="text-v1",
        completeness=ArtifactCompleteness.SAMPLE,
        title="Thread",
        text="preview",
        content_type=_TEXT_PLAIN,
    )
    result = ExtractionResult(
        result_id=ExtractionResultId.generate(),
        content_id=observation.content_id,
        profile_name="semantic-entities",
        profile_version="v1",
        extractor_manifest=(ExtractorIdentity(name="semantic-llm", version="v1"),),
        extracted_at=_MOMENT,
        entity_count=1,
    )
    mention = raw if raw is not None else "Washington"
    span = span_override or SourceSpan(
        start=text.index(mention), end=text.index(mention) + len(mention)
    )
    entity = ExtractedEntity(
        entity_id=ExtractedEntityId.generate(),
        extraction_result_id=result.result_id,
        content_id=observation.content_id,
        entity_type=entity_type,
        raw_value=mention,
        normalized_value=mention,
        source_span=span,
        extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
        extraction_confidence=0.9,
    )
    state.artifacts[artifact.artifact_id] = artifact
    state.observations[observation.content_id] = observation
    state.results[result.result_id] = result
    state.result_keys[
        (str(observation.content_id), result.profile_name, result.profile_version)
    ] = result.result_id
    state.entities[entity.entity_id] = entity
    return entity, artifact, observation


def _service(
    spi: MemExtractionSpi,
    store: ObjectStore,
    resolver: GeographicResolver,
    *,
    context_chars: int = 200,
    max_input_bytes: int = 262144,
) -> GeographicResolutionService:
    return GeographicResolutionService(
        spi=spi,
        object_store=store,
        resolver=resolver,
        context_chars=context_chars,
        max_input_bytes=max_input_bytes,
    )


class TestGeographicResolutionService:
    @pytest.mark.asyncio
    async def test_gs1_location_calls_resolver_with_bounded_context(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(
            state, store, "serving Mason Creek, Washington, US"
        )
        resolver = FakeGeographicResolver(default_result=_resolved_result())
        result = await _service(MemExtractionSpi(state), store, resolver).resolve(
            entity.entity_id
        )
        assert result.created_resolution is True
        assert resolver.call_count == 1
        call = resolver.calls[0]
        assert call.request.mention == "Washington"
        assert "Mason Creek" in call.request.left_context

    @pytest.mark.asyncio
    async def test_gs2_non_location_not_applicable(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(
            state,
            store,
            "Mason Creek General Hospital",
            raw="Mason Creek General Hospital",
            entity_type=EntityType.ORGANIZATION,
        )
        resolver = FakeGeographicResolver(default_result=_resolved_result())
        with pytest.raises(GeographicResolutionNotApplicableError):
            await _service(MemExtractionSpi(state), store, resolver).resolve(
                entity.entity_id
            )
        assert resolver.call_count == 0

    @pytest.mark.asyncio
    async def test_gs3_missing_entity_not_found(self) -> None:
        resolver = FakeGeographicResolver(default_result=_resolved_result())
        service = _service(MemExtractionSpi(), InMemoryObjectStore(), resolver)
        with pytest.raises(GeographicResolutionNotFoundError):
            await service.resolve(ExtractedEntityId.generate())

    @pytest.mark.asyncio
    async def test_gs4_span_mismatch_is_integrity_failure(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(
            state,
            store,
            "totally different text",
            raw="Washington",
            span_override=SourceSpan(start=0, end=10),
        )
        resolver = FakeGeographicResolver(default_result=_resolved_result())
        with pytest.raises(GeographicResolutionIntegrityError):
            await _service(MemExtractionSpi(state), store, resolver).resolve(
                entity.entity_id
            )
        assert resolver.call_count == 0

    @pytest.mark.asyncio
    async def test_gs5_missing_object_unavailable(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington")
        resolver = FakeGeographicResolver(default_result=_resolved_result())
        with pytest.raises(GeographicResolutionInputUnavailableError):
            await _service(
                MemExtractionSpi(state), InMemoryObjectStore(), resolver
            ).resolve(entity.entity_id)

    @pytest.mark.asyncio
    async def test_gs7_resolved_persisted(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington")
        resolver = FakeGeographicResolver(default_result=_resolved_result())
        result = await _service(MemExtractionSpi(state), store, resolver).resolve(
            entity.entity_id
        )
        resolution = result.resolution
        assert resolution.status is GeographicResolutionStatus.RESOLVED
        assert resolution.canonical_name == "Washington"
        assert resolution.country_code == "US"
        assert resolution.confidence == 0.8
        assert resolution.latitude == 47.75
        assert resolution.resolver.name == "fake-geo"

    @pytest.mark.asyncio
    async def test_gs11_ambiguous_persisted_without_canonical_place(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington")
        resolver = FakeGeographicResolver(
            default_result=GeographicResolverResult(
                status=GeographicResolutionStatus.AMBIGUOUS, confidence=0.45
            )
        )
        result = await _service(MemExtractionSpi(state), store, resolver).resolve(
            entity.entity_id
        )
        assert result.resolution.status is GeographicResolutionStatus.AMBIGUOUS
        assert result.resolution.canonical_name is None
        assert result.resolution.latitude is None

    @pytest.mark.asyncio
    async def test_gs12_unresolved_has_no_canonical_fields(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington")
        resolver = FakeGeographicResolver(
            default_result=GeographicResolverResult(
                status=GeographicResolutionStatus.UNRESOLVED
            )
        )
        result = await _service(MemExtractionSpi(state), store, resolver).resolve(
            entity.entity_id
        )
        assert result.resolution.canonical_name is None
        assert result.resolution.country_code is None

    @pytest.mark.asyncio
    async def test_gs13_existing_resolution_reused_without_resolver_call(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington")
        first_resolver = FakeGeographicResolver(default_result=_resolved_result())
        first = await _service(MemExtractionSpi(state), store, first_resolver).resolve(
            entity.entity_id
        )
        second_resolver = FakeGeographicResolver(default_result=_resolved_result())
        second = await _service(
            MemExtractionSpi(state), InMemoryObjectStore(), second_resolver
        ).resolve(entity.entity_id)
        assert second.created_resolution is False
        assert second.resolution.resolution_id == first.resolution.resolution_id
        assert second_resolver.call_count == 0

    @pytest.mark.asyncio
    async def test_gs14_new_resolver_version_coexists(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington")
        v1 = FakeGeographicResolver(
            identity=GeographicResolverIdentity(name="fake-geo", version="v1"),
            default_result=_resolved_result(name="Washington"),
        )
        v2 = FakeGeographicResolver(
            identity=GeographicResolverIdentity(name="fake-geo", version="v2"),
            default_result=_resolved_result(name="Washington State"),
        )
        first = await _service(MemExtractionSpi(state), store, v1).resolve(
            entity.entity_id
        )
        second = await _service(MemExtractionSpi(state), store, v2).resolve(
            entity.entity_id
        )
        assert first.resolution.resolution_id != second.resolution.resolution_id
        assert second.resolution.canonical_name == "Washington State"

    @pytest.mark.asyncio
    async def test_gs15_resolver_failure_no_persistence(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington")
        resolver = FakeGeographicResolver()
        resolver.enqueue(RuntimeError("provider down"))
        with pytest.raises(GeographicResolutionResolverError):
            await _service(MemExtractionSpi(state), store, resolver).resolve(
                entity.entity_id
            )
        assert state.resolutions == {}

    @pytest.mark.asyncio
    async def test_gs16_cancellation_during_resolver(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington")
        resolver = FakeGeographicResolver()
        resolver.enqueue(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _service(MemExtractionSpi(state), store, resolver).resolve(
                entity.entity_id
            )
        assert state.resolutions == {}

    @pytest.mark.asyncio
    async def test_gs18_context_at_start_of_text(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington is a state")
        resolver = FakeGeographicResolver(default_result=_resolved_result())
        await _service(MemExtractionSpi(state), store, resolver).resolve(
            entity.entity_id
        )
        assert resolver.calls[0].request.left_context == ""

    @pytest.mark.asyncio
    async def test_gs19_context_at_end_of_text(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "the state of Washington")
        resolver = FakeGeographicResolver(default_result=_resolved_result())
        await _service(MemExtractionSpi(state), store, resolver).resolve(
            entity.entity_id
        )
        assert resolver.calls[0].request.right_context == ""

    @pytest.mark.asyncio
    async def test_gs20_context_is_code_point_safe_around_unicode(self) -> None:
        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(
            state, store, "Регион Вашингтон сегодня", raw="Вашингтон"
        )
        resolver = FakeGeographicResolver(default_result=_resolved_result())
        await _service(MemExtractionSpi(state), store, resolver).resolve(
            entity.entity_id
        )
        context = resolver.calls[0].request
        assert "Регион" in context.left_context

    @pytest.mark.asyncio
    async def test_gs22_wrong_resolver_type_is_contract_error(self) -> None:
        class _WrongResolver(GeographicResolver):
            @property
            def identity(self) -> GeographicResolverIdentity:
                return GeographicResolverIdentity(name="wrong", version="v1")

            async def resolve(
                self, request: GeographicResolutionRequest
            ) -> GeographicResolverResult:
                return {"raw": "provider payload"}  # type: ignore[return-value]

        state = MemExtractionState()
        store = InMemoryObjectStore()
        entity, _, _ = await _seed_location(state, store, "Washington")
        with pytest.raises(GeographicResolutionContractError):
            await _service(MemExtractionSpi(state), store, _WrongResolver()).resolve(
                entity.entity_id
            )
