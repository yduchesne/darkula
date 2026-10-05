# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic metric-primitive tests (PR 15) — matrix EM15-1..EM15-12."""

from __future__ import annotations

import pytest

from darkula.evaluation.metrics import (
    CategoricalScore,
    GroundingScore,
    RubricCoverage,
    SetMatchScore,
)

VERSION = "v1"


def _named(score: SetMatchScore) -> dict[str, float]:
    return {metric.name: metric.value for metric in score.to_metrics(VERSION)}


class TestSetMatch:
    """EM15-1..EM15-7."""

    def test_em15_1_exact_set(self) -> None:
        score = SetMatchScore.between("x", ["a", "b"], ["b", "a"])
        assert score.precision == score.recall == score.f1 == 1.0
        assert score.true_positives == 2
        assert score.false_positives == score.false_negatives == 0

    def test_em15_2_false_positive(self) -> None:
        score = SetMatchScore.between("x", ["a"], ["a", "b"])
        assert score.true_positives == 1
        assert score.false_positives == 1
        assert score.precision == 0.5
        assert score.recall == 1.0

    def test_em15_3_false_negative(self) -> None:
        score = SetMatchScore.between("x", ["a", "b"], ["a"])
        assert score.false_negatives == 1
        assert score.recall == 0.5
        assert score.precision == 1.0

    def test_em15_4_both_empty(self) -> None:
        score = SetMatchScore.between("x", [], [])
        assert score.precision == score.recall == score.f1 == 1.0

    def test_em15_5_expected_empty_actual_nonempty(self) -> None:
        score = SetMatchScore.between("x", [], ["a"])
        assert score.precision == 0.0
        assert score.recall == 1.0
        assert score.f1 == 0.0

    def test_em15_6_actual_empty_expected_nonempty(self) -> None:
        score = SetMatchScore.between("x", ["a"], [])
        assert score.precision == 0.0
        assert score.recall == 0.0
        assert score.f1 == 0.0

    def test_em15_7_duplicates_deterministic(self) -> None:
        score = SetMatchScore.between("x", ["a", "a", "b"], ["a", "b", "b"])
        assert score.duplicate_expected == 1
        assert score.duplicate_actual == 1
        assert score.true_positives == 2

    def test_em15_12_repeat_identical(self) -> None:
        first = _named(SetMatchScore.between("x", ["a"], ["a", "b"]))
        second = _named(SetMatchScore.between("x", ["a"], ["a", "b"]))
        assert first == second

    def test_metric_values_bounded_and_counts_preserved(self) -> None:
        metrics = SetMatchScore.between("x", ["a", "b"], ["a"]).to_metrics(VERSION)
        for metric in metrics:
            assert 0.0 <= metric.value <= 1.0
            assert metric.evaluator_version == VERSION
        by_name = {metric.name: metric for metric in metrics}
        assert by_name["x.true_positives"].numerator == 1
        assert by_name["x.false_negatives"].numerator == 1


class TestCategorical:
    """EM15-8..EM15-9."""

    def test_em15_8_equal(self) -> None:
        score = CategoricalScore(name="d", expected="QUALIFY", actual="QUALIFY")
        assert score.matches is True
        assert score.to_metrics(VERSION)[0].value == 1.0

    def test_em15_9_unequal(self) -> None:
        score = CategoricalScore(name="d", expected="QUALIFY", actual="REJECT")
        assert score.matches is False
        assert score.to_metrics(VERSION)[0].value == 0.0


class TestGrounding:
    """EM15-10..EM15-11."""

    def test_em15_10_valid_ratio(self) -> None:
        score = GroundingScore(name="g", valid=3, invalid=0)
        assert score.ratio == 1.0
        by_name = {m.name: m for m in score.to_metrics(VERSION)}
        assert by_name["g.grounding_ratio"].value == 1.0

    def test_em15_11_invalid_refs_counted(self) -> None:
        score = GroundingScore(name="g", valid=1, invalid=3)
        assert score.ratio == 0.25
        by_name = {m.name: m for m in score.to_metrics(VERSION)}
        assert by_name["g.invalid_references"].numerator == 3

    def test_empty_grounding_vacuous(self) -> None:
        assert GroundingScore(name="g", valid=0, invalid=0).ratio == 1.0

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValueError):
            GroundingScore(name="g", valid=-1, invalid=0)


class TestRubricCoverage:
    def test_coverage_and_missing(self) -> None:
        score = RubricCoverage.between(
            "r", {"a", "b", "c"}, {"a", "b"}, unsupported={"bad"}
        )
        assert score.coverage == pytest.approx(2 / 3)
        assert score.missing == frozenset({"c"})
        assert score.unsupported == frozenset({"bad"})

    def test_empty_rubric_vacuous(self) -> None:
        assert RubricCoverage.between("r", set(), set()).coverage == 1.0
