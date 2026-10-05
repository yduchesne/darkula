# SPDX-License-Identifier: AGPL-3.0-only
"""PR 11 failure/concurrency slices against real PostgreSQL + ObjectStore.

Missing object and hash mismatch fail typed before extraction and persist
nothing; two concurrent same-content extractions resolve to exactly one
durable result. Extraction itself is real (never faked).
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
    ExtractionInputUnavailableError,
    ExtractionIntegrityError,
    deterministic_observables_profile,
)
from darkula.app.object_store import ContentHash, ContentType, ObjectStore
from darkula.app.persistence import DarkulaSpi
from darkula.config.settings import DatabaseSettings
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.identifiers import (
    ContentArtifactId,
    NormalizedContentId,
    ObjectKey,
)
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi

pytestmark = pytest.mark.integration

_MOMENT = datetime(2026, 4, 1, tzinfo=UTC)


def _reset(database_settings: DatabaseSettings) -> None:
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


async def _iter_bytes(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _object_key(text: str) -> tuple[ObjectKey, ContentHash, int]:
    payload = text.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return (
        ObjectKey(f"sha256/{digest[:2]}/{digest}"),
        ContentHash(algorithm="sha-256", digest_hex=digest),
        len(payload),
    )


async def _seed(
    spi: DarkulaSpi,
    *,
    text: str,
    request_id: str,
    index: int,
) -> NormalizedContent:
    key, content_hash, size = _object_key(text)
    artifact = ContentArtifact(
        artifact_id=ContentArtifactId.generate(),
        object_key=key,
        content_type=ContentType("text/plain"),
        size_bytes=size,
        content_hash=content_hash,
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
        content_type=ContentType("text/plain"),
        content_hash=content_hash,
    )
    async with spi.unit_of_work() as uow:
        await uow.content.create_artifact(artifact)
        await uow.content.create_observation(observation)
        await uow.commit()
    return observation


def _service(spi: DarkulaSpi, store: ObjectStore) -> DeterministicExtractionService:
    return DeterministicExtractionService(
        spi=spi,
        object_store=store,
        profile=deterministic_observables_profile(max_entities=5000),
    )


class TestFailureSlices:
    @pytest.mark.asyncio
    async def test_missing_object_typed_failure_no_persistence(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        observation = await _seed(
            spi,
            text="contact bob@example.com",
            request_id="crawl-missing",
            index=1,
        )
        # Fresh store does not contain the object.
        service = _service(spi, LocalFileObjectStore(tmp_path / "objects"))
        with pytest.raises(ExtractionInputUnavailableError):
            await service.extract(observation.content_id)
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM extraction_result")
                results = cur.fetchone()
                cur.execute("SELECT count(*) FROM extracted_entity")
                entities = cur.fetchone()
        finally:
            conn.close()
        assert results is not None and results[0] == 0
        assert entities is not None and entities[0] == 0

    @pytest.mark.asyncio
    async def test_hash_mismatch_integrity_failure_no_persistence(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        expected = "contact bob@example.com"
        observation = await _seed(spi, text=expected, request_id="crawl-hash", index=2)
        store = LocalFileObjectStore(tmp_path / "objects")
        # Store different bytes under the persisted object key.
        key, _, _ = _object_key(expected)
        await store.put(key=key, data=_iter_bytes(b"different bytes"))
        service = _service(spi, store)
        with pytest.raises(ExtractionIntegrityError):
            await service.extract(observation.content_id)
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM extraction_result")
                results = cur.fetchone()
                cur.execute("SELECT count(*) FROM extracted_entity")
                entities = cur.fetchone()
        finally:
            conn.close()
        assert results is not None and results[0] == 0
        assert entities is not None and entities[0] == 0


class TestConcurrentExtractionSlice:
    @pytest.mark.asyncio
    async def test_concurrent_same_content_one_durable_result(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        _reset(database_settings)
        text = "bob@example.com and 10.0.0.1"
        observation = await _seed(spi, text=text, request_id="crawl-race", index=3)
        store = LocalFileObjectStore(tmp_path / "objects")
        key, _, _ = _object_key(text)
        await store.put(key=key, data=_iter_bytes(text.encode("utf-8")))
        service = _service(spi, store)

        first, second = await asyncio.gather(
            service.extract(observation.content_id),
            service.extract(observation.content_id),
        )
        assert first.result.result_id == second.result.result_id
        assert {first.created_result, second.created_result} == {True, False}
        assert len(first.entities) == len(second.entities)

        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM extraction_result")
                results = cur.fetchone()
                cur.execute("SELECT count(*) FROM extracted_entity")
                entities = cur.fetchone()
        finally:
            conn.close()
        assert results is not None and results[0] == 1
        assert entities is not None
        assert entities[0] == len(first.entities)
