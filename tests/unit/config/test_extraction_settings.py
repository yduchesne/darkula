# SPDX-License-Identifier: AGPL-3.0-only
"""Extraction settings validation (PR 11)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from darkula.config.settings import (
    ExtractionSettings,
    GeographicResolverDriver,
    GeographySettings,
    RelationshipExtractionSettings,
    SemanticExtractionSettings,
)


class TestExtractionSettings:
    def test_default_max_entities(self) -> None:
        assert ExtractionSettings().max_entities_per_content == 5000

    def test_custom_bound(self) -> None:
        assert (
            ExtractionSettings(max_entities_per_content=7).max_entities_per_content == 7
        )

    def test_non_positive_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionSettings(max_entities_per_content=0)

    def test_over_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionSettings(max_entities_per_content=1_000_001)

    def test_unknown_field_fails_closed(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionSettings(profile_version="v2")  # type: ignore[call-arg]


class TestSemanticExtractionSettings:
    def test_defaults(self) -> None:
        settings = SemanticExtractionSettings()
        assert settings.enabled is True
        assert settings.max_entities_per_content == 500
        assert settings.max_input_bytes == 262144
        assert settings.max_attempts == 1

    def test_bounds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SemanticExtractionSettings(max_entities_per_content=0)
        with pytest.raises(ValidationError):
            SemanticExtractionSettings(max_input_bytes=0)
        with pytest.raises(ValidationError):
            SemanticExtractionSettings(max_attempts=0)
        with pytest.raises(ValidationError):
            SemanticExtractionSettings(max_attempts=4)


class TestRelationshipExtractionSettings:
    def test_defaults(self) -> None:
        settings = RelationshipExtractionSettings()
        assert settings.enabled is True
        assert settings.max_relationships_per_content == 500
        assert settings.max_model_candidates == 500
        assert settings.max_support_chars == 4096
        assert settings.max_context_chars == 256
        assert settings.max_input_bytes == 262144

    def test_positive_bounds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RelationshipExtractionSettings(max_relationships_per_content=0)
        with pytest.raises(ValidationError):
            RelationshipExtractionSettings(max_model_candidates=0)
        with pytest.raises(ValidationError):
            RelationshipExtractionSettings(max_support_chars=0)
        with pytest.raises(ValidationError):
            RelationshipExtractionSettings(max_input_bytes=0)

    def test_context_bound(self) -> None:
        with pytest.raises(ValidationError):
            RelationshipExtractionSettings(max_context_chars=-1)
        with pytest.raises(ValidationError):
            RelationshipExtractionSettings(max_context_chars=4097)

    def test_unknown_field_fails_closed(self) -> None:
        with pytest.raises(ValidationError):
            RelationshipExtractionSettings(profile_version="v2")  # type: ignore[call-arg]


class TestGeographySettings:
    def test_defaults_are_disabled(self) -> None:
        settings = GeographySettings()
        assert settings.enabled is False
        assert settings.resolver is GeographicResolverDriver.NONE
        assert settings.context_chars == 200

    def test_context_bound(self) -> None:
        with pytest.raises(ValidationError):
            GeographySettings(context_chars=-1)
        with pytest.raises(ValidationError):
            GeographySettings(context_chars=2001)

    def test_unknown_resolver_fails_closed(self) -> None:
        with pytest.raises(ValidationError):
            GeographySettings(resolver="google")  # type: ignore[arg-type]
