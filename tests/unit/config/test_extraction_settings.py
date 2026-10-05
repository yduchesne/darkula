# SPDX-License-Identifier: AGPL-3.0-only
"""Extraction settings validation (PR 11)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from darkula.config.settings import ExtractionSettings


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
