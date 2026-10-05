# SPDX-License-Identifier: AGPL-3.0-only
"""PR 10 ReconSettings + LlmProviderSettings validation matrix."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from darkula.config.loader import load_settings
from darkula.config.settings import (
    LlmDriver,
    LlmProviderSettings,
    LlmSettings,
    ReconSettings,
    Settings,
)

_SHIPPED_CONFIG = Path(__file__).resolve().parents[3] / "config"


@pytest.fixture(autouse=True)
def _clean_darkula_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ambient DARKULA_* variable before each test."""
    for key in [key for key in os.environ if key.startswith("DARKULA_")]:
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides: Any) -> ReconSettings:
    defaults: dict[str, Any] = {
        "max_turns": 4,
        "max_inspections": 3,
        "max_pages_per_inspection": 6,
        "max_requests_per_inspection": 40,
        "max_depth_per_inspection": 2,
        "inspection_timeout_seconds": 90.0,
        "max_evidence_items": 12,
        "max_excerpt_chars": 800,
        "max_context_chars": 6000,
        "structured_output_repair_attempts": 1,
    }
    defaults.update(overrides)
    return ReconSettings(**defaults)


class TestReconSettings:
    """ReconSettings bounds: positive, ordered, fail closed."""

    def test_defaults_are_sane(self) -> None:
        settings = ReconSettings()
        assert settings.max_turns == 4
        assert settings.max_inspections == 3
        assert settings.max_evidence_items == 12
        assert settings.structured_output_repair_attempts == 1

    @pytest.mark.parametrize(
        "field",
        ["max_turns", "max_inspections", "max_evidence_items", "max_context_chars"],
    )
    def test_positive_integers_required(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _settings(**{field: 0})
        with pytest.raises(ValidationError):
            _settings(**{field: -1})

    def test_timeout_bounded(self) -> None:
        with pytest.raises(ValidationError):
            _settings(inspection_timeout_seconds=0)
        with pytest.raises(ValidationError):
            _settings(inspection_timeout_seconds=601)

    def test_excerpt_must_not_exceed_context(self) -> None:
        with pytest.raises(ValidationError):
            _settings(max_excerpt_chars=900, max_context_chars=800)

    def test_repair_attempts_bounded(self) -> None:
        with pytest.raises(ValidationError):
            _settings(structured_output_repair_attempts=4)

    def test_crawl_budgets_within_trusted_ceiling(self) -> None:
        with pytest.raises(ValidationError):
            _settings(max_pages_per_inspection=201)
        with pytest.raises(ValidationError):
            _settings(max_requests_per_inspection=501)
        with pytest.raises(ValidationError):
            _settings(max_depth_per_inspection=11)

    def test_evidence_items_cap(self) -> None:
        with pytest.raises(ValidationError):
            _settings(max_evidence_items=33)

    def test_unknown_fields_fail_closed(self) -> None:
        with pytest.raises(ValidationError):
            ReconSettings.model_validate({"extra_tool": "yes"})


class TestLlmProviderSettings:
    """Provider settings: bounded, secret-safe, no retry field."""

    def test_provider_defaults(self) -> None:
        provider = LlmProviderSettings()
        assert provider.model_name is None
        assert provider.api_key is None
        assert provider.base_url is None

    def test_model_name_blank_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LlmProviderSettings(model_name="   ")

    def test_api_key_blank_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LlmProviderSettings(api_key="  ")

    def test_no_retry_setting_exists(self) -> None:
        # One LlmClient call is one model attempt; no retry configuration
        # exists anywhere in the provider settings contract.
        assert not hasattr(LlmProviderSettings, "max_retries")
        with pytest.raises(ValidationError):
            LlmProviderSettings.model_validate({"max_retries": 3})

    def test_llm_settings_timeout_bounded(self) -> None:
        with pytest.raises(ValidationError):
            LlmSettings(timeout_seconds=0)
        with pytest.raises(ValidationError):
            LlmSettings(timeout_seconds=601)
        assert LlmSettings(timeout_seconds=30).timeout_seconds == 30
        assert LlmSettings().timeout_seconds is None


class TestLayeredReconSettings:
    """ReconSettings resolve through the layered loader with env override."""

    def test_default_profile_carries_recon_group(self) -> None:
        settings = load_settings(config_dir=_SHIPPED_CONFIG)
        assert settings.recon.max_turns == 4
        assert settings.llm.driver is LlmDriver.FAKE

    def test_environment_overrides_recon_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_RECON__MAX_TURNS", "7")
        settings = load_settings(config_dir=_SHIPPED_CONFIG)
        assert settings.recon.max_turns == 7
        assert settings.recon.max_inspections == 3

    def test_environment_cannot_break_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_RECON__MAX_TURNS", "0")
        with pytest.raises(ValidationError):
            load_settings(config_dir=_SHIPPED_CONFIG)

    def test_empty_environment_has_no_override_effect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_RECON__MAX_TURNS", "")
        settings = load_settings(config_dir=_SHIPPED_CONFIG)
        assert settings.recon.max_turns == 4

    def test_openai_driver_environment_selects_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_LLM__DRIVER", "openai")
        monkeypatch.setenv("DARKULA_LLM__PROVIDER__MODEL_NAME", "gpt-test")
        settings = load_settings(config_dir=_SHIPPED_CONFIG)
        assert settings.llm.driver is LlmDriver.OPENAI
        assert settings.llm.provider.model_name == "gpt-test"

    def test_settings_are_frozen(self) -> None:
        settings = Settings()
        with pytest.raises(ValidationError):
            settings.recon.max_turns = 99
        with pytest.raises(ValidationError):
            settings.llm.driver = LlmDriver.OPENAI
