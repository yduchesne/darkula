# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 11 canonical vertical slice: real crawler -> ingest -> extract.

real CrawlerController -> real PodmanSandbox -> Playwright/Chromium -> HTTP
-> Fake World -> production mapper -> production normalizer ->
ObjectStore normalized representation -> real PostgreSQL NormalizedContent /
ContentArtifact -> real DeterministicExtractionService -> real deterministic
extractors -> real PostgreSQL ExtractionResult / ExtractedEntity.

The extraction architecture is never faked: only the external world (Fake
World) is synthetic. No LLM is involved.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from darkula.app.artifacts import ArtifactStorageService
from darkula.app.content import ContentIngestService
from darkula.app.extraction import (
    DeterministicExtractionService,
    deterministic_observables_profile,
)
from darkula.app.normalization import DeterministicContentNormalizer
from darkula.config.settings import DatabaseSettings
from darkula.crawler import CrawlerController, CrawlStatus
from darkula.crawler.mapping import page_observation_to_content
from darkula.domain.extraction import EntityType
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from tests.integration.conftest import _raw_connect
from tests.integration.crawler.helpers import crawl_request, truth_leaks

pytestmark = pytest.mark.integration


def _obs_at() -> datetime:
    return datetime(2026, 4, 1, tzinfo=UTC)


def _reset_extraction_tables(database_settings: DatabaseSettings) -> None:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE extracted_entity, extraction_result, "
                "normalized_content, content_artifact CASCADE"
            )
        conn.commit()
    finally:
        conn.close()


async def _canonical_text(
    spi: PostgresDarkulaSpi, store: LocalFileObjectStore, content_id: object
) -> str:
    from darkula.domain.identifiers import NormalizedContentId

    assert isinstance(content_id, NormalizedContentId)
    async with spi.unit_of_work() as uow:
        content = await uow.content.get_observation(content_id)
        assert content is not None and content.artifact_id is not None
        artifact = await uow.content.get_artifact(content.artifact_id)
    assert artifact is not None
    reader = await store.get(artifact.object_key)
    payload = b"".join([chunk async for chunk in reader])
    assert hashlib.sha256(payload).hexdigest() == artifact.content_hash.digest_hex  # type: ignore[union-attr]
    return payload.decode("utf-8")


class TestExtractionVerticalSlice:
    @pytest.mark.asyncio
    async def test_real_crawler_ingest_extract_slice(
        self,
        controller: CrawlerController,
        fake_world_url: str,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset_extraction_tables(database_settings)
        store = LocalFileObjectStore(tmp_path / "objects")
        normalizer = DeterministicContentNormalizer(
            artifact_service=ArtifactStorageService(object_store=store)
        )
        ingest = ContentIngestService(normalizer=normalizer, spi=spi)
        extraction = DeterministicExtractionService(
            spi=spi,
            object_store=store,
            profile=deterministic_observables_profile(max_entities=5000),
        )

        result = await controller.crawl(
            crawl_request(
                fake_world_url,
                start_path="/thread/thr-collector-samples",
                max_pages=4,
                max_requests=40,
                max_depth=1,
                timeout_seconds=180.0,
            )
        )
        assert result.status is CrawlStatus.COMPLETED, result.reason
        assert truth_leaks(result) == []
        assert result.pages, "real crawler produced no page observations"

        ingested = []
        for index, page in enumerate(result.pages, start=1):
            observation = page_observation_to_content(
                page,
                crawl_request_id=result.request_id,
                observed_at=_obs_at(),
                observation_index=index,
            )
            ingested.append(await ingest.ingest(observation))
        assert ingested

        # Extract every ingested observation and keep the one carrying the
        # synthetic observables (identified by the EMAIL occurrence).
        target = None
        for item in ingested:
            extracted = await extraction.extract(item.normalized_content.content_id)
            if any(e.entity_type is EntityType.EMAIL for e in extracted.entities):
                target = (item, extracted)
                break
        assert target is not None, "no ingested page contained the observables"
        item, extracted = target
        assert extracted.created_result is True

        # 1) Real ingestion + persisted normalized content.
        assert item.normalized_content.artifact_id is not None
        text = await _canonical_text(spi, store, item.normalized_content.content_id)

        # 2) Expected entity types (the URL yields URL + its DOMAIN occurrence).
        entity_types = {e.entity_type for e in extracted.entities}
        assert EntityType.EMAIL in entity_types
        assert EntityType.DOMAIN in entity_types
        assert EntityType.URL in entity_types
        assert EntityType.IP_ADDRESS in entity_types
        assert EntityType.HASH in entity_types

        # 3) Every raw value is exactly the canonical-text slice it claims.
        for entity in extracted.entities:
            span = entity.source_span
            assert text[span.start : span.end] == entity.raw_value

        # 4) Normalized values follow the documented v1 rules.
        normalized = {e.normalized_value for e in extracted.entities}
        assert "collector@collector-samples.example.test" in normalized
        assert "collector-samples.example.test" in normalized
        assert "https://collector-samples.example.test/report?id=42" in normalized
        assert "203.0.113.77" in normalized
        assert "2001:db8::c0de" in normalized
        assert (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            in normalized
        )

        # 5) Extractor name/version and the exact manifest are persisted.
        for entity in extracted.entities:
            assert entity.extractor.version == "v1"
        assert [h.name for h in extracted.result.extractor_manifest] == [
            "domain",
            "email",
            "hash",
            "ip",
            "url",
        ]

        # 6) Deterministic ordering.
        order_keys = [
            (
                e.source_span.start,
                e.source_span.end,
                e.entity_type.value,
                e.normalized_value,
                e.extractor.name,
            )
            for e in extracted.entities
        ]
        assert order_keys == sorted(order_keys)

        # 7) Duplicate value at different spans is preserved (the host twice).
        host_spans = [
            e.source_span.start
            for e in extracted.entities
            if e.normalized_value == "collector-samples.example.test"
        ]
        assert len(host_spans) >= 2
        assert len(set(host_spans)) == len(host_spans)

        # 8) Replay returns/reuses the same durable result; no duplicate rows.
        replay = await extraction.extract(item.normalized_content.content_id)
        assert replay.created_result is False
        assert replay.result.result_id == extracted.result.result_id
        assert len(replay.entities) == len(extracted.entities)

        # 9) No relationship or SourceAssessment behavior was created.
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM source_assessment")
                assessments = cur.fetchone()
                cur.execute("SELECT count(*) FROM extracted_entity")
                entity_rows = cur.fetchone()
        finally:
            conn.close()
        assert assessments is not None and assessments[0] == 0
        assert entity_rows is not None
        assert entity_rows[0] == len(extracted.entities)
