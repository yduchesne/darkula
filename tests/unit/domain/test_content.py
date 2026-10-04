# SPDX-License-Identifier: AGPL-3.0-only
"""Domain validation tests for the PR 8 content model (CV-*)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from darkula.app.object_store import ContentHash, ContentType
from darkula.domain.content import (
    MAX_ARTIFACT_BYTES,
    MAX_URI_LENGTH,
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    ContentObservation,
    NormalizedContent,
)
from darkula.domain.identifiers import (
    ContentArtifactId,
    NormalizedContentId,
    ObjectKey,
)


def _utc() -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC)


def _observation(**kw: object) -> ContentObservation:
    defaults: dict[str, object] = {
        "crawl_request_id": "crawl-1",
        "source_uri": "http://blackgate.example.test/thread/1",
        "observed_at": _utc(),
        "observation_index": 1,
        "text": "hello",
    }
    defaults.update(kw)
    return ContentObservation(**defaults)  # type: ignore[arg-type]


class TestContentObservation:
    """CV-01/02/03: input DTO bounds and completeness explicitness."""

    def test_missing_uri_rejected(self) -> None:
        with pytest.raises(ValueError, match="source_uri"):
            _observation(source_uri="  ")

    def test_uri_control_characters_rejected(self) -> None:
        with pytest.raises(ValueError, match="control"):
            _observation(source_uri="http://x.test/\x00page")

    def test_naive_observed_at_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _observation(observed_at=datetime(2024, 1, 1))

    def test_body_bytes_requires_artifact_kind(self) -> None:
        with pytest.raises(ValueError, match="artifact_kind"):
            _observation(body_bytes=b"x")

    def test_oversized_body_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"MAX_ARTIFACT_BYTES|exceed"):
            _observation(
                body_bytes=b"x" * (MAX_ARTIFACT_BYTES + 1),
                artifact_kind=ArtifactKind.DOWNLOAD_SAMPLE,
            )

    def test_oversized_uri_rejected(self) -> None:
        with pytest.raises(ValueError, match="source_uri"):
            _observation(source_uri="http://x.test/" + "a" * MAX_URI_LENGTH)

    def test_secret_like_metadata_rejected(self) -> None:
        with pytest.raises(ValueError, match="secret"):
            _observation(structural_metadata={"session_token": "abc"})

    def test_metadata_keys_and_values_bounded(self) -> None:
        with pytest.raises(ValueError, match="keys and values"):
            _observation(structural_metadata={"a": 1})


class TestContentArtifact:
    """CV-04/05: artifact identity is distinct from key and hash."""

    def test_artifact_requires_nonnegative_size(self) -> None:
        with pytest.raises(ValueError, match="size_bytes"):
            ContentArtifact(
                artifact_id=ContentArtifactId.generate(),
                object_key=ObjectKey("sha256/ab/ab"),
                content_type=None,
                size_bytes=-1,
                content_hash=None,
                artifact_kind=ArtifactKind.NORMALIZED_TEXT,
                completeness=ArtifactCompleteness.COMPLETE,
                created_at=_utc(),
            )

    def test_hash_algorithm_is_darkula_representation(self) -> None:
        with pytest.raises(ValueError, match="algorithm"):
            ContentArtifact(
                artifact_id=ContentArtifactId.generate(),
                object_key=ObjectKey("sha256/ab/ab"),
                content_type=None,
                size_bytes=1,
                content_hash=ContentHash(algorithm="md5", digest_hex="0" * 32),
                artifact_kind=ArtifactKind.NORMALIZED_TEXT,
                completeness=ArtifactCompleteness.COMPLETE,
                created_at=_utc(),
            )


class TestNormalizedContent:
    """CV-06: normalized content bounds and provenance."""

    def test_valid_multilingual_text_preserved(self) -> None:
        content = NormalizedContent(
            content_id=NormalizedContentId.generate(),
            source_uri="http://x/1",
            observed_at=_utc(),
            crawl_request_id="crawl-1",
            observation_index=1,
            normalization_version="text-v1",
            completeness=ArtifactCompleteness.SAMPLE,
            text="您好 世界 café",
        )
        assert content.text == "您好 世界 café"

    def test_title_bounded(self) -> None:
        with pytest.raises(ValueError, match="title"):
            NormalizedContent(
                content_id=NormalizedContentId.generate(),
                source_uri="http://x/1",
                observed_at=_utc(),
                crawl_request_id="crawl-1",
                observation_index=1,
                normalization_version="text-v1",
                completeness=ArtifactCompleteness.SAMPLE,
                title="t" * 600,
            )

    def test_content_type_accepted(self) -> None:
        content = NormalizedContent(
            content_id=NormalizedContentId.generate(),
            source_uri="http://x/1",
            observed_at=_utc(),
            crawl_request_id="crawl-1",
            observation_index=1,
            normalization_version="text-v1",
            completeness=ArtifactCompleteness.SAMPLE,
            content_type=ContentType("text/plain"),
        )
        assert content.content_type == ContentType("text/plain")
