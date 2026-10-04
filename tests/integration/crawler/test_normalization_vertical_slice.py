# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 8 canonical vertical slice (section 17): real crawler -> normalization
-> ObjectStore -> PostgreSQL.

real CrawlerController -> real PodmanSandbox -> Playwright/Chromium -> HTTP
-> Fake World -> production mapper -> production normalizer -> real
LocalFileObjectStore -> real PostgreSQL repositories/stored functions ->
reload of NormalizedContent + ContentArtifact.

Exercises deduplication that preserves independent provenance, bounded
SAMPLE semantics, and truthful non-leakage -- all on the real PR 7 crawler
path (never FakeCrawler) and without live illicit content.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from darkula.app.artifacts import ArtifactStorageService, content_addressed_key
from darkula.app.content import ContentIngestService
from darkula.app.normalization import DeterministicContentNormalizer
from darkula.app.object_store import (
    ObjectStoreUnavailableError,
    StoredObject,
)
from darkula.config.settings import DatabaseSettings
from darkula.crawler import CrawlerController, CrawlStatus
from darkula.crawler.mapping import page_observation_to_content
from darkula.domain.content import (
    ArtifactCompleteness,
    ContentObservation,
)
from darkula.domain.identifiers import ObjectKey
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from tests.integration.conftest import _raw_connect
from tests.integration.crawler.helpers import crawl_request, truth_leaks

pytestmark = pytest.mark.integration


def _obs_at() -> datetime:
    """Fixed provenance timestamp for this slice (deterministic)."""
    return datetime(2026, 1, 1, tzinfo=UTC)


