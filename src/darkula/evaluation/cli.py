# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit opt-in evaluation command line (PR 15).

This command is never invoked by ``--qa``/``--sec``/``--intg`` or ordinary
CI. It is explicit opt-in:

- it requires the evaluation subsystem to be enabled in configuration;
- it fails fast when a live run is requested while the composed ``LlmClient``
  is the deterministic fake (the fake never masquerades as live);
- it writes bounded artifacts under the configured output directory;
- it distinguishes configuration errors from case/gate failures via exit
  codes and prints only safe, concise output.

Deterministic replay uses prebuilt, bounded observation fixtures so metric
regressions are testable without a live model or infrastructure. Live-model
execution through the production-capability port is documented in
``docs/EVALUATIONS.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from darkula.config.loader import load_settings
from darkula.evaluation.artifacts import EvalArtifactStore
from darkula.evaluation.cases import EvalCasePlan, list_eval_cases
from darkula.evaluation.domain import EvalLayer, EvalModelIdentity
from darkula.evaluation.evaluators import (
    GeographyObservation,
    ReconObservation,
    RelationshipObservation,
    SemanticObservation,
    SemanticOccurrence,
    SourceAnalystObservation,
)
from darkula.evaluation.runner import (
    EvalConfigurationError,
    EvalRunner,
    ProductionObservation,
    select_case_plans,
)

EXIT_OK = 0
EXIT_CASE_FAILURE = 1
EXIT_CONFIGURATION_ERROR = 2

__all__ = ["ReplayEvalExecutor", "build_parser", "main"]


class ReplayEvalExecutor:
    """Deterministic executor replaying bounded observation fixtures."""

    def __init__(
        self, *, model_identity: EvalModelIdentity, fixtures: dict[str, Any]
    ) -> None:
        self._model_identity = model_identity
        self._fixtures = fixtures

    @property
    def model_identity(self) -> EvalModelIdentity:
        """Configured identity of the model under test."""
        return self._model_identity

    async def execute(self, plan: EvalCasePlan) -> ProductionObservation:
        """Return the prebuilt observation for ``plan`` or fail closed."""
        case_id = plan.case.case_id.value
        payload = self._fixtures.get(case_id)
        if payload is None:
            raise EvalConfigurationError(
                f"no replay observation provided for case {case_id!r}"
            )
        return _observation_from_payload(plan.case.layer, payload)


def _observation_from_payload(
    layer: EvalLayer, payload: dict[str, Any]
) -> ProductionObservation:
    if layer is EvalLayer.RECON:
        return ReconObservation(
            source_type=str(payload.get("source_type", "")),
            useful_paths=tuple(str(item) for item in payload.get("useful_paths", [])),
            disposition=str(payload.get("disposition", "")),
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs", [])),
            mirror_hosts=tuple(str(item) for item in payload.get("mirror_hosts", [])),
        )
    if layer is EvalLayer.SEMANTIC_EXTRACTION:
        occurrences = tuple(
            SemanticOccurrence(
                entity_type=str(item["entity_type"]),
                normalized_value=str(item["normalized_value"]),
                start=int(item["start"]),
                end=int(item["end"]),
            )
            for item in payload.get("occurrences", [])
        )
        return SemanticObservation(occurrences=occurrences)
    if layer is EvalLayer.GEOGRAPHY:
        return GeographyObservation(
            resolutions=tuple(str(item) for item in payload.get("resolutions", [])),
            canonical_fields=tuple(
                str(item) for item in payload.get("canonical_fields", [])
            ),
        )
    if layer is EvalLayer.RELATIONSHIP_EXTRACTION:
        return RelationshipObservation(
            assertions=tuple(str(item) for item in payload.get("assertions", [])),
            supports=tuple(str(item) for item in payload.get("supports", [])),
        )
    return SourceAnalystObservation(
        covered_rubric_items=tuple(
            str(item) for item in payload.get("covered_rubric_items", [])
        ),
        evidence_refs=tuple(str(item) for item in payload.get("evidence_refs", [])),
        conclusions=tuple(str(item) for item in payload.get("conclusions", [])),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the deterministic CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="darkula-eval",
        description=(
            "Run Darkula's deterministic evaluation suite. Explicit opt-in; "
            "never part of ordinary builds or CI."
        ),
    )
    parser.add_argument("--list", action="store_true", help="list registered cases")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="restrict to a case ID (repeatable)",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="path to a JSON observation fixture for deterministic replay",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="artifact output root (defaults to configured evaluation.output_dir)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="request a live-model run (fails fast when the model is not live)",
    )
    parser.add_argument(
        "--quality-gate",
        action="store_true",
        help="enable explicit quality gating",
    )
    parser.add_argument(
        "--no-fail-on-case-error",
        action="store_true",
        help="do not exit non-zero when a case fails",
    )
    return parser


