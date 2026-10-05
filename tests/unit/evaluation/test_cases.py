# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Evaluation case-registry tests (PR 15) — matrix ED15-8..ED15-10."""

from __future__ import annotations

import pytest

from darkula.evaluation.cases import (
    EvalCaseNotFoundError,
    get_case_plan,
    get_eval_case,
    list_case_plans,
    list_eval_cases,
)
from darkula.evaluation.domain import EvalLayer


def test_registry_order_deterministic() -> None:
    first = [case.case_id.value for case in list_eval_cases()]
    second = [case.case_id.value for case in list_eval_cases()]
    assert first == second
    assert first[0] == "EV-RECON-001"
    assert "EV-SA-004" in first


def test_all_layers_covered() -> None:
    layers = {case.layer for case in list_eval_cases()}
    assert layers == {
        EvalLayer.RECON,
        EvalLayer.SEMANTIC_EXTRACTION,
        EvalLayer.GEOGRAPHY,
        EvalLayer.RELATIONSHIP_EXTRACTION,
        EvalLayer.SOURCE_ANALYSIS,
    }


def test_every_case_plan_internally_consistent() -> None:
    for plan in list_case_plans():
        assert plan.case.evaluator_name in {
            "recon",
            "semantic",
            "geography",
            "relationship",
            "source_analysis",
        }
        assert plan.case.scenario_id
        assert plan.case.scenario_version >= 1
        assert plan.case.version >= 1


def test_get_eval_case_known() -> None:
    case = get_eval_case("EV-RECON-001")
    assert case.case_id.value == "EV-RECON-001"
    assert case.layer is EvalLayer.RECON


def test_unknown_case_typed_error() -> None:
    with pytest.raises(EvalCaseNotFoundError):
        get_eval_case("EV-NOPE-999")


def test_invalid_version_typed_error() -> None:
    with pytest.raises(EvalCaseNotFoundError):
        get_case_plan("EV-RECON-001", version=0)


def test_duplicate_keys_absent() -> None:
    keys = [plan.case.key for plan in list_case_plans()]
    assert len(keys) == len(set(keys))
