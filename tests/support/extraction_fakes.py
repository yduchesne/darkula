# SPDX-License-Identifier: AGPL-3.0-only
"""In-memory extraction persistence fakes for deterministic PR 11 unit tests.

These fakes implement the same repository contracts and atomicity semantics
as the production PostgreSQL adapter (one unit of work = one transaction that
either commits all result/entity writes or discards them), so application
service behavior is tested offline. They are test-only and never used in
production composition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Self

from darkula.app.object_store import ContentHash
from darkula.app.persistence import (
    ConflictError,
    DarkulaSpi,
    IntegrityError,
    UnitOfWork,
)
from darkula.app.repositories import (
    CollectionRepository,
    ContentRepository,
    ExtractionRepository,
    GeographicResolutionRepository,
    RelationshipRepository,
)
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.extraction import ExtractedEntity, ExtractionResult
from darkula.domain.geography import GeographicResolution
from darkula.domain.identifiers import (
    ContentArtifactId,
    ExtractedEntityId,
    ExtractedRelationshipId,
    ExtractionResultId,
    GeographicResolutionId,
    NormalizedContentId,
    RelationshipExtractionResultId,
)
from darkula.domain.relationships import (
    ExtractedRelationship,
    RelationshipExtractionResult,
)


@dataclass
class MemExtractionState:
    """Mutable in-memory persisted state shared across units of work."""

    artifacts: dict[ContentArtifactId, ContentArtifact] = field(default_factory=dict)
    observations: dict[NormalizedContentId, NormalizedContent] = field(
        default_factory=dict
    )
    results: dict[ExtractionResultId, ExtractionResult] = field(default_factory=dict)
    result_keys: dict[tuple[str, str, str], ExtractionResultId] = field(
        default_factory=dict
    )
    entities: dict[ExtractedEntityId, ExtractedEntity] = field(default_factory=dict)
    resolutions: dict[GeographicResolutionId, GeographicResolution] = field(
        default_factory=dict
    )
    resolution_keys: dict[tuple[str, str, str], GeographicResolutionId] = field(
        default_factory=dict
    )
    relationship_results: dict[
        RelationshipExtractionResultId, RelationshipExtractionResult
    ] = field(default_factory=dict)
    relationship_result_keys: dict[
        tuple[str, str, str], RelationshipExtractionResultId
    ] = field(default_factory=dict)
    relationships: dict[ExtractedRelationshipId, ExtractedRelationship] = field(
        default_factory=dict
    )

    def snapshot(self) -> MemExtractionState:
        return MemExtractionState(
            artifacts=dict(self.artifacts),
            observations=dict(self.observations),
            results=dict(self.results),
            result_keys=dict(self.result_keys),
            entities=dict(self.entities),
            resolutions=dict(self.resolutions),
            resolution_keys=dict(self.resolution_keys),
            relationship_results=dict(self.relationship_results),
            relationship_result_keys=dict(self.relationship_result_keys),
            relationships=dict(self.relationships),
        )

    def restore(self, other: MemExtractionState) -> None:
        self.artifacts = other.artifacts
        self.observations = other.observations
        self.results = other.results
        self.result_keys = other.result_keys
        self.entities = other.entities
        self.resolutions = other.resolutions
        self.resolution_keys = other.resolution_keys
        self.relationship_results = other.relationship_results
        self.relationship_result_keys = other.relationship_result_keys
        self.relationships = other.relationships


class _MemContentRepository(ContentRepository):
    def __init__(self, state: MemExtractionState) -> None:
        self._state = state

    async def create_artifact(self, artifact: ContentArtifact) -> None:
        self._state.artifacts[artifact.artifact_id] = artifact

    async def get_artifact(
        self, artifact_id: ContentArtifactId
    ) -> ContentArtifact | None:
        return self._state.artifacts.get(artifact_id)

    async def find_artifact_by_representation(
        self,
        *,
        content_hash: ContentHash,
        kind: ArtifactKind,
        completeness: ArtifactCompleteness,
    ) -> ContentArtifact | None:
        for artifact in self._state.artifacts.values():
            if (
                artifact.content_hash == content_hash
                and artifact.artifact_kind is kind
                and artifact.completeness is completeness
            ):
                return artifact
        return None

    async def create_observation(self, content: NormalizedContent) -> None:
        self._state.observations[content.content_id] = content

    async def get_observation(
        self, content_id: NormalizedContentId
    ) -> NormalizedContent | None:
        return self._state.observations.get(content_id)

    async def list_observations_for_requests(
        self,
        request_ids: tuple[str, ...],
        *,
        window_start: datetime,
        window_end: datetime,
        limit: int,
    ) -> tuple[NormalizedContent, ...]:
        allowed = set(request_ids)
        matches = [
            content
            for content in self._state.observations.values()
            if content.crawl_request_id in allowed
            and window_start <= content.observed_at <= window_end
        ]
        ordered = sorted(
            matches, key=lambda item: (item.observed_at, str(item.content_id))
        )
        return tuple(ordered[:limit])


class _MemExtractionRepository(ExtractionRepository):
    def __init__(
        self,
        state: MemExtractionState,
        *,
        fail_on_entity_index: int | None = None,
    ) -> None:
        self._state = state
        self._fail_on_entity_index = fail_on_entity_index
        self._entity_writes = 0

    async def create_result(self, result: ExtractionResult) -> None:
        if result.content_id not in self._state.observations:
            raise IntegrityError("the referenced normalized content does not exist")
        key = (str(result.content_id), result.profile_name, result.profile_version)
        if key in self._state.result_keys:
            raise ConflictError("an extraction result already exists")
        self._state.results[result.result_id] = result
        self._state.result_keys[key] = result.result_id

    async def get_result(
        self, result_id: ExtractionResultId
    ) -> ExtractionResult | None:
        return self._state.results.get(result_id)

    async def get_result_by_profile(
        self,
        content_id: NormalizedContentId,
        profile_name: str,
        profile_version: str,
    ) -> ExtractionResult | None:
        key = (str(content_id), profile_name, profile_version)
        result_id = self._state.result_keys.get(key)
        return None if result_id is None else self._state.results.get(result_id)

    async def create_entity(self, entity: ExtractedEntity) -> None:
        self._entity_writes += 1
        if self._fail_on_entity_index == self._entity_writes:
            raise IntegrityError("injected entity persistence failure")
        result = self._state.results.get(entity.extraction_result_id)
        if result is None:
            raise IntegrityError("the referenced extraction result does not exist")
        if result.content_id != entity.content_id:
            raise IntegrityError("content does not match result content")
        for existing in self._state.entities.values():
            if (
                existing.extraction_result_id == entity.extraction_result_id
                and existing.entity_type is entity.entity_type
                and existing.source_span == entity.source_span
                and existing.extractor == entity.extractor
                and existing.normalized_value == entity.normalized_value
            ):
                raise ConflictError("this extracted-entity occurrence already exists")
        self._state.entities[entity.entity_id] = entity

    def _ordered(self, entities: list[ExtractedEntity]) -> tuple[ExtractedEntity, ...]:
        return tuple(
            sorted(
                entities,
                key=lambda item: (
                    item.source_span.start,
                    item.source_span.end,
                    item.entity_type.value,
                    item.normalized_value,
                    item.extractor.name,
                    item.extractor.version,
                    str(item.entity_id),
                ),
            )
        )

    async def list_entities_for_result(
        self, result_id: ExtractionResultId
    ) -> tuple[ExtractedEntity, ...]:
        return self._ordered(
            [
                entity
                for entity in self._state.entities.values()
                if entity.extraction_result_id == result_id
            ]
        )

    async def list_entities_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[ExtractedEntity, ...]:
        return self._ordered(
            [
                entity
                for entity in self._state.entities.values()
                if entity.content_id == content_id
            ]
        )

    async def get_entity(self, entity_id: ExtractedEntityId) -> ExtractedEntity | None:
        return self._state.entities.get(entity_id)


class _MemGeographicResolutionRepository(GeographicResolutionRepository):
    def __init__(self, state: MemExtractionState) -> None:
        self._state = state

    async def create(self, resolution: GeographicResolution) -> None:
        key = (
            str(resolution.extracted_entity_id),
            resolution.resolver.name,
            resolution.resolver.version,
        )
        if key in self._state.resolution_keys or (
            resolution.resolution_id in self._state.resolutions
        ):
            raise ConflictError("a geographic resolution already exists")
        if resolution.extracted_entity_id not in self._state.entities:
            raise IntegrityError("the referenced extracted entity does not exist")
        self._state.resolutions[resolution.resolution_id] = resolution
        self._state.resolution_keys[key] = resolution.resolution_id

    async def get(
        self, resolution_id: GeographicResolutionId
    ) -> GeographicResolution | None:
        return self._state.resolutions.get(resolution_id)

    async def get_for_entity(
        self,
        entity_id: ExtractedEntityId,
        resolver_name: str,
        resolver_version: str,
    ) -> GeographicResolution | None:
        key = (str(entity_id), resolver_name, resolver_version)
        resolution_id = self._state.resolution_keys.get(key)
        if resolution_id is None:
            return None
        return self._state.resolutions.get(resolution_id)

    async def list_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[GeographicResolution, ...]:
        entity_ids = {
            entity.entity_id
            for entity in self._state.entities.values()
            if entity.content_id == content_id
        }
        resolutions = [
            resolution
            for resolution in self._state.resolutions.values()
            if resolution.extracted_entity_id in entity_ids
        ]
        return tuple(
            sorted(
                resolutions,
                key=lambda item: (item.resolved_at, str(item.resolution_id)),
            )
        )


class _MemRelationshipRepository(RelationshipRepository):
    def __init__(
        self,
        state: MemExtractionState,
        *,
        fail_on_relationship_index: int | None = None,
    ) -> None:
        self._state = state
        self._fail_on_relationship_index = fail_on_relationship_index
        self._relationship_writes = 0

    async def create_result(self, result: RelationshipExtractionResult) -> None:
        if result.content_id not in self._state.observations:
            raise IntegrityError("the referenced normalized content does not exist")
        key = (str(result.content_id), result.profile_name, result.profile_version)
        if key in self._state.relationship_result_keys:
            raise ConflictError("a relationship result already exists")
        self._state.relationship_results[result.result_id] = result
        self._state.relationship_result_keys[key] = result.result_id

    async def get_result(
        self, result_id: RelationshipExtractionResultId
    ) -> RelationshipExtractionResult | None:
        return self._state.relationship_results.get(result_id)

    async def get_result_by_profile(
        self,
        content_id: NormalizedContentId,
        profile_name: str,
        profile_version: str,
    ) -> RelationshipExtractionResult | None:
        key = (str(content_id), profile_name, profile_version)
        result_id = self._state.relationship_result_keys.get(key)
        return (
            None
            if result_id is None
            else self._state.relationship_results.get(result_id)
        )

    async def create_relationship(self, relationship: ExtractedRelationship) -> None:
        self._relationship_writes += 1
        if self._fail_on_relationship_index == self._relationship_writes:
            raise IntegrityError("injected relationship persistence failure")
        result = self._state.relationship_results.get(relationship.extraction_result_id)
        if result is None:
            raise IntegrityError("the referenced relationship result does not exist")
        if result.content_id != relationship.content_id:
            raise IntegrityError("content does not match result content")
        for endpoint_id in (
            relationship.source_entity_id,
            relationship.target_entity_id,
        ):
            entity = self._state.entities.get(endpoint_id)
            if entity is None:
                raise IntegrityError("the referenced endpoint does not exist")
            if entity.content_id != relationship.content_id:
                raise IntegrityError("an endpoint belongs to a different content")
        for existing in self._state.relationships.values():
            if (
                existing.extraction_result_id == relationship.extraction_result_id
                and existing.source_entity_id == relationship.source_entity_id
                and existing.predicate == relationship.predicate
                and existing.target_entity_id == relationship.target_entity_id
                and existing.support_span == relationship.support_span
                and existing.extractor == relationship.extractor
            ):
                raise ConflictError("this relationship assertion already exists")
        self._state.relationships[relationship.relationship_id] = relationship

    def _ordered(
        self, relationships: list[ExtractedRelationship]
    ) -> tuple[ExtractedRelationship, ...]:
        return tuple(
            sorted(
                relationships,
                key=lambda item: (
                    item.support_span.start,
                    item.support_span.end,
                    str(item.source_entity_id),
                    item.predicate.value,
                    str(item.target_entity_id),
                    str(item.relationship_id),
                ),
            )
        )

    async def list_for_result(
        self, result_id: RelationshipExtractionResultId
    ) -> tuple[ExtractedRelationship, ...]:
        return self._ordered(
            [
                relationship
                for relationship in self._state.relationships.values()
                if relationship.extraction_result_id == result_id
            ]
        )

    async def list_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[ExtractedRelationship, ...]:
        return self._ordered(
            [
                relationship
                for relationship in self._state.relationships.values()
                if relationship.content_id == content_id
            ]
        )

    async def get_relationship(
        self, relationship_id: ExtractedRelationshipId
    ) -> ExtractedRelationship | None:
        return self._state.relationships.get(relationship_id)


class MemExtractionUnitOfWork(UnitOfWork):
    """One in-memory transaction with snapshot/commit/rollback semantics."""

    def __init__(
        self,
        state: MemExtractionState,
        *,
        fail_on_entity_index: int | None = None,
        fail_on_relationship_index: int | None = None,
    ) -> None:
        self._state = state
        self._fail_on_entity_index = fail_on_entity_index
        self._fail_on_relationship_index = fail_on_relationship_index
        self._working = state.snapshot()
        self._content: _MemContentRepository | None = None
        self._extraction: _MemExtractionRepository | None = None
        self._geography: _MemGeographicResolutionRepository | None = None
        self._relationships: _MemRelationshipRepository | None = None

    async def __aenter__(self) -> Self:
        self._working = self._state.snapshot()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        self._state.restore(self._working)

    async def rollback(self) -> None:
        self._working = self._state.snapshot()

    @property
    def source_candidates(self) -> Any:
        raise AssertionError("extraction fake exposes no candidate repository")

    @property
    def sources(self) -> Any:
        raise AssertionError("extraction fake exposes no source repository")

    @property
    def outbox(self) -> Any:
        raise AssertionError("extraction fake exposes no outbox repository")

    @property
    def processed_messages(self) -> Any:
        raise AssertionError("extraction fake exposes no processed-message repository")

    @property
    def content(self) -> ContentRepository:
        if self._content is None:
            self._content = _MemContentRepository(self._working)
        return self._content

    @property
    def collection(self) -> CollectionRepository:
        raise AssertionError("extraction fake exposes no collection repository")

    @property
    def extraction(self) -> ExtractionRepository:
        if self._extraction is None:
            self._extraction = _MemExtractionRepository(
                self._working, fail_on_entity_index=self._fail_on_entity_index
            )
        return self._extraction

    @property
    def geography(self) -> GeographicResolutionRepository:
        if self._geography is None:
            self._geography = _MemGeographicResolutionRepository(self._working)
        return self._geography

    @property
    def relationships(self) -> RelationshipRepository:
        if self._relationships is None:
            self._relationships = _MemRelationshipRepository(
                self._working,
                fail_on_relationship_index=self._fail_on_relationship_index,
            )
        return self._relationships


class MemExtractionSpi(DarkulaSpi):
    """One in-memory SPI producing snapshot-isolated extraction units of work."""

    def __init__(
        self,
        state: MemExtractionState | None = None,
        *,
        fail_on_entity_index: int | None = None,
        fail_on_relationship_index: int | None = None,
    ) -> None:
        self.state = state or MemExtractionState()
        self._fail_on_entity_index = fail_on_entity_index
        self._fail_on_relationship_index = fail_on_relationship_index

    def unit_of_work(self) -> UnitOfWork:
        return MemExtractionUnitOfWork(
            self.state,
            fail_on_entity_index=self._fail_on_entity_index,
            fail_on_relationship_index=self._fail_on_relationship_index,
        )


__all__ = [
    "MemExtractionSpi",
    "MemExtractionState",
    "MemExtractionUnitOfWork",
]
