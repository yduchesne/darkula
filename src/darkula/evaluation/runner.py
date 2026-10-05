# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic evaluation runner (PR 15).

The runner selects explicitly registered cases, invokes an injected
production-capability port once per case, measures duration, scores with the
case's deterministic evaluator, writes bounded artifacts incrementally, and
finalizes a manifest atomically. It never constructs a model/provider SDK,
never puts truth in a prompt, and never deduplicates historical runs.

Live execution is opt-in: requesting a live run while the composed model is
the deterministic fake fails fast (the fake can never masquerade as live).

Cancellation propagates: a partial manifest is written before the
``asyncio.CancelledError`` is re-raised so a cancelled run is never mistaken
for a completed one.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from darkula.evaluation.artifacts import EvalArtifactStore
from darkula.evaluation.cases import (
    EvalCasePlan,
    get_case_plan,
    list_case_plans,
)
from darkula.evaluation.domain import (
    EvalCaseResult,
    EvalInvocationId,
    EvalMetric,
    EvalModelIdentity,
    EvalRunId,
    EvalRunManifest,
    EvalRunStatus,
    EvalStatus,
)
from darkula.evaluation.evaluators import (
    EvalEvaluatorError,
    GeographyExpectation,
    GeographyObservation,
    ReconExpectation,
    ReconObservation,
    RelationshipExpectation,
    RelationshipObservation,
    SemanticExpectation,
    SemanticObservation,
    SourceAnalystExpectation,
    SourceAnalystObservation,
    evaluate_geography,
    evaluate_recon,
    evaluate_relationship,
    evaluate_semantic,
    evaluate_source_analyst,
)

__all__ = [
    "EvalConfigurationError",
    "EvalExecutor",
    "EvalModelError",
    "EvalRunner",
    "EvalSetupError",
    "ProductionObservation",
    "select_case_plans",
]


class EvalConfigurationError(RuntimeError):
    """Explicit, bounded runner/configuration failure (no fake fallback)."""


class EvalModelError(RuntimeError):
    """Bounded model/provider failure raised by a production executor."""


class EvalSetupError(RuntimeError):
    """Bounded setup failure (missing browser/sandbox/infrastructure)."""


ProductionObservation = (
    ReconObservation
    | SemanticObservation
    | GeographyObservation
    | RelationshipObservation
    | SourceAnalystObservation
)


class EvalExecutor(Protocol):
    """Production-capability port invoked once per evaluation case.

    Implementations reuse existing Darkula production contracts internally;
    the runner never constructs a provider SDK. ``execute`` must raise
    :class:`EvalModelError` for model failures and :class:`EvalSetupError` for
    missing infrastructure; every other raised exception is bounded to a
    ``CASE_ERROR``.
    """

    @property
    def model_identity(self) -> EvalModelIdentity:
        """Configured identity of the model under test."""
        ...

    async def execute(self, plan: EvalCasePlan) -> ProductionObservation:
        """Return one bounded production-output observation for ``plan``."""
        ...


