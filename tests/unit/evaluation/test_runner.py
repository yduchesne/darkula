# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Evaluation runner/artifact tests (PR 15) — matrix ER15-1..ER15-12."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from darkula.evaluation.artifacts import EvalArtifactStore
from darkula.evaluation.cases import get_case_plan, list_case_plans
from darkula.evaluation.domain import (
    EvalModelIdentity,
    EvalRunStatus,
)
from darkula.evaluation.evaluators import (
    ReconExpectation,
    ReconObservation,
    SemanticObservation,
)
from darkula.evaluation.runner import (
    EvalConfigurationError,
    EvalModelError,
    EvalRunner,
    EvalSetupError,
    ProductionObservation,
    select_case_plans,
)

FIXED_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class FakeExecutor:
    """Deterministic production-capability test double."""

    def __init__(
        self,
        *,
        provider: str = "fake",
        observations: dict[str, ProductionObservation] | None = None,
        raise_for: dict[str, BaseException] | None = None,
    ) -> None:
        self._identity = EvalModelIdentity(provider=provider, model_name="fixture")
        self._observations = observations or {}
        self._raise_for = raise_for or {}
        self.seen: list[str] = []

    @property
    def model_identity(self) -> EvalModelIdentity:
        return self._identity

    async def execute(self, plan):  # type: ignore[no-untyped-def]
        case_id = plan.case.case_id.value
        self.seen.append(case_id)
        if case_id in self._raise_for:
            raise self._raise_for[case_id]
        return self._observations.get(case_id, ReconObservation(source_type="forum"))


def _clock() -> datetime:
    return FIXED_NOW


def _store(tmp_path: Path) -> EvalArtifactStore:
    return EvalArtifactStore(tmp_path)


def _recon_observation() -> ReconObservation:
    plan = get_case_plan("EV-RECON-001")
    expectation = plan.expectation
    assert isinstance(expectation, ReconExpectation)
    return ReconObservation(
        source_type=expectation.source_type,
        useful_paths=tuple(expectation.useful_paths),
        disposition=expectation.disposition,
        evidence_refs=tuple(expectation.allowed_evidence_refs),
        mirror_hosts=tuple(expectation.mirror_hosts),
    )


