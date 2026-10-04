# SPDX-License-Identifier: AGPL-3.0-only
"""Offline unit tests for the PostgreSQL content repository (P-series).

Verifies that the content repository (a) issues only stored-function
invocation strings (the SQL-boundary guard covers the whole package too),
(b) marshals parameters and maps results exactly, and (c) surfaces controlled
conflicts as typed bounded errors — all without PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self

import pytest

from darkula.app.object_store import ContentHash, ContentType
from darkula.app.persistence import ConflictError, IntegrityError, PersistenceError
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
from darkula.infrastructure.persistence.postgresql.content_repository import (
    PostgresContentRepository,
)

_MOMENT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)


class _RecordingCursor:
    """Async cursor recording the last SQL + params and serving queued rows."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.executed_sql: list[str] = []
        self.executed_params: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed_sql.append(sql)
        self.executed_params.append(params or ())

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Scope:
    """Deterministic ExecutionScope under test (no real DB connection)."""

    def __init__(self, cursor: _RecordingCursor, *, closed: bool = False) -> None:
        self.cursor = cursor
        self.closed = closed
        self.exception: BaseException | None = None

    def ensure_open(self) -> None:
        if self.closed:
            raise PersistenceError("this unit of work is closed")

    def connection(self) -> Any:
        self.ensure_open()
        if self.exception is not None:
            raise self.exception

        class _Conn:
            def cursor(self) -> _RecordingCursor:
                return cur

        cur = self.cursor
        return _Conn()


def _artifact() -> ContentArtifact:
    return ContentArtifact(
        artifact_id=ContentArtifactId.generate(),
        object_key=ObjectKey("sha256/ab/ab"),
        content_type=ContentType("text/plain"),
        size_bytes=4,
        content_hash=ContentHash(algorithm="sha-256", digest_hex="a" * 64),
        artifact_kind=ArtifactKind.NORMALIZED_TEXT,
        completeness=ArtifactCompleteness.COMPLETE,
        created_at=_MOMENT,
    )


def _observation() -> NormalizedContent:
    return NormalizedContent(
        content_id=NormalizedContentId.generate(),
        artifact_id=ContentArtifactId.generate(),
        source_uri="http://blackgate.example.test/thread/1",
        observed_at=_MOMENT,
        crawl_request_id="crawl-1",
        observation_index=2,
        normalization_version="text-v1",
        completeness=ArtifactCompleteness.SAMPLE,
        title="A title",
        text="hello world",
        content_type=ContentType("text/plain"),
        content_hash=ContentHash(algorithm="sha-256", digest_hex="b" * 64),
        structural_metadata={"lang": "en"},
    )


# (algorithm, digest, kind, completeness)
_ARTIFACT_ROW = (
    "1a2b3c4d-0000-0000-0000-000000000001",
    "sha256/ab/ab",
    "text/plain",
    4,
    "sha-256",
    "a" * 64,
    "NORMALIZED_TEXT",
    "COMPLETE",
    _MOMENT,
)