class EvalRunner:
    """Run a deterministic sequence of evaluation cases and write artifacts."""

    def __init__(
        self,
        *,
        executor: EvalExecutor,
        artifacts: EvalArtifactStore,
        clock: Callable[[], datetime] | None = None,
        run_id: str | None = None,
        quality_gate: Callable[[tuple[EvalCaseResult, ...]], bool] | None = None,
    ) -> None:
        self._executor = executor
        self._artifacts = artifacts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_id = EvalRunId(run_id or f"run-{uuid.uuid4().hex[:12]}")
        self._quality_gate = quality_gate

    @property
    def run_id(self) -> EvalRunId:
        """The stable identity of this run."""
        return self._run_id

    async def run(
        self,
        plans: Sequence[EvalCasePlan],
        *,
        live_requested: bool = False,
        quality_gate_enabled: bool = False,
    ) -> EvalRunManifest:
        """Execute, score, and persist the selected cases in order.

        :raises EvalConfigurationError: live execution was requested while
            the composed model is not a live provider, or a quality gate was
            enabled without an explicit gate.
        :raises asyncio.CancelledError: unchanged (after a partial manifest).
        """
        if not plans:
            raise EvalConfigurationError("no evaluation cases selected")
        if live_requested and not self._executor.model_identity.is_live:
            raise EvalConfigurationError(
                "live evaluation was requested but the composed model is not live"
            )
        if quality_gate_enabled and self._quality_gate is None:
            raise EvalConfigurationError(
                "quality gating was enabled without an explicit quality gate"
            )
        self._artifacts.begin_run(self._run_id.value)
        started_at = self._clock()
        results: list[EvalCaseResult] = []
        cancelled = False
        try:
            for index, plan in enumerate(plans, start=1):
                invocation_id = EvalInvocationId(f"{self._run_id.value}-{index:04d}")
                result = await self._run_case(plan, invocation_id)
                results.append(result)
                self._artifacts.append_result(self._run_id.value, result)
        except asyncio.CancelledError:
            cancelled = True
            self._artifacts.write_manifest(
                self._run_id.value,
                self._build_manifest(
                    started_at=started_at,
                    completed_at=self._clock(),
                    plans=plans,
                    results=tuple(results),
                    cancelled=True,
                    quality_gate_enabled=quality_gate_enabled,
                    quality_gate_passed=None,
                ),
            )
            raise
        completed_at = self._clock()
        gate_passed: bool | None = None
        if quality_gate_enabled and self._quality_gate is not None:
            gate_passed = bool(self._quality_gate(tuple(results)))
        manifest = self._build_manifest(
            started_at=started_at,
            completed_at=completed_at,
            plans=plans,
            results=tuple(results),
            cancelled=cancelled,
            quality_gate_enabled=quality_gate_enabled,
            quality_gate_passed=gate_passed,
        )
        self._artifacts.write_manifest(self._run_id.value, manifest)
        return manifest

    async def _run_case(
        self, plan: EvalCasePlan, invocation_id: EvalInvocationId
    ) -> EvalCaseResult:
        started = time.monotonic()
        try:
            observation = await self._executor.execute(plan)
        except asyncio.CancelledError:
            raise
        except EvalModelError as exc:
            return self._failed_result(
                plan, invocation_id, EvalStatus.MODEL_ERROR, exc, started
            )
        except EvalSetupError as exc:
            return self._failed_result(
                plan, invocation_id, EvalStatus.SETUP_ERROR, exc, started
            )
        except Exception as exc:
            return self._failed_result(
                plan, invocation_id, EvalStatus.CASE_ERROR, exc, started
            )
        try:
            metrics = _score(plan, observation)
        except asyncio.CancelledError:
            raise
        except EvalEvaluatorError as exc:
            return self._failed_result(
                plan, invocation_id, EvalStatus.EVALUATOR_ERROR, exc, started
            )
        except Exception as exc:
            return self._failed_result(
                plan, invocation_id, EvalStatus.EVALUATOR_ERROR, exc, started
            )
        case = plan.case
        return EvalCaseResult(
            run_id=self._run_id,
            invocation_id=invocation_id,
            case_id=case.case_id,
            case_version=case.version,
            layer=case.layer,
            scenario_id=case.scenario_id,
            scenario_version=case.scenario_version,
            evaluator_name=case.evaluator_name,
            evaluator_version=case.evaluator_version,
            operation_name=case.operation_name,
            model=self._executor.model_identity,
            status=EvalStatus.COMPLETED,
            duration_ms=(time.monotonic() - started) * 1000,
            metrics=metrics,
        )

    def _failed_result(
        self,
        plan: EvalCasePlan,
        invocation_id: EvalInvocationId,
        status: EvalStatus,
        error: BaseException,
        started: float,
    ) -> EvalCaseResult:
        case = plan.case
        return EvalCaseResult(
            run_id=self._run_id,
            invocation_id=invocation_id,
            case_id=case.case_id,
            case_version=case.version,
            layer=case.layer,
            scenario_id=case.scenario_id,
            scenario_version=case.scenario_version,
            evaluator_name=case.evaluator_name,
            evaluator_version=case.evaluator_version,
            operation_name=case.operation_name,
            model=self._executor.model_identity,
            status=status,
            duration_ms=(time.monotonic() - started) * 1000,
            failure_code=_failure_code(error),
        )

    def _build_manifest(
        self,
        *,
        started_at: datetime,
        completed_at: datetime,
        plans: Sequence[EvalCasePlan],
        results: tuple[EvalCaseResult, ...],
        cancelled: bool,
        quality_gate_enabled: bool,
        quality_gate_passed: bool | None,
    ) -> EvalRunManifest:
        completed_cases = sum(1 for result in results if result.succeeded)
        failed_cases = len(results) - completed_cases
        total = len(plans)
        if cancelled:
            status = EvalRunStatus.CANCELLED
        elif failed_cases == 0 and len(results) == total:
            status = EvalRunStatus.COMPLETED
        else:
            status = EvalRunStatus.PARTIAL
        return EvalRunManifest(
            run_id=self._run_id,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            model=self._executor.model_identity,
            scenario_ids=tuple(sorted({plan.case.scenario_id for plan in plans})),
            case_ids=tuple(plan.case.case_id.value for plan in plans),
            total_cases=total,
            completed_cases=completed_cases,
            failed_cases=failed_cases,
            cancelled=cancelled,
            quality_gate_enabled=quality_gate_enabled,
            quality_gate_passed=quality_gate_passed,
        )


