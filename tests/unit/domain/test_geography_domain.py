# SPDX-License-Identifier: AGPL-3.0-only
"""Geographic-resolution domain matrix (PR 12)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from darkula.domain.geography import (
    GeographicResolution,
    GeographicResolutionRequest,
    GeographicResolutionStatus,
    GeographicResolverIdentity,
    GeographicResolverResult,
)
from darkula.domain.identifiers import ExtractedEntityId, GeographicResolutionId

_MOMENT = datetime(2026, 5, 1, tzinfo=UTC)


def _resolver() -> GeographicResolverIdentity:
    return GeographicResolverIdentity(name="fake-geo", version="v1")


def _resolved(**overrides: object) -> GeographicResolution:
    base: dict[str, object] = {
        "resolution_id": GeographicResolutionId.generate(),
        "extracted_entity_id": ExtractedEntityId.generate(),
        "status": GeographicResolutionStatus.RESOLVED,
        "resolver": _resolver(),
        "resolved_at": _MOMENT,
        "canonical_name": "Washington",
        "country_code": "US",
        "administrative_area": "Washington",
        "latitude": 47.75,
        "longitude": -120.74,
        "confidence": 0.8,
    }
    base.update(overrides)
    return GeographicResolution(**base)  # type: ignore[arg-type]


class TestResolverIdentityAndRequest:
    def test_valid_identity(self) -> None:
        assert str(_resolver()) == "fake-geo/v1"

    def test_blank_identity_rejected(self) -> None:
        with pytest.raises(ValueError):
            GeographicResolverIdentity(name="", version="v1")

    def test_request_bounds(self) -> None:
        request = GeographicResolutionRequest(mention="Washington", left_context="a ")
        assert request.mention == "Washington"
        with pytest.raises(ValueError):
            GeographicResolutionRequest(mention="   ")


class TestResolutionStatuses:
    def test_resolved_valid(self) -> None:
        resolution = _resolved()
        assert resolution.status is GeographicResolutionStatus.RESOLVED

    def test_resolved_without_canonical_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            _resolved(canonical_name=None)

    def test_resolved_without_confidence_rejected(self) -> None:
        with pytest.raises(ValueError):
            _resolved(confidence=None)

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            _resolved(confidence=1.5)
        with pytest.raises(ValueError):
            _resolved(confidence=float("nan"))

    def test_invalid_coordinates_rejected(self) -> None:
        with pytest.raises(ValueError):
            _resolved(latitude=100.0)
        with pytest.raises(ValueError):
            _resolved(longitude=-200.0)

    def test_partial_coordinates_rejected(self) -> None:
        with pytest.raises(ValueError):
            _resolved(latitude=47.0, longitude=None)

    def test_invalid_country_code_rejected(self) -> None:
        with pytest.raises(ValueError):
            _resolved(country_code="USA")

    def test_ambiguous_must_not_carry_canonical_place(self) -> None:
        with pytest.raises(ValueError):
            GeographicResolution(
                resolution_id=GeographicResolutionId.generate(),
                extracted_entity_id=ExtractedEntityId.generate(),
                status=GeographicResolutionStatus.AMBIGUOUS,
                resolver=_resolver(),
                resolved_at=_MOMENT,
                canonical_name="Washington",
            )
        ambiguous = GeographicResolution(
            resolution_id=GeographicResolutionId.generate(),
            extracted_entity_id=ExtractedEntityId.generate(),
            status=GeographicResolutionStatus.AMBIGUOUS,
            resolver=_resolver(),
            resolved_at=_MOMENT,
            confidence=0.45,
        )
        assert ambiguous.canonical_name is None

    def test_unresolved_has_no_canonical_fields(self) -> None:
        unresolved = GeographicResolution(
            resolution_id=GeographicResolutionId.generate(),
            extracted_entity_id=ExtractedEntityId.generate(),
            status=GeographicResolutionStatus.UNRESOLVED,
            resolver=_resolver(),
            resolved_at=_MOMENT,
        )
        assert unresolved.canonical_name is None
        assert unresolved.latitude is None

    def test_resolver_result_resolved_requires_name_and_confidence(self) -> None:
        with pytest.raises(ValueError):
            GeographicResolverResult(
                status=GeographicResolutionStatus.RESOLVED, canonical_name="X"
            )
        result = GeographicResolverResult(
            status=GeographicResolutionStatus.RESOLVED,
            canonical_name="Washington",
            confidence=0.9,
            latitude=47.0,
            longitude=-120.0,
        )
        assert result.confidence == 0.9
