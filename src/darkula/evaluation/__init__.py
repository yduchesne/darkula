# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula evaluation subsystem (PR 15).

This package is evaluator-side only: production domain/application/
infrastructure/composition code must never import it. It consumes production
contracts from outside the production pipeline and compares bounded outputs
with versioned Fake World truth after execution.

Layout:

- ``domain`` — bounded evaluation identity/result/manifest models;
- ``metrics`` — deterministic metric primitives;
- ``evaluators`` — deterministic per-layer evaluators;
- ``cases`` — explicit versioned case registry;
- ``runner`` — the evaluation runner and production-capability port;
- ``artifacts`` — bounded atomic result/manifest artifacts;
- ``cli`` — explicit opt-in command-line entry point.
"""

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
from darkula.evaluation.runner import (
    EvalConfigurationError,
    EvalExecutor,
    EvalModelError,
    EvalRunner,
    EvalSetupError,
)

__all__ = [
    "EvalCase",
    "EvalCaseId",
    "EvalCaseResult",
    "EvalConfigurationError",
    "EvalExecutor",
    "EvalInvocationId",
    "EvalLayer",
    "EvalMetric",
    "EvalModelError",
    "EvalModelIdentity",
    "EvalRunId",
    "EvalRunManifest",
    "EvalRunStatus",
    "EvalRunner",
    "EvalSetupError",
    "EvalStatus",
    "EvalValidationError",
]
