# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Evaluation settings tests (PR 15)."""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from darkula.config.settings import (
    AgentObservabilitySettings,
    EvaluationSettings,
    Settings,
)


class TestEvaluationSettings:
    def test_defaults_disabled(self) -> None:
        settings = EvaluationSettings()
        assert settings.enabled is False
        assert settings.live_model_enabled is False
        assert settings.output_dir == pathlib.Path(".eval-results")
        assert settings.quality_gate_enabled is False

    def test_settings_root_includes_evaluation(self) -> None:
        assert isinstance(Settings().evaluation, EvaluationSettings)

    def test_blank_dataset_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvaluationSettings(dataset="   ")

    def test_blank_case_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvaluationSettings(case_ids=("  ",))

    def test_control_case_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvaluationSettings(case_ids=("bad\x01",))

    def test_valid_case_ids(self) -> None:
        settings = EvaluationSettings(case_ids=("EV-RECON-001",))
        assert settings.case_ids == ("EV-RECON-001",)


class TestAgentObservabilitySettings:
    def test_defaults(self) -> None:
        settings = AgentObservabilitySettings()
        assert settings.project is None
        assert settings.api_key is None

    def test_blank_project_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentObservabilitySettings(project="  ")

    def test_blank_api_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentObservabilitySettings(api_key="  ")