def _reset_content_tables(database_settings: DatabaseSettings) -> None:
    """Test-only cleanup of the content tables (raw SQL setup/teardown)."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE normalized_content, content_artifact CASCADE")
        conn.commit()
    finally:
        conn.close()


def _ingest(
    store: LocalFileObjectStore, spi: PostgresDarkulaSpi
) -> ContentIngestService:
    normalizer = DeterministicContentNormalizer(
        artifact_service=ArtifactStorageService(object_store=store)
    )
    return ContentIngestService(normalizer=normalizer, spi=spi)


class TestNormalizationVerticalSlice:
    """Real-crawler normalization across ObjectStore + PostgreSQL."""

    @pytest.mark.asyncio
    async def test_canonical_blackgate_normalization_slice(
        self,
        controller: CrawlerController,
        fake_world_url: str,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset_content_tables(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        ingest = _ingest(store, spi)

        result = await controller.crawl(
            crawl_request(
                fake_world_url,
                start_path="/board/board-announcements",
                max_pages=12,
                max_requests=60,
                max_depth=2,
                timeout_seconds=180.0,
            )
        )
        assert result.status is CrawlStatus.COMPLETED, result.reason
        assert truth_leaks(result) == []

        pages = result.pages
        assert pages, "real crawler produced no page observations"

        ingested = []
        for index, page in enumerate(pages, start=1):
            obs = page_observation_to_content(
                page,
                crawl_request_id=result.request_id,
                observed_at=_obs_at(),
                observation_index=index,
            )
            ingested.append(await ingest.ingest(obs))

        # 1) A bounded page excerpt is normalized and stored as SAMPLE.
        assert ingested
        assert any(
            o.normalized_content.completeness is ArtifactCompleteness.SAMPLE
            for o in ingested
        ), "crawler excerpts must be SAMPLE (never complete originals)"

        # 2) Stored artifact bytes round-trip and SHA-256 equals exact bytes.
        first = next(o for o in ingested if o.artifact is not None)
        assert first.artifact is not None
        art = first.artifact
        assert art.content_hash is not None
        reader = await store.get(art.object_key)
        stored_bytes = b"".join([c async for c in reader])
        assert hashlib.sha256(stored_bytes).hexdigest() == art.content_hash.digest_hex
        assert art.object_key == content_addressed_key(art.content_hash)

        # 3) PostgreSQL persisted the artifact + observation; reload proves it.
        async with spi.unit_of_work() as uow:
            reloaded = await uow.content.get_observation(
                first.normalized_content.content_id
            )
        assert reloaded is not None
        assert reloaded.source_uri == first.normalized_content.source_uri
        assert reloaded.crawl_request_id == result.request_id
        assert reloaded.artifact_id == art.artifact_id

        # 4) Truth-only identifiers and the auth password never leak.
        assert truth_leaks(result) == []

    @pytest.mark.asyncio
    async def test_dedup_vertical_slice_preserves_provenance(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset_content_tables(database_settings)
        ingest = _ingest(LocalFileObjectStore(tmp_path / "objects"), spi)
        # Same normalized representation, two distinct provenance observations.
        obs_a = ContentObservation(
            crawl_request_id="crawl-A",
            source_uri="http://blackgate.example.test/thread/1",
            observed_at=_obs_at(),
            observation_index=1,
            text="identical excerpt bytes",
        )
        obs_b = ContentObservation(
            crawl_request_id="crawl-B",
            source_uri="http://blackgate.example.test/archive/thread/1",
            observed_at=_obs_at(),
            observation_index=1,
            text="identical excerpt bytes",
        )
        a = await ingest.ingest(obs_a)
        b = await ingest.ingest(obs_b)

        # One physical ObjectStore key.
        assert a.artifact is not None and b.artifact is not None
        assert a.artifact.object_key == b.artifact.object_key
        # Two provenance-bearing observations with distinct identities.
        assert a.normalized_content.content_id != b.normalized_content.content_id
        async with spi.unit_of_work() as uow:
            ra = await uow.content.get_observation(a.normalized_content.content_id)
            rb = await uow.content.get_observation(b.normalized_content.content_id)
        assert ra is not None and rb is not None
        assert ra.source_uri != rb.source_uri
        assert ra.crawl_request_id != rb.crawl_request_id

    @pytest.mark.asyncio
    async def test_changed_content_slice_same_uri_distinct_hash(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset_content_tables(database_settings)
        ingest = _ingest(LocalFileObjectStore(tmp_path / "objects"), spi)
        uri = "http://blackgate.example.test/thread/9"
        at = _obs_at()
        v1 = await ingest.ingest(
            ContentObservation(
                crawl_request_id="crawl-1",
                source_uri=uri,
                observed_at=at,
                observation_index=1,
                text="page version one",
            )
        )
        v2 = await ingest.ingest(
            ContentObservation(
                crawl_request_id="crawl-1",
                source_uri=uri,
                observed_at=at,
                observation_index=2,
                text="page version two",
            )
        )
        assert v1.artifact is not None and v2.artifact is not None
        # Same logical URI, changed bytes -> distinct representation/object.
        assert v1.normalized_content.content_hash != v2.normalized_content.content_hash
        assert v1.artifact.object_key != v2.artifact.object_key


class TestFailureSlice:
    """Failure before PostgreSQL persistence produces no false success."""

    @pytest.mark.asyncio
    async def test_object_store_failure_prevents_persistence(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        _reset_content_tables(database_settings)

        class _FailingStore(LocalFileObjectStore):
            async def stat(self, key: ObjectKey) -> StoredObject | None:
                raise ObjectStoreUnavailableError("unavailable")

        ingest = _ingest(_FailingStore(Path("/tmp/unused")), spi)
        obs = ContentObservation(
            crawl_request_id="crawl-x",
            source_uri="http://blackgate.example.test/thread/77",
            observed_at=_obs_at(),
            observation_index=1,
            text="should never be persisted",
        )
        with pytest.raises(ObjectStoreUnavailableError):
            await ingest.ingest(obs)

        # No structured DB record claims success.
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM normalized_content")
                obs_row = cur.fetchone()
                cur.execute("SELECT count(*) FROM content_artifact")
                art_row = cur.fetchone()
        finally:
            conn.close()
        assert obs_row is not None and obs_row[0] == 0
        assert art_row is not None and art_row[0] == 0
