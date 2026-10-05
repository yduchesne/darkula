# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded evaluation domain model (PR 15).

Evaluation identity is deliberately distinct from production identity:

- ``EvalCaseId`` is the stable identity of one evaluation case;
- ``EvalRunId`` identifies one experiment/run;
- ``EvalInvocationId`` identifies one case execution within a run;
- scenario/version identifies the synthetic dataset;
- evaluator name/version identifies scoring semantics;
- ``EvalModelIdentity`` identifies the model under test.

Everything here is immutable, bounded, control-free, and serializable to a
deterministic JSON document. Raw exceptions, prompts, outputs, secrets, and
unrestricted content are not representable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from darkula.testing.fake_world.identifiers import require_utc

#: Upper bound for any evaluation string field.
MAX_EVAL_FIELD_LENGTH = 200

#: Conservative token shape for evaluation identities.
_EVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EvalValidationError(ValueError):
    """Typed, bounded validation failure for the evaluation domain."""


def _require_text(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise EvalValidationError(f"{field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise EvalValidationError(f"{field_name} must not be blank")
    if len(stripped) > max_length:
        raise EvalValidationError(
            f"{field_name} must not exceed {max_length} characters"
        )
    if any(ord(char) < 32 for char in stripped):
        raise EvalValidationError(f"{field_name} must not contain control characters")
    return stripped


@dataclass(frozen=True, slots=True)
class EvalCaseId:
    """Stable identity of one evaluation case (for example ``EV-RECON-001``)."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _validate_eval_id(self.value, field_name="EvalCaseId"),
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class EvalRunId:
    """Stable identity of one evaluation run/experiment."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _validate_eval_id(self.value, field_name="EvalRunId"),
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class EvalInvocationId:
    """Stable identity of one case execution within a run."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _validate_eval_id(self.value, field_name="EvalInvocationId"),
        )

    def __str__(self) -> str:
        return self.value


def _validate_eval_id(value: str, *, field_name: str) -> str:
    stripped = _require_text(value, field_name=field_name, max_length=128)
    if _EVAL_ID_RE.fullmatch(stripped) is None:
        raise EvalValidationError(
            f"{field_name} must match {_EVAL_ID_RE.pattern!r} (got {value!r})"
        )
    return stripped


class EvalLayer(StrEnum):
    """Finite set of evaluated production layers."""

    RECON = "RECON"
    SEMANTIC_EXTRACTION = "SEMANTIC_EXTRACTION"
    GEOGRAPHY = "GEOGRAPHY"
    RELATIONSHIP_EXTRACTION = "RELATIONSHIP_EXTRACTION"
    SOURCE_ANALYSIS = "SOURCE_ANALYSIS"


class EvalStatus(StrEnum):
    """Finite per-case/run status vocabulary (no raw exceptions)."""

    COMPLETED = "COMPLETED"
    CASE_ERROR = "CASE_ERROR"
    MODEL_ERROR = "MODEL_ERROR"
    EVALUATOR_ERROR = "EVALUATOR_ERROR"
    SETUP_ERROR = "SETUP_ERROR"
    CANCELLED = "CANCELLED"


class EvalRunStatus(StrEnum):
    """Finite run status vocabulary; partial runs stay distinguishable."""

    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class EvalModelIdentity:
    """Configured identity of the model under test.

    ``provider`` is ``fake`` for deterministic runs and a provider name (for
    example ``openai``) for live runs. ``profile`` is the configured profile
    name when one exists. This is configuration identity only; prompts,
    outputs, and provider response metadata are never represented here.
    """

    provider: str
    model_name: str
    profile: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider",
            _require_text(self.provider, field_name="model provider", max_length=128),
        )
        object.__setattr__(
            self,
            "model_name",
            _require_text(self.model_name, field_name="model name", max_length=256),
        )
        if self.profile is not None:
            object.__setattr__(
                self,
                "profile",
                _require_text(self.profile, field_name="model profile", max_length=128),
            )

    @property
    def is_live(self) -> bool:
        """Return whether this identity names a live provider model."""
        return self.provider.lower() != "fake"


@dataclass(frozen=True, slots=True)
class EvalMetric:
    """One bounded metric produced by a deterministic evaluator.

    ``value`` is always within ``[0, 1]``. Exact counts are preserved
    alongside the normalized value where they apply.
    """

    name: str
    evaluator_version: str
    value: float
    numerator: int | None = None
    denominator: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            _require_text(self.name, field_name="metric name", max_length=128),
        )
        object.__setattr__(
            self,
            "evaluator_version",
            _require_text(
                self.evaluator_version,
                field_name="evaluator version",
                max_length=64,
            ),
        )
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise EvalValidationError("metric value must be a number")
        if not (0.0 <= float(self.value) <= 1.0):
            raise EvalValidationError("metric value must be within [0, 1]")
        object.__setattr__(self, "value", float(self.value))
        for label, count in (
            ("numerator", self.numerator),
            ("denominator", self.denominator),
        ):
            if count is not None and (not isinstance(count, int) or count < 0):
                raise EvalValidationError(f"metric {label} must be a non-negative int")


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One stable, versioned evaluation case definition.

    A case names the scenario/version, the evaluated layer, the production
    operation, the evaluator name/version, and whether a live model is
    required. Setup requirements are declared as bounded tags/notes rather
    than executable code.
    """

    case_id: EvalCaseId
    version: int
    layer: EvalLayer
    scenario_id: str
    scenario_version: int
    description: str
    evaluator_name: str
    evaluator_version: str
    operation_name: str
    tags: tuple[str, ...] = ()
    requires_live_model: bool = False
    requires_browser: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise EvalValidationError("case version must be an integer")
        if self.version < 1:
            raise EvalValidationError("case version must be a positive integer")
        if not isinstance(self.scenario_version, int) or isinstance(
            self.scenario_version, bool
        ):
            raise EvalValidationError("scenario version must be an integer")
        if self.scenario_version < 1:
            raise EvalValidationError("scenario version must be a positive integer")
        object.__setattr__(
            self,
            "scenario_id",
            _require_text(self.scenario_id, field_name="scenario id", max_length=128),
        )
        object.__setattr__(
            self,
            "description",
            _require_text(
                self.description, field_name="case description", max_length=500
            ),
        )
        object.__setattr__(
            self,
            "evaluator_name",
            _require_text(
                self.evaluator_name, field_name="evaluator name", max_length=128
            ),
        )
        object.__setattr__(
            self,
            "evaluator_version",
            _require_text(
                self.evaluator_version, field_name="evaluator version", max_length=64
            ),
        )
        object.__setattr__(
            self,
            "operation_name",
            _require_text(
                self.operation_name, field_name="operation name", max_length=128
            ),
        )
        for tag in self.tags:
            _require_text(tag, field_name="case tag", max_length=64)

    @property
    def key(self) -> tuple[str, int]:
        """Stable registry key: ``(case_id, version)``."""
        return (self.case_id.value, self.version)


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """One bounded result of executing and scoring one case.

    No raw exception object, prompt, output, or unrestricted body is
    representable; failures carry only a bounded string code.
    """

    run_id: EvalRunId
    invocation_id: EvalInvocationId
    case_id: EvalCaseId
    case_version: int
    layer: EvalLayer
    scenario_id: str
    scenario_version: int
    evaluator_name: str
    evaluator_version: str
    operation_name: str
    model: EvalModelIdentity
    status: EvalStatus
    duration_ms: float
    metrics: tuple[EvalMetric, ...] = ()
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.case_version < 1 or self.scenario_version < 1:
            raise EvalValidationError("result versions must be positive")
        if isinstance(self.duration_ms, bool) or not isinstance(
            self.duration_ms, (int, float)
        ):
            raise EvalValidationError("duration_ms must be a number")
        if self.duration_ms < 0:
            raise EvalValidationError("duration_ms must be non-negative")
        object.__setattr__(self, "duration_ms", float(self.duration_ms))
        object.__setattr__(
            self,
            "scenario_id",
            _require_text(self.scenario_id, field_name="scenario id", max_length=128),
        )
        object.__setattr__(
            self,
            "evaluator_name",
            _require_text(
                self.evaluator_name, field_name="evaluator name", max_length=128
            ),
        )
        object.__setattr__(
            self,
            "evaluator_version",
            _require_text(
                self.evaluator_version, field_name="evaluator version", max_length=64
            ),
        )
        object.__setattr__(
            self,
            "operation_name",
            _require_text(
                self.operation_name, field_name="operation name", max_length=128
            ),
        )
        if self.failure_code is not None:
            object.__setattr__(
                self,
                "failure_code",
                _require_text(
                    self.failure_code, field_name="failure code", max_length=64
                ),
            )

    @property
    def succeeded(self) -> bool:
        """Return whether the case completed successfully."""
        return self.status is EvalStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class EvalRunManifest:
    """Bounded manifest finalizing one evaluation run.

    ``status`` is ``COMPLETED`` only when every selected case completed.
    A run interrupted mid-flight is written as ``PARTIAL``/``FAILED`` so a
    partial run is never mistaken for a completed one.
    """

    run_id: EvalRunId
    started_at: datetime
    completed_at: datetime
    status: EvalRunStatus
    model: EvalModelIdentity
    scenario_ids: tuple[str, ...]
    case_ids: tuple[str, ...]
    total_cases: int
    completed_cases: int
    failed_cases: int
    cancelled: bool = False
    quality_gate_enabled: bool = False
    quality_gate_passed: bool | None = None

    def __post_init__(self) -> None:
        require_utc(self.started_at, field_name="manifest started_at")
        require_utc(self.completed_at, field_name="manifest completed_at")
        if self.completed_at < self.started_at:
            raise EvalValidationError(
                "manifest completed_at must not precede started_at"
            )
        for label, count in (
            ("total_cases", self.total_cases),
            ("completed_cases", self.completed_cases),
            ("failed_cases", self.failed_cases),
        ):
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise EvalValidationError(
                    f"manifest {label} must be a non-negative int"
                )
        if self.completed_cases + self.failed_cases > self.total_cases:
            raise EvalValidationError(
                "manifest completed+failed cases must not exceed total cases"
            )
        for scenario_id in self.scenario_ids:
            _require_text(
                scenario_id, field_name="manifest scenario id", max_length=128
            )
        for case_id in self.case_ids:
            _require_text(case_id, field_name="manifest case id", max_length=128)


__all__ = [
    "MAX_EVAL_FIELD_LENGTH",
    "EvalCase",
    "EvalCaseId",
    "EvalCaseResult",
    "EvalInvocationId",
    "EvalLayer",
    "EvalMetric",
    "EvalModelIdentity",
    "EvalRunId",
    "EvalRunManifest",
    "EvalRunStatus",
    "EvalStatus",
    "EvalValidationError",
]