def _failure_code(error: BaseException) -> str:
    if isinstance(error, EvalModelError):
        return "model_error"
    if isinstance(error, EvalSetupError):
        return "setup_error"
    if isinstance(error, EvalEvaluatorError):
        return "evaluator_error"
    return "case_error"


def _score(
    plan: EvalCasePlan, observation: ProductionObservation
) -> tuple[EvalMetric, ...]:
    case = plan.case
    expectation = plan.expectation
    if case.evaluator_name == "recon":
        if not isinstance(observation, ReconObservation) or not isinstance(
            expectation, ReconExpectation
        ):
            raise EvalEvaluatorError("recon case received a non-recon observation")
        return evaluate_recon(expectation, observation)
    if case.evaluator_name == "semantic":
        if not isinstance(observation, SemanticObservation) or not isinstance(
            expectation, SemanticExpectation
        ):
            raise EvalEvaluatorError(
                "semantic case received a non-semantic observation"
            )
        return evaluate_semantic(expectation, observation)
    if case.evaluator_name == "geography":
        if not isinstance(observation, GeographyObservation) or not isinstance(
            expectation, GeographyExpectation
        ):
            raise EvalEvaluatorError(
                "geography case received a non-geography observation"
            )
        return evaluate_geography(expectation, observation)
    if case.evaluator_name == "relationship":
        if not isinstance(observation, RelationshipObservation) or not isinstance(
            expectation, RelationshipExpectation
        ):
            raise EvalEvaluatorError(
                "relationship case received a non-relationship observation"
            )
        return evaluate_relationship(expectation, observation)
    if case.evaluator_name == "source_analysis":
        if not isinstance(observation, SourceAnalystObservation) or not isinstance(
            expectation, SourceAnalystExpectation
        ):
            raise EvalEvaluatorError(
                "source-analysis case received a non-analysis observation"
            )
        return evaluate_source_analyst(expectation, observation)
    raise EvalEvaluatorError(f"unknown evaluator {case.evaluator_name!r}")


def select_case_plans(case_ids: Sequence[str]) -> tuple[EvalCasePlan, ...]:
    """Resolve case IDs to plans in the requested order, failing closed."""
    if not case_ids:
        return list_case_plans()
    return tuple(get_case_plan(case_id) for case_id in case_ids)
