# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted-side crawler page -> content observation mapping (PR 8, M-*)."""

from __future__ import annotations

from datetime import UTC, datetime

from darkula.crawler import CrawlPageObservation
from darkula.crawler.mapping import page_observation_to_content
from darkula.domain.content import ArtifactCompleteness, ArtifactKind

_MOMENT = datetime(2025, 1, 1, tzinfo=UTC)


def _page(**kw: object) -> CrawlPageObservation:
    defaults: dict[str, object] = {
        "url": "http://blackgate.example.test/thread/1?page=2",
        "depth": 2,
        "http_status": 200,
        "rendered": True,
        "title": "  Threat thread  ",
        "text_excerpt": "  bounded excerpt  ",
    }
    defaults.update(kw)
    return CrawlPageObservation(**defaults)  # type: ignore[arg-type]


class TestMapPageToObservation:
    """M-01..M-05: bounded page -> normalization input semantics."""

    def test_m01_maps_fields(self) -> None:
        obs = page_observation_to_content(
            _page(),
            crawl_request_id="crawl-1",
            observed_at=_MOMENT,
            observation_index=3,
        )
        assert obs.crawl_request_id == "crawl-1"
        assert obs.observation_index == 3
        # URL (with pagination query) is preserved as observed.
        assert obs.source_uri == "http://blackgate.example.test/thread/1?page=2"
        assert obs.title == "Threat thread"
        assert obs.text == "bounded excerpt"

    def test_m02_excerpt_is_sample_never_complete(self) -> None:
        obs = page_observation_to_content(
            _page(), crawl_request_id="c", observed_at=_MOMENT, observation_index=1
        )
        assert obs.completeness is ArtifactCompleteness.SAMPLE
        assert obs.artifact_kind is ArtifactKind.NORMALIZED_TEXT

    def test_m03_empty_excerpt_yields_text_none(self) -> None:
        obs = page_observation_to_content(
            _page(text_excerpt="   "),
            crawl_request_id="c",
            observed_at=_MOMENT,
            observation_index=1,
        )
        assert obs.text is None

    def test_m04_bounded_structural_metadata(self) -> None:
        obs = page_observation_to_content(
            _page(http_status=404, depth=1, rendered=False),
            crawl_request_id="c",
            observed_at=_MOMENT,
            observation_index=1,
        )
        assert obs.structural_metadata == {
            "http_status": "404",
            "depth": "1",
            "rendered": "false",
        }

    def test_m05_no_secret_leak_in_metadata(self) -> None:
        # Mapping never copies cookies/session/credentials; the metadata
        # allowlist is inherently safe.
        obs = page_observation_to_content(
            _page(), crawl_request_id="c", observed_at=_MOMENT, observation_index=1
        )
        structural = obs.structural_metadata
        assert structural is not None
        assert "password" not in structural
        assert "cookie" not in structural
