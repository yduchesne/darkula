# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, atomic evaluation artifacts (PR 15).

Run artifacts are written under ``.eval-results/<run-id>/``:

- ``results.jsonl`` — one bounded JSON object per case result;
- ``manifest.json`` — the finalized run manifest.

The manifest is replaced atomically (write-temp + ``os.replace``) so a
reader never observes a half-written manifest, and a partial run is written
with a ``PARTIAL``/``CANCELLED`` status rather than a false ``COMPLETED``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from darkula.evaluation.domain import (
    EvalCaseResult,
    EvalMetric,
    EvalRunManifest,
)

#: Default output root (git-ignored).
DEFAULT_EVAL_RESULTS_DIR = Path(".eval-results")


def metric_to_dict(metric: EvalMetric) -> dict[str, Any]:
    """Serialize one metric deterministically."""
    payload: dict[str, Any] = {
        "name": metric.name,
        "evaluator_version": metric.evaluator_version,
        "value": metric.value,
    }
    if metric.numerator is not None:
        payload["numerator"] = metric.numerator
    if metric.denominator is not None:
        payload["denominator"] = metric.denominator
    return payload


def result_to_dict(result: EvalCaseResult) -> dict[str, Any]:
    """Serialize one bounded case result deterministically."""
    payload: dict[str, Any] = {
        "run_id": result.run_id.value,
        "invocation_id": result.invocation_id.value,
        "case_id": result.case_id.value,
        "case_version": result.case_version,
        "layer": result.layer.value,
        "scenario_id": result.scenario_id,
        "scenario_version": result.scenario_version,
        "evaluator_name": result.evaluator_name,
        "evaluator_version": result.evaluator_version,
        "operation_name": result.operation_name,
        "model": {
            "provider": result.model.provider,
            "model_name": result.model.model_name,
            "profile": result.model.profile,
        },
        "status": result.status.value,
        "duration_ms": result.duration_ms,
        "metrics": [metric_to_dict(metric) for metric in result.metrics],
    }
    if result.failure_code is not None:
        payload["failure_code"] = result.failure_code
    return payload


def manifest_to_dict(manifest: EvalRunManifest) -> dict[str, Any]:
    """Serialize the bounded run manifest deterministically."""
    return {
        "run_id": manifest.run_id.value,
        "started_at": manifest.started_at.isoformat(),
        "completed_at": manifest.completed_at.isoformat(),
        "status": manifest.status.value,
        "model": {
            "provider": manifest.model.provider,
            "model_name": manifest.model.model_name,
            "profile": manifest.model.profile,
        },
        "scenario_ids": list(manifest.scenario_ids),
        "case_ids": list(manifest.case_ids),
        "total_cases": manifest.total_cases,
        "completed_cases": manifest.completed_cases,
        "failed_cases": manifest.failed_cases,
        "cancelled": manifest.cancelled,
        "quality_gate_enabled": manifest.quality_gate_enabled,
        "quality_gate_passed": manifest.quality_gate_passed,
    }


class EvalArtifactError(RuntimeError):
    """Output-directory/serialization failure (never a false completed run)."""


class EvalArtifactStore:
    """Filesystem-backed bounded artifact store for one output root."""

    def __init__(self, root: Path | str = DEFAULT_EVAL_RESULTS_DIR) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """Configured output root."""
        return self._root

    def run_dir(self, run_id: str) -> Path:
        """Return (but do not create) the directory for one run."""
        return self._root / run_id

    def begin_run(self, run_id: str) -> Path:
        """Create the run directory (and output root) or fail closed."""
        try:
            run_dir = self.run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - defensive filesystem failure
            raise EvalArtifactError(
                "unable to create evaluation output directory"
            ) from exc
        return run_dir

    def append_result(self, run_id: str, result: EvalCaseResult) -> None:
        """Append one bounded result JSON line and flush it durably."""
        line = json.dumps(
            result_to_dict(result),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            with (self.run_dir(run_id) / "results.jsonl").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:  # pragma: no cover - defensive filesystem failure
            raise EvalArtifactError(
                "unable to write evaluation result artifact"
            ) from exc

    def write_manifest(self, run_id: str, manifest: EvalRunManifest) -> Path:
        """Atomically replace the run manifest."""
        payload = json.dumps(
            manifest_to_dict(manifest),
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
        run_dir = self.run_dir(run_id)
        tmp_path = run_dir / "manifest.json.tmp"
        final_path = run_dir / "manifest.json"
        try:
            tmp_path.write_text(payload + "\n", encoding="utf-8")
            os.replace(tmp_path, final_path)
        except OSError as exc:  # pragma: no cover - defensive filesystem failure
            raise EvalArtifactError("unable to write evaluation manifest") from exc
        return final_path

    def read_manifest(self, run_id: str) -> Mapping[str, Any]:
        """Read and parse a finalized manifest (test/CLI convenience)."""
        text = (self.run_dir(run_id) / "manifest.json").read_text(encoding="utf-8")
        loaded = json.loads(text)
        if not isinstance(loaded, dict):  # pragma: no cover - defensive
            raise EvalArtifactError("manifest artifact is not a JSON object")
        return loaded

    def read_results(self, run_id: str) -> list[Mapping[str, Any]]:
        """Read and parse all result lines (test/CLI convenience)."""
        path = self.run_dir(run_id) / "results.jsonl"
        if not path.exists():
            return []
        results: list[Mapping[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            loaded = json.loads(line)
            if not isinstance(loaded, dict):  # pragma: no cover - defensive
                raise EvalArtifactError("result artifact line is not a JSON object")
            results.append(loaded)
        return results


__all__ = [
    "DEFAULT_EVAL_RESULTS_DIR",
    "EvalArtifactError",
    "EvalArtifactStore",
    "manifest_to_dict",
    "metric_to_dict",
    "result_to_dict",
]
