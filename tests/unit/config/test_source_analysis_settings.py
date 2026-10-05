# SPDX-License-Identifier: AGPL-3.0-only
"""PR 14 SourceAnalysisSettings validation and environment override matrix."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from darkula.config.loader import load_settings
from darkula.config.settings import SourceAnalysisSettings

_SHIPPED_CONFIG = Path(__file__).resolve().parents[3] / "config"


@pytest.fixture(autouse=True)
def _clean_darkula_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [key for key in os.environ if key.startswith("DARKULA_")]:
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides: Any) -> SourceAnalysisSettings:
    defaults: dict[str, Any] = {
        "max_window_days": 30,
        "max_evidence_items": 500,
        "max_context_chars": 30000,
        "max_evidence_summary_chars": 1000,
        "max_evidence_refs": 100,
    }
    defaults.update(overrides)
    return SourceAnalysisSettings(**defaults)


class TestSourceAnalysisSettings:
    def test_defaults_are_bounded(self) -> None:
        settings = SourceAnalysisSettings()
        assert settings.enabled is True
        assert settings.max_window_days == 30
        assert settings.max_evidence_items == 500
        assert settings.max_context_chars == 30000
        assert settings.max_evidence_summary_chars == 1000
        assert settings.max_evidence_refs == 100

    @pytest.mark.parametrize(
        "field",
        [
            "max_window_days",
            "max_evidence_items",
            "max_context_chars",
            "max_evidence_summary_chars",
            "max_evidence_refs",
        ],
    )
    def test_positive_bounds_required(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _settings(**{field: 0})
        with pytest.raises(ValidationError):
            _settings(**{field: -1})

    def test_window_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            _settings(max_window_days=4000)

    def test_evidence_items_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            _settings(max_evidence_items=20000)

    def test_summary_must_not_exceed_context(self) -> None:
        with pytest.raises(ValidationError):
            _settings(max_evidence_summary_chars=2000, max_context_chars=100)

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceAnalysisSettings(profile_name="caller-controlled")  # type: ignore[call-arg]

    def test_env_override_is_ultimate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DARKULA_SOURCE_ANALYSIS__ENABLED", "false")
        monkeypatch.setenv("DARKULA_SOURCE_ANALYSIS__MAX_WINDOW_DAYS", "7")
        settings = load_settings(config_dir=_SHIPPED_CONFIG)
        assert settings.source_analysis.enabled is False
        assert settings.source_analysis.max_window_days == 7
