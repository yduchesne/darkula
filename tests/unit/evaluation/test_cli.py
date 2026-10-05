# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Evaluation CLI tests (PR 15). Deterministic; no live model or network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from darkula.evaluation.cases import get_case_plan
from darkula.evaluation.cli import (
    EXIT_CASE_FAILURE,
    EXIT_CONFIGURATION_ERROR,
    EXIT_OK,
    ReplayEvalExecutor,
    _observation_from_payload,
    build_parser,
    main,
)
from darkula.evaluation.domain import EvalLayer, EvalModelIdentity
from darkula.evaluation.evaluators import ReconExpectation, ReconObservation


def _fixtures() -> dict[str, Any]:
    plan = get_case_plan("EV-RECON-001")
    expectation = plan.expectation
    assert isinstance(expectation, ReconExpectation)
    return {
        "EV-RECON-001": {
            "source_type": expectation.source_type,
            "useful_paths": sorted(expectation.useful_paths),
            "disposition": expectation.disposition,
            "evidence_refs": sorted(expectation.allowed_evidence_refs),
            "mirror_hosts": sorted(expectation.mirror_hosts),
        }
    }


def test_parser_has_eval_options() -> None:
    parser = build_parser()
    args = parser.parse_args(["--list"])
    assert args.list is True


def test_list_exits_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "EV-RECON-001" in out


def test_observation_from_payload_recon() -> None:
    observation = _observation_from_payload(
        EvalLayer.RECON,
        {"source_type": "forum", "useful_paths": ["/"], "disposition": "QUALIFY"},
    )
    assert isinstance(observation, ReconObservation)
    assert observation.source_type == "forum"


def test_observation_from_payload_all_layers() -> None:
    assert _observation_from_payload(EvalLayer.SEMANTIC_EXTRACTION, {})
    assert _observation_from_payload(EvalLayer.GEOGRAPHY, {})
    assert _observation_from_payload(EvalLayer.RELATIONSHIP_EXTRACTION, {})
    assert _observation_from_payload(EvalLayer.SOURCE_ANALYSIS, {})


async def _run_replay_executor() -> None:
    executor = ReplayEvalExecutor(
        model_identity=EvalModelIdentity(provider="fake", model_name="fake"),
        fixtures=_fixtures(),
    )
    result = await executor.execute(get_case_plan("EV-RECON-001"))
    assert isinstance(result, ReconObservation)
    assert result.source_type == "forum"


def test_replay_executor() -> None:
    import asyncio

    asyncio.run(_run_replay_executor())


def test_disabled_evaluation_returns_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DARKULA_EVALUATION__ENABLED", "false")
    assert main([]) == EXIT_CONFIGURATION_ERROR


def test_run_with_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DARKULA_EVALUATION__ENABLED", "true")
    monkeypatch.setenv("DARKULA_LLM__DRIVER", "fake")
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"cases": _fixtures()}), encoding="utf-8")
    out_dir = tmp_path / "results"
    code = main(
        [
            "--case",
            "EV-RECON-001",
            "--fixtures",
            str(fixtures),
            "--output",
            str(out_dir),
        ]
    )
    assert code == EXIT_OK
    assert list(out_dir.glob("*/manifest.json"))


def test_live_request_with_fake_driver_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DARKULA_EVALUATION__ENABLED", "true")
    monkeypatch.setenv("DARKULA_LLM__DRIVER", "fake")
    assert main(["--live", "--case", "EV-RECON-001"]) == EXIT_CONFIGURATION_ERROR


def test_live_request_with_live_driver_requires_production_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKULA_EVALUATION__ENABLED", "true")
    monkeypatch.setenv("DARKULA_LLM__DRIVER", "openai")
    monkeypatch.setenv("DARKULA_LLM__PROVIDER__MODEL_NAME", "gpt-4o")
    monkeypatch.setenv("DARKULA_LLM__PROVIDER__API_KEY", "fake-key")
    assert main(["--live", "--case", "EV-RECON-001"]) == EXIT_CONFIGURATION_ERROR


def test_no_fixtures_returns_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKULA_EVALUATION__ENABLED", "true")
    assert main(["--case", "EV-RECON-001"]) == EXIT_CONFIGURATION_ERROR


def test_unknown_case_returns_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DARKULA_EVALUATION__ENABLED", "true")
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"cases": {}}), encoding="utf-8")
    assert (
        main(["--case", "EV-NOPE-999", "--fixtures", str(fixtures)])
        == EXIT_CONFIGURATION_ERROR
    )


def test_fail_on_case_error_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DARKULA_EVALUATION__ENABLED", "true")
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"cases": {}}), encoding="utf-8")
    code = main(
        [
            "--case",
            "EV-RECON-001",
            "--fixtures",
            str(fixtures),
            "--output",
            str(tmp_path / "r"),
        ]
    )
    assert code == EXIT_CASE_FAILURE


def test_quality_gate_flagged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DARKULA_EVALUATION__ENABLED", "true")
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"cases": _fixtures()}), encoding="utf-8")
    code = main(
        [
            "--case",
            "EV-RECON-001",
            "--fixtures",
            str(fixtures),
            "--output",
            str(tmp_path / "gate"),
            "--quality-gate",
        ]
    )
    assert code == EXIT_OK


# Keep the fixtures helper available to future CLI tests.
