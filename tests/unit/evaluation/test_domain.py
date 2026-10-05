# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Evaluation domain-model tests (PR 15) — matrix ED15-1..ED15-10."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from darkula.evaluation.artifacts import manifest_to_dict, result_to_dict
from darkula.evaluation.domain import (
    EvalCase,
    EvalCaseId,
    EvalCaseResult,
    EvalInvocationId,
    EvalLayer,
    EvalMetric,
    EvalModelIdentity,
    EvalRunId,
    EvalRunManifest,
    EvalRunStatus,
    EvalStatus,
    EvalValidationError,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)


class TestIdentity:
    """ED15-1..ED15-4."""

    def test_ed15_1_valid_case_id(self) -> None:
        assert EvalCaseId("EV-RECON-001").value == "EV-RECON-001"

    def test_ed15_2_blank_control_id_rejected(self) -> None:
        with pytest.raises(EvalValidationError):
            EvalCaseId("   ")
        with pytest.raises(EvalValidationError):
            EvalCaseId("bad\x01id")

    def test_ed15_3_invalid_version_rejected(self) -> None:
        with pytest.raises(EvalValidationError):
            _case(version=0)
        with pytest.raises(EvalValidationError):
            _case(version=True)

    def test_ed15_4_model_identity(self) -> None:
        identity = EvalModelIdentity(provider="openai", model_name="gpt-4o")
        assert identity.is_live is True
        assert EvalModelIdentity(provider="fake", model_name="fake").is_live is False

    def test_ed15_5_invalid_identity_rejected(self) -> None:
        with pytest.raises(EvalValidationError):
            EvalModelIdentity(provider="  ", model_name="x")
        with pytest.raises(EvalValidationError):
            EvalModelIdentity(provider="openai", model_name="")


class TestResult:
    """ED15-6..ED15-7."""

    def _result(self, **overrides: object) -> EvalCaseResult:
        defaults: dict[str, object] = {
            "run_id": EvalRunId("run-1"),
            "invocation_id": EvalInvocationId("run-1-0001"),
            "case_id": EvalCaseId("EV-RECON-001"),
            "case_version": 1,
            "layer": EvalLayer.RECON,
            "scenario_id": "darkula-cross-source",
            "scenario_version": 1,
            "evaluator_name": "recon",
            "evaluator_version": "v1",
            "operation_name": "recon.assess",
            "model": EvalModelIdentity(provider="fake", model_name="fake"),
            "status": EvalStatus.COMPLETED,
            "duration_ms": 1.5,
            "metrics": (
                EvalMetric(name="recon.f1", evaluator_version="v1", value=1.0),
            ),
        }
        defaults.update(overrides)
        return EvalCaseResult(**defaults)  # type: ignore[arg-type]

    def test_ed15_6_raw_exception_not_representable(self) -> None:
        with pytest.raises(EvalValidationError):
            self._result(failure_code=ValueError("boom"))
        result = self._result(failure_code="model_error")
        assert result.failure_code == "model_error"

    def test_ed15_7_serialization_deterministic_bounded(self) -> None:
        result = self._result()
        first = json.dumps(result_to_dict(result), sort_keys=True)
        second = json.dumps(result_to_dict(result), sort_keys=True)
        assert first == second
        assert "http" not in first and "prompt" not in first

    def test_duration_and_version_validation(self) -> None:
        with pytest.raises(EvalValidationError):
            self._result(duration_ms=-1.0)
        with pytest.raises(EvalValidationError):
            self._result(case_version=0)

    def test_succeeded_flag(self) -> None:
        assert self._result().succeeded is True
        assert self._result(status=EvalStatus.MODEL_ERROR).succeeded is False


class TestManifest:
    def _manifest(self, **overrides: object) -> EvalRunManifest:
        defaults: dict[str, object] = {
            "run_id": EvalRunId("run-1"),
            "started_at": T0,
            "completed_at": T1,
            "status": EvalRunStatus.COMPLETED,
            "model": EvalModelIdentity(provider="fake", model_name="fake"),
            "scenario_ids": ("darkula-cross-source",),
            "case_ids": ("EV-RECON-001",),
            "total_cases": 1,
            "completed_cases": 1,
            "failed_cases": 0,
        }
        defaults.update(overrides)
        return EvalRunManifest(**defaults)  # type: ignore[arg-type]

    def test_manifest_serialization_deterministic(self) -> None:
        manifest = self._manifest()
        first = json.dumps(manifest_to_dict(manifest), sort_keys=True)
        second = json.dumps(manifest_to_dict(manifest), sort_keys=True)
        assert first == second

    def test_manifest_rejects_bad_order(self) -> None:
        with pytest.raises(EvalValidationError):
            self._manifest(completed_at=T0.replace(year=2025))

    def test_manifest_rejects_overcount(self) -> None:
        with pytest.raises(EvalValidationError):
            self._manifest(total_cases=1, completed_cases=1, failed_cases=1)

    def test_partial_status_distinguishable(self) -> None:
        manifest = self._manifest(
            status=EvalRunStatus.PARTIAL, total_cases=2, failed_cases=1
        )
        payload = manifest_to_dict(manifest)
        assert payload["status"] == "PARTIAL"
        assert payload["failed_cases"] == 1


def _case(*, version: int = 1) -> EvalCase:
    return EvalCase(
        case_id=EvalCaseId("EV-RECON-001"),
        version=version,
        layer=EvalLayer.RECON,
        scenario_id="blackgate-core",
        scenario_version=1,
        description="d",
        evaluator_name="recon",
        evaluator_version="v1",
        operation_name="recon.assess",
    )
