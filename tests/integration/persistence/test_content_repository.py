# SPDX-License-Identifier: AGPL-3.0-only
"""P-series: real PostgreSQL content repository round-trips (PR 8).

Exercises the production path ``PostgresContentRepository -> stored function
-> PostgreSQL`` for content artifacts and normalized observations, including
deduplicated artifact reuse and immutable multi-observation provenance.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from darkula.app.object_store import ContentHash, ContentType
from darkula.app.persistence import (
    ConflictError,
    DarkulaSpi,
    IntegrityError,
)
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.identifiers import ContentArtifactId, NormalizedContentId, ObjectKey

pytestmark = pytest.mark.integration

_MOMENT = datetime(2025, 3, 1, tzinfo=UTC)


def _artifact(digest: str = "a" * 64) -> ContentArtifact:
    return ContentArtifact(
        artifact_id=ContentArtifactId.generate(),
        object_key=ObjectKey(f"sha256/{digest[:2]}/{digest}"),
        content_type=ContentType("text/plain"),
        size_bytes=len(digest),
        content_hash=ContentHash(algorithm="sha-256", digest_hex=digest),
        artifact_kind=ArtifactKind.NORMALIZED_TEXT,
        completeness=ArtifactCompleteness.COMPLETE,
        created_at=_MOMENT,
    )


def _observation(
    *,
    request_id: str,
    index: int,
    artifact_id: ContentArtifactId | None,
    digest: str = "b" * 64,
) -> NormalizedContent:
    return NormalizedContent(
        content_id=NormalizedContentId.generate(),
        artifact_id=artifact_id,
        source_uri=f"http://blackgate.example.test/thread/{index}",
        observed_at=_MOMENT,
        crawl_request_id=request_id,
        observation_index=index,
        normalization_version="text-v1",
        completeness=ArtifactCompleteness.SAMPLE,
        title=f"Thread {index}",
        text="bounded normalized preview",
        content_type=ContentType("text/plain"),
        content_hash=ContentHash(algorithm="sha-256", digest_hex=digest),
        structural_metadata={"depth": str(index)},
    )


class TestArtifactPersistence:
    """P1/P8: create/get/find artifact metadata round-trip."""

    @pytest.mark.asyncio
    async def test_p1_create_and_get_artifact(self, spi: DarkulaSpi) -> None:
        artifact = _artifact()
        async with spi.unit_of_work() as uow:
            await uow.content.create_artifact(artifact)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            found = await uow.content.get_artifact(artifact.artifact_id)
        assert found is not None
        assert found.object_key == artifact.object_key
        assert found.content_hash == artifact.content_hash
        assert found.completeness is ArtifactCompleteness.COMPLETE

    @pytest.mark.asyncio
    async def test_p1_find_by_representation(self, spi: DarkulaSpi) -> None:
        artifact = _artifact(digest="c" * 64)
        async with spi.unit_of_work() as uow:
            await uow.content.create_artifact(artifact)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            found = await uow.content.find_artifact_by_representation(
                content_hash=artifact.content_hash,  # type: ignore[arg-type]
                kind=ArtifactKind.NORMALIZED_TEXT,
                completeness=ArtifactCompleteness.COMPLETE,
            )
        assert found is not None
        assert found.artifact_id == artifact.artifact_id

    @pytest.mark.asyncio
    async def test_p1_duplicate_artifact_conflict(self, spi: DarkulaSpi) -> None:
        artifact = _artifact()
        async with spi.unit_of_work() as uow:
            await uow.content.create_artifact(artifact)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.content.create_artifact(artifact)
            await uow.rollback()


class TestObservationPersistence:
    """P2-P4, P10-P12: observation round-trip, immutability, idempotency."""

    @pytest.mark.asyncio
    async def test_p2_create_and_get_observation(self, spi: DarkulaSpi) -> None:
        artifact = _artifact(digest="d" * 64)
        obs = _observation(
            request_id="crawl-1", index=1, artifact_id=artifact.artifact_id
        )
        async with spi.unit_of_work() as uow:
            await uow.content.create_artifact(artifact)
            await uow.content.create_observation(obs)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            found = await uow.content.get_observation(obs.content_id)
        assert found is not None
        assert found.source_uri == obs.source_uri
        assert found.observation_index == obs.observation_index
        assert found.crawl_request_id == "crawl-1"
        assert found.artifact_id == artifact.artifact_id
        assert found.normalization_version == "text-v1"

    @pytest.mark.asyncio
    async def test_p3_two_observations_same_artifact(self, spi: DarkulaSpi) -> None:
        artifact = _artifact(digest="e" * 64)
        obs_a = _observation(
            request_id="crawl-1", index=1, artifact_id=artifact.artifact_id
        )
        obs_b = _observation(
            request_id="crawl-1", index=2, artifact_id=artifact.artifact_id
        )
        async with spi.unit_of_work() as uow:
            await uow.content.create_artifact(artifact)
            await uow.content.create_observation(obs_a)
            await uow.content.create_observation(obs_b)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            a = await uow.content.get_observation(obs_a.content_id)
            b = await uow.content.get_observation(obs_b.content_id)
        # One artifact referenced by two distinct provenance observations.
        assert a is not None and b is not None
        assert a.artifact_id == b.artifact_id == artifact.artifact_id
        assert a.content_id != b.content_id

    @pytest.mark.asyncio
    async def test_p4_same_uri_two_times_two_observations(
        self, spi: DarkulaSpi
    ) -> None:
        # Same logical URI observed at two times (different crawl / index)
        # remains two immutable observations, never an upsert.
        obs_a = _observation(request_id="crawl-1", index=1, artifact_id=None)
        obs_b = _observation(
            request_id="crawl-2",
            index=1,
            artifact_id=None,
            digest="f" * 64,
        )
        # Force the same source_uri for both.
        obs_b = NormalizedContent(
            **{
                **{
                    k: getattr(obs_b, k)
                    for k in (
                        "content_id",
                        "artifact_id",
                        "observed_at",
                        "crawl_request_id",
                        "observation_index",
                        "normalization_version",
                        "completeness",
                        "title",
                        "text",
                        "content_type",
                        "content_hash",
                        "structural_metadata",
                    )
                },
                "source_uri": obs_a.source_uri,
            }
        )
        async with spi.unit_of_work() as uow:
            await uow.content.create_observation(obs_a)
            await uow.content.create_observation(obs_b)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            a = await uow.content.get_observation(obs_a.content_id)
            b = await uow.content.get_observation(obs_b.content_id)
        assert a is not None and b is not None
        assert a.source_uri == b.source_uri
        assert a.content_id != b.content_id
        assert a.crawl_request_id != b.crawl_request_id

    @pytest.mark.asyncio
    async def test_p10_structural_metadata_round_trip(self, spi: DarkulaSpi) -> None:
        obs = _observation(request_id="crawl-1", index=5, artifact_id=None)
        async with spi.unit_of_work() as uow:
            await uow.content.create_observation(obs)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            found = await uow.content.get_observation(obs.content_id)
        assert found is not None and found.structural_metadata is not None
        assert found.structural_metadata["depth"] == "5"

    @pytest.mark.asyncio
    async def test_p11_sample_completeness_round_trip(self, spi: DarkulaSpi) -> None:
        obs = _observation(request_id="crawl-1", index=6, artifact_id=None)
        async with spi.unit_of_work() as uow:
            await uow.content.create_observation(obs)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            found = await uow.content.get_observation(obs.content_id)
        assert found is not None
        assert found.completeness is ArtifactCompleteness.SAMPLE

    @pytest.mark.asyncio
    async def test_p12_normalization_version_round_trip(self, spi: DarkulaSpi) -> None:
        obs = _observation(request_id="crawl-1", index=7, artifact_id=None)
        async with spi.unit_of_work() as uow:
            await uow.content.create_observation(obs)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            found = await uow.content.get_observation(obs.content_id)
        assert found is not None
        assert found.normalization_version == "text-v1"

    @pytest.mark.asyncio
    async def test_p2_duplicate_provenance_idempotent_conflict(
        self, spi: DarkulaSpi
    ) -> None:
        obs = _observation(request_id="crawl-1", index=9, artifact_id=None)
        async with spi.unit_of_work() as uow:
            await uow.content.create_observation(obs)
            await uow.commit()
        duplicate = _observation(request_id="crawl-1", index=9, artifact_id=None)
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.content.create_observation(duplicate)
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_p5_unknown_artifact_rejected(self, spi: DarkulaSpi) -> None:
        obs = _observation(
            request_id="crawl-1",
            index=10,
            artifact_id=ContentArtifactId.generate(),  # never created
        )
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.content.create_observation(obs)
            await uow.rollback()
