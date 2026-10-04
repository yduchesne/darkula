# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted-side mapping from PR 7 crawler observations to PR 8 content.

PR 7 deliberately returns bounded page observations, never unrestricted raw
HTML/download bodies. This module maps those bounded observations into the
Darkula-owned :class:`~darkula.domain.content.ContentObservation` input DTO
(section 13 of the PR 8 plan): it imports no crawler-runtime implementation
classes and never weakens the sandbox output contract.

The bounded page excerpt is normalized as an observed text **SAMPLE** — never
as the complete original HTML. Its representation hash identifies the
normalized sample representation, not the unknown complete page body.
"""

from __future__ import annotations

from datetime import datetime

from darkula.app.object_store import ContentType
from darkula.crawler import CrawlPageObservation
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentObservation,
)

#: Rendered pages observed by the crawler are HTML media.
_PAGE_MEDIA_TYPE = ContentType("text/html")


def page_observation_to_content(
    observation: CrawlPageObservation,
    *,
    crawl_request_id: str,
    observed_at: datetime,
    observation_index: int,
) -> ContentObservation:
    """Map one bounded page observation into normalization input.

    The text excerpt is bounded hostile data (SAMPLE); the URL is preserved
    as observed (including query/pagination) and is provenance, never storage
    identity. ``observed_at`` is supplied by the caller (trusted-side wall
    clock) and does not affect the deterministic normalized representation.
    """
    excerpt = observation.text_excerpt.strip()
    return ContentObservation(
        crawl_request_id=crawl_request_id,
        source_uri=observation.url,
        observed_at=observed_at,
        observation_index=observation_index,
        title=observation.title.strip() if observation.title else None,
        text=excerpt if excerpt else None,
        media_type=_PAGE_MEDIA_TYPE,
        artifact_kind=ArtifactKind.NORMALIZED_TEXT,
        completeness=ArtifactCompleteness.SAMPLE,
        structural_metadata={
            **(
                {"http_status": str(observation.http_status)}
                if observation.http_status is not None
                else {}
            ),
            "depth": str(observation.depth),
            "rendered": str(observation.rendered).lower(),
        },
    )


__all__ = ["page_observation_to_content"]
