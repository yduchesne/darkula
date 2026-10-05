# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the content-ingestion orchestration (PR 8, ING-*)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self

import pytest

from darkula.app.artifacts import ArtifactStorageService
from darkula.app.content import ContentIngestService
from darkula.app.normalization import DeterministicContentNormalizer
from darkula.app.persistence import ConflictError
from darkula.app.repositories import ContentRepository
from darkula.domain.content import (
    ContentArtifact,
    ContentObservation,
    NormalizedContent,
)
from darkula.domain.identifiers import ContentArtifactId, NormalizedContentId
from darkula.infrastructure.object_store.in_memory import InMemoryObjectStore

_MOMENT = datetime(2025, 1, 1, tzinfo=UTC)


class _MemContentRepository(ContentRepository):
    """In-memory content repository for deterministic orchestration tests."""

    def __init__(self) -> None:
        self.artifacts: dict[ContentArtifactId, ContentArtifact] = {}
        self.observations: dict[NormalizedContentId, NormalizedContent] = {}
        self._by_rep: dict[tuple[object, object, object], ContentArtifact] = {}

    async def create_artifact(self, artifact: ContentArtifact) -> None:
        from darkula.app.persistence import ConflictError

        if artifact.artifact_id in self.artifacts:
            raise ConflictError("duplicate")
        self.artifacts[artifact.artifact_id] = artifact
        if artifact.content_hash is not None:
            self._by_rep[
                (artifact.content_hash, artifact.artifact_kind, artifact.completeness)
            ] = artifact

    async def get_artifact(
        self, artifact_id: ContentArtifactId
    ) -> ContentArtifact | None:
        return self.artifacts.get(artifact_id)

    async def find_artifact_by_representation(
        self, **kw: object
    ) -> ContentArtifact | None:

        return self._by_rep.get((kw["content_hash"], kw["kind"], kw["completeness"]))

    async def create_observation(self, content: NormalizedContent) -> None:
        # Unique on provenance identity (crawl_request_id, index, source_uri).
        for existing in self.observations.values():
            if (
                existing.crawl_request_id == content.crawl_request_id
                and existing.observation_index == content.observation_index
                and existing.source_uri == content.source_uri
            ):
                raise ConflictError("duplicate_provenance")
        self.observations[content.content_id] = content

    async def get_observation(
        self, content_id: NormalizedContentId
    ) -> NormalizedContent | None:
        return self.observations.get(content_id)

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
            for content in self.observations.values()
            if content.crawl_request_id in allowed
            and window_start <= content.observed_at <= window_end
        ]
        ordered = sorted(
            matches, key=lambda item: (item.observed_at, str(item.content_id))
        )
        return tuple(ordered[:limit])


class _FakeUow:
    def __init__(self, repo: _MemContentRepository) -> None:
        self._repo = repo
        self.commits = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    @property
    def content(self) -> ContentRepository:
        return self._repo


class _FakeSpi:
    def __init__(self, repo: _MemContentRepository) -> None:
        self._repo = repo
        self.uows: list[_FakeUow] = []

    def unit_of_work(self) -> _FakeUow:
        uow = _FakeUow(self._repo)
        self.uows.append(uow)
        return uow


def _ingest_service() -> tuple[ContentIngestService, _MemContentRepository]:
    normalizer = DeterministicContentNormalizer(
        artifact_service=ArtifactStorageService(object_store=InMemoryObjectStore())
    )
    repo = _MemContentRepository()
    return ContentIngestService(normalizer=normalizer, spi=_FakeSpi(repo)), repo  # type: ignore[arg-type]


def _obs(request_id: str, index: int, text: str) -> ContentObservation:
    return ContentObservation(
        crawl_request_id=request_id,
        source_uri=f"http://blackgate.example.test/thread/{index}",
        observed_at=_MOMENT,
        observation_index=index,
        text=text,
    )


class TestIngest:
    """ING-01: normalize + store + persist in the correct order."""

    @pytest.mark.asyncio
    async def test_ing_valid_creates_artifact_and_observation(self) -> None:
        service, repo = _ingest_service()
        obs = _obs("crawl-1", 1, "hello")
        result = await service.ingest(obs)
        assert result.artifact is not None
        assert result.created_observation is True
        assert repo.artifacts
        assert repo.observations
        # normalized observation references the artifact.
        persisted = next(iter(repo.observations.values()))
        assert persisted.artifact_id == result.artifact.artifact_id

    @pytest.mark.asyncio
    async def test_ing_dedup_reuses_artifact_distinct_provenance(self) -> None:
        service, repo = _ingest_service()
        a = await service.ingest(_obs("crawl-1", 1, "same"))
        b = await service.ingest(_obs("crawl-1", 2, "same"))
        assert a.artifact is not None and b.artifact is not None
        # Same physical bytes -> same artifact record reused (dedup), not dup.
        assert b.deduplicated is True
        assert len(repo.artifacts) == 1
        # Two distinct provenance observations retained.
        assert len(repo.observations) == 2
        assert a.artifact.artifact_id == b.artifact.artifact_id

    @pytest.mark.asyncio
    async def test_ing_idempotent_provenance_no_duplicate_observation(self) -> None:
        service, repo = _ingest_service()
        await service.ingest(_obs("crawl-1", 5, "text"))
        result = await service.ingest(_obs("crawl-1", 5, "text"))
        # Same provenance identity retried -> created_observation False.
        assert result.created_observation is False
        assert len(repo.observations) == 1

    @pytest.mark.asyncio
    async def test_ing_no_text_no_artifact(self) -> None:
        service, repo = _ingest_service()
        obs = _obs("crawl-1", 1, "")
        result = await service.ingest(obs)
        assert result.artifact is None
        assert result.created_observation is True
        assert not repo.artifacts
