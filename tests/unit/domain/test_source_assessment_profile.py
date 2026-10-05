# SPDX-License-Identifier: AGPL-3.0-only
"""PR 14 SourceAssessment profile provenance and immutability (SA matrix)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from darkula.domain.identifiers import SourceAssessmentId, SourceId
from darkula.domain.source import (
    Confidence,
    SourceAssessment,
)

_MOMENT = datetime(2026, 2, 5, 12, 0, tzinfo=UTC)


def _assessment(**overrides: object) -> SourceAssessment:
    base: dict[str, object] = {
        "assessment_id": SourceAssessmentId.generate(),
        "source_id": SourceId.generate(),
        "assessed_at": _MOMENT,
        "window_start": _MOMENT,
        "window_end": _MOMENT,
        "confidence": Confidence(0.8),
        "relevance": Confidence(0.6),
        "activity": None,
        "novelty": Confidence(0.5),
        "evidence_references": ("content:abc",),
        "characteristics": {"source_type": "marketplace"},
        "profile_name": "source-analysis",
        "profile_version": "v1",
    }
    base.update(overrides)
    return SourceAssessment(**base)  # type: ignore[arg-type]


class TestSourceAssessmentProfile:
    """SA1/SA4/SA5/SA11/SA12: profile provenance is bounded and immutable."""

    def test_sa1_valid_profile_accepted(self) -> None:
        assessment = _assessment()
        assert assessment.profile_name == "source-analysis"
        assert assessment.profile_version == "v1"

    def test_sa4_blank_profile_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="profile_name"):
            _assessment(profile_name="   ")

    def test_sa4_blank_profile_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="profile_version"):
            _assessment(profile_version="")

    def test_sa5_overlong_profile_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="profile_name"):
            _assessment(profile_name="x" * 5000)

    def test_sa5_overlong_profile_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="profile_version"):
            _assessment(profile_version="x" * 200)

    def test_sa11_assessment_is_immutable(self) -> None:
        assessment = _assessment()
        with pytest.raises((AttributeError, TypeError)):
            assessment.profile_name = "other"  # type: ignore[misc]

    def test_sa12_identity_is_distinct(self) -> None:
        first = SourceAssessmentId.generate()
        second = SourceAssessmentId.generate()
        assert first != second

    def test_window_default_profile_is_legacy(self) -> None:
        # Legacy rows (pre-PR14) round-trip with the explicit legacy profile.
        assessment = SourceAssessment(
            assessment_id=SourceAssessmentId.generate(),
            source_id=SourceId.generate(),
            assessed_at=_MOMENT,
            window_start=_MOMENT,
            window_end=_MOMENT,
            confidence=Confidence(0.5),
        )
        assert assessment.profile_name == "legacy"
        assert assessment.profile_version == "v0"

    def test_sa6_confidence_bounds(self) -> None:
        with pytest.raises(ValueError):
            Confidence(1.5)
        with pytest.raises(ValueError):
            Confidence(-0.1)

    def test_sa7_nan_and_inf_rejected(self) -> None:
        with pytest.raises(ValueError):
            Confidence(float("nan"))
        with pytest.raises(ValueError):
            Confidence(float("inf"))

    def test_sa9_secret_like_characteristics_rejected(self) -> None:
        with pytest.raises(ValueError, match="credential-like"):
            _assessment(characteristics={"summary": "leaked credential value"})