class TestRunner:
    """ER15-1..ER15-12."""

    @pytest.mark.asyncio
    async def test_er15_1_one_deterministic_case(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(observations={"EV-RECON-001": _recon_observation()})
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-fixed"
        )
        manifest = await runner.run([get_case_plan("EV-RECON-001")])
        assert manifest.status is EvalRunStatus.COMPLETED
        assert manifest.completed_cases == 1
        results = store.read_results("run-fixed")
        assert len(results) == 1
        assert results[0]["status"] == "COMPLETED"
        assert results[0]["run_id"] == "run-fixed"

    @pytest.mark.asyncio
    async def test_er15_2_stable_multi_case_ordering(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(
            observations={
                "EV-RECON-001": _recon_observation(),
                "EV-RECON-002": _recon_observation(),
                "EV-RECON-003": _recon_observation(),
            }
        )
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-order"
        )
        plans = select_case_plans(["EV-RECON-001", "EV-RECON-002", "EV-RECON-003"])
        await runner.run(list(plans))
        assert executor.seen == ["EV-RECON-001", "EV-RECON-002", "EV-RECON-003"]

    @pytest.mark.asyncio
    async def test_er15_3_bounded_case_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(raise_for={"EV-RECON-001": RuntimeError("raw secret")})
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-case"
        )
        manifest = await runner.run([get_case_plan("EV-RECON-001")])
        results = store.read_results("run-case")
        assert manifest.status is EvalRunStatus.PARTIAL
        assert results[0]["status"] == "CASE_ERROR"
        assert results[0]["failure_code"] == "case_error"
        assert "raw secret" not in str(results[0])

    @pytest.mark.asyncio
    async def test_er15_4_model_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(
            raise_for={"EV-RECON-001": EvalModelError("provider returned an error")}
        )
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-model"
        )
        await runner.run([get_case_plan("EV-RECON-001")])
        assert store.read_results("run-model")[0]["status"] == "MODEL_ERROR"

    @pytest.mark.asyncio
    async def test_er15_5_evaluator_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # Wrong observation type for a recon case -> evaluator error.
        executor = FakeExecutor(
            observations={
                "EV-RECON-001": SemanticObservation(occurrences=()),
            }
        )
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-eval"
        )
        await runner.run([get_case_plan("EV-RECON-001")])
        assert store.read_results("run-eval")[0]["status"] == "EVALUATOR_ERROR"

    @pytest.mark.asyncio
    async def test_er15_4_setup_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(
            raise_for={"EV-RECON-001": EvalSetupError("browser not provisioned")}
        )
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-setup"
        )
        await runner.run([get_case_plan("EV-RECON-001")])
        assert store.read_results("run-setup")[0]["status"] == "SETUP_ERROR"

    @pytest.mark.asyncio
    async def test_er15_6_cancellation_propagates(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(raise_for={"EV-RECON-001": asyncio.CancelledError()})
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-cancel"
        )
        with pytest.raises(asyncio.CancelledError):
            await runner.run([get_case_plan("EV-RECON-001")])
        manifest = store.read_manifest("run-cancel")
        assert manifest["status"] == "CANCELLED"
        assert manifest["cancelled"] is True

    @pytest.mark.asyncio
    async def test_er15_7_live_requested_with_fake_driver_fails(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(provider="fake")
        runner = EvalRunner(executor=executor, artifacts=store, clock=_clock)
        with pytest.raises(EvalConfigurationError, match="not live"):
            await runner.run([get_case_plan("EV-RECON-001")], live_requested=True)

    @pytest.mark.asyncio
    async def test_er15_8_artifacts_safe(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(observations={"EV-RECON-001": _recon_observation()})
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-safe"
        )
        await runner.run([get_case_plan("EV-RECON-001")])
        text = (tmp_path / "run-safe" / "results.jsonl").read_text()
        for forbidden in ("prompt", "system_prompt", "http://", "FakeWorldTruth"):
            assert forbidden not in text

    @pytest.mark.asyncio
    async def test_er15_9_incomplete_distinguishable(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(
            observations={"EV-RECON-001": _recon_observation()},
            raise_for={"EV-RECON-002": EvalSetupError("browser missing")},
        )
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-partial"
        )
        manifest = await runner.run(
            [get_case_plan("EV-RECON-001"), get_case_plan("EV-RECON-002")]
        )
        # Both execute, but one fails; status is PARTIAL, not COMPLETED.
        assert manifest.status is EvalRunStatus.PARTIAL
        assert manifest.failed_cases == 1
        assert store.read_manifest("run-partial")["status"] == "PARTIAL"

    @pytest.mark.asyncio
    async def test_er15_10_gate_disabled_does_not_fail(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(observations={"EV-RECON-001": _recon_observation()})
        runner = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-nogate"
        )
        manifest = await runner.run(
            [get_case_plan("EV-RECON-001")], quality_gate_enabled=False
        )
        assert manifest.quality_gate_enabled is False
        assert manifest.quality_gate_passed is None

    @pytest.mark.asyncio
    async def test_er15_11_explicit_gate(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(observations={"EV-RECON-001": _recon_observation()})
        runner = EvalRunner(
            executor=executor,
            artifacts=store,
            clock=_clock,
            run_id="run-gate",
            quality_gate=lambda results: all(r.succeeded for r in results),
        )
        manifest = await runner.run(
            [get_case_plan("EV-RECON-001")], quality_gate_enabled=True
        )
        assert manifest.quality_gate_passed is True

    @pytest.mark.asyncio
    async def test_gate_enabled_without_gate_fails(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(observations={"EV-RECON-001": _recon_observation()})
        runner = EvalRunner(executor=executor, artifacts=store, clock=_clock)
        with pytest.raises(EvalConfigurationError, match="quality gate"):
            await runner.run([get_case_plan("EV-RECON-001")], quality_gate_enabled=True)

    @pytest.mark.asyncio
    async def test_er15_12_rerun_new_run_id_same_cases(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        executor = FakeExecutor(observations={"EV-RECON-001": _recon_observation()})
        first = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-a"
        )
        second = EvalRunner(
            executor=executor, artifacts=store, clock=_clock, run_id="run-b"
        )
        manifest_a = await first.run([get_case_plan("EV-RECON-001")])
        manifest_b = await second.run([get_case_plan("EV-RECON-001")])
        assert manifest_a.run_id != manifest_b.run_id
        assert manifest_a.case_ids == manifest_b.case_ids

    @pytest.mark.asyncio
    async def test_empty_selection_fails(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = EvalRunner(executor=FakeExecutor(), artifacts=store, clock=_clock)
        with pytest.raises(EvalConfigurationError, match="no evaluation cases"):
            await runner.run([])

    def test_select_case_plans_unknown_fails(self) -> None:
        with pytest.raises(KeyError):
            select_case_plans(["EV-NOPE-999"])

    def test_select_all_plans(self) -> None:
        assert len(select_case_plans([])) == len(list_case_plans())
