# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic in-memory persistence fakes for PR 14 unit tests.

These fakes implement only the repository surface the source-analysis context
builder and service depend on, with direct shared state and explicit
``append_source_assessment`` failure injection. They never touch PostgreSQL,
the crawler, ObjectStore, DataStream, or any model provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Self

from darkula.app.persistence import ConflictError, DarkulaSpi, UnitOfWork
from darkula.domain.collection import CollectionRun
from darkula.domain.content import NormalizedContent
from darkula.domain.extraction import ExtractedEntity
from darkula.domain.geography import GeographicResolution
from darkula.domain.identifiers import (
    CollectionRunId,
    NormalizedContentId,
    SourceAssessmentId,
    SourceId,
)
from darkula.domain.relationships import ExtractedRelationship
from darkula.domain.source import Source, SourceAssessment


@dataclass
class MemSourceAnalysisState:
    """Committed in-memory source-analysis state shared across units of work."""

    sources: dict[SourceId, Source] = field(default_factory=dict)
    runs: dict[CollectionRunId, CollectionRun] = field(default_factory=dict)
    assessments: dict[SourceAssessmentId, SourceAssessment] = field(
        default_factory=dict
    )
    observations: dict[NormalizedContentId, NormalizedContent] = field(
        default_factory=dict
    )
    entities: dict[str, ExtractedEntity] = field(default_factory=dict)
    resolutions: dict[str, GeographicResolution] = field(default_factory=dict)
    relationships: dict[str, ExtractedRelationship] = field(default_factory=dict)
    #: When set, ``append_source_assessment`` raises this instead of appending.
    append_error: Exception | None = None
    #: When True, ``append_source_assessment`` raises a semantic ConflictError.
    semantic_conflict: bool = False
    commit_count: int = 0
    rollback_count: int = 0


class _MemSources:
    def __init__(self, state: MemSourceAnalysisState) -> None:
        self._state = state

    async def get(self, source_id: SourceId) -> Source | None:
        return self._state.sources.get(source_id)

    async def get_source_assessment_by_profile(
        self,
        source_id: SourceId,
        window_start: datetime,
        window_end: datetime,
        profile_name: str,
        profile_version: str,
    ) -> SourceAssessment | None:
        for assessment in self._state.assessments.values():
            if (
                assessment.source_id == source_id
                and assessment.window_start == window_start
                and assessment.window_end == window_end
                and assessment.profile_name == profile_name
                and assessment.profile_version == profile_version
            ):
                return assessment
        return None

    async def append_source_assessment(self, assessment: SourceAssessment) -> None:
        if self._state.append_error is not None:
            raise self._state.append_error
        if self._state.semantic_conflict:
            raise ConflictError("semantic duplicate")
        if assessment.assessment_id in self._state.assessments:
            raise ConflictError("duplicate assessment identity")
        for existing in self._state.assessments.values():
            if (
                existing.source_id == assessment.source_id
                and existing.window_start == assessment.window_start
                and existing.window_end == assessment.window_end
                and existing.profile_name == assessment.profile_name
                and existing.profile_version == assessment.profile_version
            ):
                raise ConflictError("semantic duplicate")
        self._state.assessments[assessment.assessment_id] = assessment


class _MemCollection:
    def __init__(self, state: MemSourceAnalysisState) -> None:
        self._state = state

    async def list_runs_for_source_window(
        self,
        source_id: SourceId,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[CollectionRun, ...]:
        matches = [
            run
            for run in self._state.runs.values()
            if run.source_id == source_id
            and run.created_at <= window_end
            and (run.completed_at is None or run.completed_at >= window_start)
        ]
        return tuple(sorted(matches, key=lambda run: (run.created_at, str(run.run_id))))


class _MemContent:
    def __init__(self, state: MemSourceAnalysisState) -> None:
        self._state = state

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


class _MemExtraction:
    def __init__(self, state: MemSourceAnalysisState) -> None:
        self._state = state

    async def list_entities_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[ExtractedEntity, ...]:
        return tuple(
            sorted(
                (
                    entity
                    for entity in self._state.entities.values()
                    if entity.content_id == content_id
                ),
                key=lambda item: (
                    item.source_span.start,
                    item.source_span.end,
                    str(item.entity_id),
                ),
            )
        )


class _MemGeography:
    def __init__(self, state: MemSourceAnalysisState) -> None:
        self._state = state

    async def list_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[GeographicResolution, ...]:
        entity_ids = {
            str(entity.entity_id)
            for entity in self._state.entities.values()
            if entity.content_id == content_id
        }
        return tuple(
            sorted(
                (
                    resolution
                    for resolution in self._state.resolutions.values()
                    if str(resolution.extracted_entity_id) in entity_ids
                ),
                key=lambda item: str(item.resolution_id),
            )
        )


class _MemRelationships:
    def __init__(self, state: MemSourceAnalysisState) -> None:
        self._state = state

    async def list_for_content(
        self, content_id: NormalizedContentId
    ) -> tuple[ExtractedRelationship, ...]:
        return tuple(
            sorted(
                (
                    relationship
                    for relationship in self._state.relationships.values()
                    if relationship.content_id == content_id
                ),
                key=lambda item: str(item.relationship_id),
            )
        )


class MemSourceAnalysisUnitOfWork(UnitOfWork):
    """One no-op-commit in-memory transaction exposing the PR 14 surface."""

    def __init__(self, state: MemSourceAnalysisState) -> None:
        self._state = state
        self._sources = _MemSources(state)
        self._collection = _MemCollection(state)
        self._content = _MemContent(state)
        self._extraction = _MemExtraction(state)
        self._geography = _MemGeography(state)
        self._relationships = _MemRelationships(state)

    async def __aenter__(self) -> Self:
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
        self._state.commit_count += 1

    async def rollback(self) -> None:
        self._state.rollback_count += 1

    @property
    def source_candidates(self) -> Any:
        raise AssertionError("source-analysis fake exposes no candidate repository")

    @property
    def sources(self) -> Any:
        return self._sources

    @property
    def outbox(self) -> Any:
        raise AssertionError("source-analysis fake exposes no outbox repository")

    @property
    def processed_messages(self) -> Any:
        raise AssertionError("source-analysis fake exposes no processed repository")

    @property
    def content(self) -> Any:
        return self._content

    @property
    def collection(self) -> Any:
        return self._collection

    @property
    def extraction(self) -> Any:
        return self._extraction

    @property
    def geography(self) -> Any:
        return self._geography

    @property
    def relationships(self) -> Any:
        return self._relationships


class MemSourceAnalysisSpi(DarkulaSpi):
    """One in-memory SPI producing the PR 14 fakes."""

    def __init__(self, state: MemSourceAnalysisState | None = None) -> None:
        self.state = state or MemSourceAnalysisState()

    def unit_of_work(self) -> UnitOfWork:
        return MemSourceAnalysisUnitOfWork(self.state)


__all__ = [
    "MemSourceAnalysisSpi",
    "MemSourceAnalysisState",
    "MemSourceAnalysisUnitOfWork",
]
