# SPDX-License-Identifier: AGPL-3.0-only
"""Domain test matrix for the six PR 4 source concepts (D1-D14)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from darkula.domain.identifiers import (
    CandidateEventId,
    ReconAssessmentId,
    SourceAssessmentId,
    SourceCandidateId,
    SourceEndpointId,
    SourceId,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    Confidence,
    EndpointStatus,
    EndpointType,
    ReconAssessment,
    ReconDisposition,
    Source,
    SourceAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
    SourceEndpoint,
    SourceStatus,
)

_UTC_NOW = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
_OBSERVED = _UTC_NOW - timedelta(days=7)
_WINDOW_START = _UTC_NOW - timedelta(days=30)
_WINDOW_END = _UTC_NOW


def _candidate(**overrides: object) -> SourceCandidate:
    base: dict[str, object] = {
        "candidate_id": SourceCandidateId.generate(),
        "discovered_at": _UTC_NOW,
        "discovery_method": "manual",
        "entrypoint": "http://example.invalid/index",
        "status": CandidateStatus.DISCOVERED,
    }
    base.update(overrides)
    return SourceCandidate(**base)  # type: ignore[arg-type]


def _event(**overrides: object) -> SourceCandidateEventHistory:
    base: dict[str, object] = {
        "event_id": CandidateEventId.generate(),
        "candidate_id": SourceCandidateId.generate(),
        "event_type": CandidateEventType.DISCOVERED,
        "occurred_at": _UTC_NOW,
        "context": {"channel": "seed-list"},
        "reason": "referenced by existing corpus",
        "provenance": "manual-review",
    }
    base.update(overrides)
    return SourceCandidateEventHistory(**base)  # type: ignore[arg-type]


def _source(**overrides: object) -> Source:
    base: dict[str, object] = {
        "source_id": SourceId.generate(),
        "status": SourceStatus.ACTIVE,
        "created_at": _WINDOW_START,
        "updated_at": _WINDOW_START,
        "name": "example",
    }
    base.update(overrides)
    return Source(**base)  # type: ignore[arg-type]


def _endpoint(**overrides: object) -> SourceEndpoint:
    base: dict[str, object] = {
        "endpoint_id": SourceEndpointId.generate(),
        "source_id": SourceId.generate(),
        "uri": "http://mirror.example.invalid/",
        "endpoint_type": EndpointType.MIRROR,
        "status": EndpointStatus.ACTIVE,
        "first_observed_at": _OBSERVED,
        "last_observed_at": _OBSERVED,
        "metadata": {"lang": "en"},
    }
    base.update(overrides)
    return SourceEndpoint(**base)  # type: ignore[arg-type]


def _recon(**overrides: object) -> ReconAssessment:
    base: dict[str, object] = {
        "assessment_id": ReconAssessmentId.generate(),
        "candidate_id": SourceCandidateId.generate(),
        "assessed_at": _UTC_NOW,
        "disposition": ReconDisposition.NEEDS_MORE_RECON,
        "confidence": Confidence(0.7),
        "evidence_references": ("recon-session-1",),
        "characteristics": {"focus": "marketplace-focus", "language": "en"},
    }
    base.update(overrides)
    return ReconAssessment(**base)  # type: ignore[arg-type]


def _source_assessment(**overrides: object) -> SourceAssessment:
    base: dict[str, object] = {
        "assessment_id": SourceAssessmentId.generate(),
        "source_id": SourceId.generate(),
        "assessed_at": _UTC_NOW,
        "window_start": _WINDOW_START,
        "window_end": _WINDOW_END,
        "confidence": Confidence(0.8),
        "relevance": Confidence(0.6),
        "activity": Confidence(0.4),
        "novelty": Confidence(0.5),
        "evidence_references": ("analysis-run-9",),
        "characteristics": {"focus": "marketplace-focus"},
    }
    base.update(overrides)
    return SourceAssessment(**base)  # type: ignore[arg-type]


class TestSourceCandidate:
    """D1/D3/D13: valid candidates are accepted with documented identity."""

    def test_valid_candidate_accepted(self) -> None:
        candidate = _candidate()
        assert candidate.status is CandidateStatus.DISCOVERED
        assert candidate.discovered_at == _UTC_NOW

    def test_documented_status_accepted(self) -> None:
        for status in CandidateStatus:
            assert _candidate(status=status).status is status

    def test_candidate_identity_differs_from_entrypoint(self) -> None:
        """D4: identity is the UUID, never the entrypoint text."""
        candidate = _candidate()
        assert str(candidate.candidate_id) != candidate.entrypoint
        assert candidate.entrypoint == "http://example.invalid/index"

    def test_valid_json_context_accepted(self) -> None:
        assert _candidate(
            discovery_context={"source": "index", "n": 3}
        ).discovery_context == {
            "source": "index",
            "n": 3,
        }

    def test_controls_in_text_rejected(self) -> None:
        with pytest.raises(ValueError):
            _candidate(entrypoint="http://x.invalid/a\x00b")

    def test_blank_entrypoint_rejected(self) -> None:
        with pytest.raises(ValueError):
            _candidate(entrypoint="   ")

    def test_secret_keys_rejected(self) -> None:
        with pytest.raises(ValueError, match="secret-like"):
            _candidate(discovery_context={"password": "hunter2"})


class TestTimeValidation:
    """D2: naive timestamps are rejected everywhere."""

    def test_naive_discovered_at_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _candidate(discovered_at=datetime(2025, 1, 1))

    def test_naive_event_occurred_at_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _event(occurred_at=datetime(2025, 1, 1))

    def test_naive_source_times_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _source(created_at=datetime(2025, 1, 1))
        with pytest.raises(ValueError, match="timezone-aware"):
            _source(updated_at=datetime(2025, 1, 1))

    def test_naive_endpoint_times_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _endpoint(first_observed_at=datetime(2025, 1, 1))
        with pytest.raises(ValueError, match="timezone-aware"):
            _endpoint(last_observed_at=datetime(2025, 1, 1))

    def test_naive_recon_assessed_at_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _recon(assessed_at=datetime(2025, 1, 1))

    def test_naive_assessment_times_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _source_assessment(assessed_at=datetime(2025, 1, 1))
        with pytest.raises(ValueError, match="timezone-aware"):
            _source_assessment(window_start=datetime(2025, 1, 1))
        with pytest.raises(ValueError, match="timezone-aware"):
            _source_assessment(window_end=datetime(2025, 1, 1))

    def test_aware_non_utc_normalized(self) -> None:
        aware = datetime(2025, 1, 1, tzinfo=UTC).astimezone()
        candidate = _candidate(discovered_at=aware)
        assert candidate.discovered_at.utcoffset() == timedelta(0)
        assert candidate.discovered_at == datetime(2025, 1, 1, tzinfo=UTC)


class TestConfidence:
    """D6-D9: one bounded confidence representation."""

    def test_lower_bound_accepted(self) -> None:
        assert Confidence(0.0).value == 0.0

    def test_below_lower_bound_rejected(self) -> None:
        with pytest.raises(ValueError):
            Confidence(-0.01)

    def test_upper_bound_accepted(self) -> None:
        assert Confidence(1.0).value == 1.0

    def test_above_upper_bound_rejected(self) -> None:
        with pytest.raises(ValueError):
            Confidence(1.01)

    def test_boolean_rejected(self) -> None:
        with pytest.raises(ValueError):
            Confidence(True)


class TestSourceEndpoint:
    """D5: observation ordering is validated."""

    def test_first_before_last_accepted(self) -> None:
        assert _endpoint().last_observed_at >= _endpoint().first_observed_at

    def test_first_after_last_rejected(self) -> None:
        with pytest.raises(ValueError, match="last_observed_at"):
            _endpoint(first_observed_at=_UTC_NOW, last_observed_at=_OBSERVED)


class TestSourceAssessmentWindow:
    """D10/D11: window validation."""

    def test_start_equals_end_accepted(self) -> None:
        assessment = _source_assessment(window_start=_UTC_NOW, window_end=_UTC_NOW)
        assert assessment.window_start == assessment.window_end

    def test_start_after_end_rejected(self) -> None:
        with pytest.raises(ValueError, match="window_end"):
            _source_assessment(
                window_start=_UTC_NOW, window_end=_UTC_NOW - timedelta(days=1)
            )


class TestImmutability:
    """D12: immutable history/assessments cannot be mutated."""

    @pytest.mark.parametrize(
        ("factory", "field"),
        [
            (_event, "occurred_at"),
            (_recon, "assessed_at"),
            (_source_assessment, "assessed_at"),
        ],
    )
    def test_records_are_frozen(self, factory: Callable[[], Any], field: str) -> None:
        record = factory()
        assert record is not None
        with pytest.raises(FrozenInstanceError):
            # Direct assignment goes through the frozen dataclass
            # __setattr__ and must raise FrozenInstanceError.
            exec(f"record.{field} = _OBSERVED")


class TestJsonMetadata:
    """D13/D14: JSON-compatible metadata accepted; non-JSON rejected."""

    def test_valid_json_metadata_accepted(self) -> None:
        assert _endpoint(metadata={"list": [1, 2], "nested": {"k": "v"}}).metadata == {
            "list": [1, 2],
            "nested": {"k": "v"},
        }

    def test_non_json_metadata_rejected(self) -> None:
        with pytest.raises(ValueError, match="JSON-compatible"):
            _endpoint(metadata={"bad": {1, 2, 3}})

    def test_non_string_keys_rejected(self) -> None:
        with pytest.raises(ValueError, match="keys must be strings"):
            _endpoint(metadata={1: "x"})

    def test_credential_like_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="credential-like"):
            _candidate(discovery_context={"note": "token=abc123"})