def _load_fixtures(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalConfigurationError("unable to read evaluation fixtures") from exc
    if not isinstance(loaded, dict):
        raise EvalConfigurationError("evaluation fixtures must be a JSON object")
    cases = loaded.get("cases")
    if not isinstance(cases, dict):
        raise EvalConfigurationError("evaluation fixtures require a 'cases' object")
    return cases


def main(argv: Sequence[str] | None = None) -> int:
    """Run the evaluation CLI and return a bounded process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        for case in list_eval_cases():
            print(
                f"{case.case_id} v{case.version} {case.layer.value} "
                f"{case.scenario_id}@{case.scenario_version}"
            )
        return EXIT_OK

    try:
        settings = load_settings()
    except Exception as exc:
        print("evaluation configuration error", file=sys.stderr)
        print(str(exc).splitlines()[0] if str(exc) else "", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR

    if not settings.evaluation.enabled:
        print(
            "evaluation is not enabled (set DARKULA_EVALUATION__ENABLED=true)",
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION_ERROR

    provider = settings.llm.driver.value
    model_name = settings.llm.provider.model_name or "fixture-model"
    live_identity = provider != "fake"
    model_identity = EvalModelIdentity(
        provider=provider,
        model_name=model_name,
        profile=settings.config_profile,
    )
    if args.live and not live_identity:
        print(
            "live evaluation was requested but the composed model is not live",
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION_ERROR
    if args.live:
        print(
            "live execution is driven by the production-capability port, not "
            "the deterministic replay CLI",
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION_ERROR

    try:
        case_ids = tuple(args.case) or tuple(settings.evaluation.case_ids)
        plans = select_case_plans(case_ids)
    except KeyError as exc:
        print(f"evaluation case error: {exc}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR

    if args.fixtures is None:
        print(
            "no observation fixtures provided; deterministic replay requires "
            "--fixtures (live execution uses the production-capability port)",
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION_ERROR
    try:
        fixtures = _load_fixtures(args.fixtures)
    except EvalConfigurationError as exc:
        print(f"evaluation configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR

    executor = ReplayEvalExecutor(model_identity=model_identity, fixtures=fixtures)
    output = args.output or settings.evaluation.output_dir
    artifacts = EvalArtifactStore(output)
    quality_gate_enabled = args.quality_gate or settings.evaluation.quality_gate_enabled
    gate = (
        (lambda results: all(result.succeeded for result in results))
        if quality_gate_enabled
        else None
    )
    runner = EvalRunner(executor=executor, artifacts=artifacts, quality_gate=gate)

    try:
        manifest = asyncio.run(
            runner.run(
                plans,
                live_requested=args.live,
                quality_gate_enabled=quality_gate_enabled,
            )
        )
    except EvalConfigurationError as exc:
        print(f"evaluation configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR

    print(
        f"run {manifest.run_id} {manifest.status.value}: "
        f"{manifest.completed_cases}/{manifest.total_cases} cases completed"
    )
    print(f"artifacts: {artifacts.run_dir(manifest.run_id.value)}")

    fail_on_case_error = (
        settings.evaluation.fail_on_case_error and not args.no_fail_on_case_error
    )
    if fail_on_case_error and manifest.failed_cases > 0:
        return EXIT_CASE_FAILURE
    if manifest.quality_gate_passed is False:
        return EXIT_CASE_FAILURE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