class TestArtifactRepository:
    """P1/P7: create/get/find artifact metadata with only function calls."""

    @pytest.mark.asyncio
    async def test_p1_create_round_trip(self) -> None:
        artifact = _artifact()
        cursor = _RecordingCursor(rows=[("added",)])
        repo = PostgresContentRepository(_Scope(cursor))
        await repo.create_artifact(artifact)
        assert cursor.executed_sql == [
            "SELECT * FROM content_artifact_create_v1("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        ]
        # Only %s placeholders; values are bound as separate parameters.
        assert cursor.executed_params[0][0] == str(artifact.artifact_id)
        assert cursor.executed_params[0][1] == "sha256/ab/ab"

    @pytest.mark.asyncio
    async def test_p1_duplicate_artifact_conflict(self) -> None:
        cursor = _RecordingCursor(rows=[("duplicate",)])
        repo = PostgresContentRepository(_Scope(cursor))
        with pytest.raises(ConflictError):
            await repo.create_artifact(_artifact())

    @pytest.mark.asyncio
    async def test_p7_sql_is_only_stored_function_invocation(self) -> None:
        cursor = _RecordingCursor(rows=[_ARTIFACT_ROW])
        repo = PostgresContentRepository(_Scope(cursor))
        # find_artifact_by_representation invokes content_artifact_find_v1
        found = await repo.find_artifact_by_representation(
            content_hash=ContentHash(algorithm="sha-256", digest_hex="a" * 64),
            kind=ArtifactKind.NORMALIZED_TEXT,
            completeness=ArtifactCompleteness.COMPLETE,
        )
        assert found is not None
        assert found.object_key.value == "sha256/ab/ab"
        assert found.size_bytes == 4

    @pytest.mark.asyncio
    async def test_get_artifact_missing_returns_none(self) -> None:
        repo = PostgresContentRepository(_Scope(_RecordingCursor(rows=[])))
        assert await repo.get_artifact(ContentArtifactId.generate()) is None


class TestObservationRepository:
    """P2-P4, P10-P12: create/get observations and idempotency semantics."""

    @pytest.mark.asyncio
    async def test_p2_create_observation_round_trip(self) -> None:
        observation = _observation()
        cursor = _RecordingCursor(rows=[("added",)])
        repo = PostgresContentRepository(_Scope(cursor))
        await repo.create_observation(observation)
        assert cursor.executed_sql == [
            "SELECT * FROM normalized_content_create_v1("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        ]
        params = cursor.executed_params[0]
        assert params[0] == str(observation.content_id)
        assert params[1] == str(observation.artifact_id)
        assert params[3] == "A title"

    @pytest.mark.asyncio
    async def test_p2_duplicate_provenance_conflict(self) -> None:
        cursor = _RecordingCursor(rows=[("duplicate_provenance",)])
        repo = PostgresContentRepository(_Scope(cursor))
        with pytest.raises(ConflictError, match="provenance"):
            await repo.create_observation(_observation())

    @pytest.mark.asyncio
    async def test_p5_unknown_artifact_integrity(self) -> None:
        cursor = _RecordingCursor(rows=[("unknown_artifact",)])
        repo = PostgresContentRepository(_Scope(cursor))
        with pytest.raises(IntegrityError, match="artifact"):
            await repo.create_observation(_observation())

    @pytest.mark.asyncio
    async def test_get_observation_missing_returns_none(self) -> None:
        repo = PostgresContentRepository(_Scope(_RecordingCursor(rows=[])))
        assert await repo.get_observation(NormalizedContentId.generate()) is None

    @pytest.mark.asyncio
    async def test_mapping_round_trip(self) -> None:
        row = (
            "11111111-0000-0000-0000-000000000001",
            "1a2b3c4d-0000-0000-0000-000000000001",
            "http://blackgate.example.test/thread/1",
            "A title",
            "hello world",
            "text/plain",
            _MOMENT,
            "crawl-1",
            2,
            "text-v1",
            "sha-256",
            "b" * 64,
            "SAMPLE",
            {"lang": "en"},
            _MOMENT,
        )
        repo = PostgresContentRepository(_Scope(_RecordingCursor(rows=[row])))
        content = await repo.get_observation(
            NormalizedContentId.from_str("11111111-0000-0000-0000-000000000001")
        )
        assert content is not None
        assert content.source_uri == "http://blackgate.example.test/thread/1"
        assert content.observation_index == 2
        assert content.content_hash == ContentHash(
            algorithm="sha-256", digest_hex="b" * 64
        )
        assert content.structural_metadata == {"lang": "en"}


class TestClosedScope:
    """Repository use after a closed unit of work fails boundedly."""

    @pytest.mark.asyncio
    async def test_closed_scope_fails_closed(self) -> None:
        repo = PostgresContentRepository(_Scope(_RecordingCursor(), closed=True))
        with pytest.raises(PersistenceError, match="closed"):
            await repo.create_artifact(_artifact())
