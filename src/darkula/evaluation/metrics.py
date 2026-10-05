# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic, versioned metric primitives (PR 15).

Scoring is deliberately narrow and deterministic: the same expected/actual
inputs always produce the same score. No metric calls an LLM, the network,
or any production service.

Set semantics (documented and tested):

- ``TP = |expected ∩ actual|``, ``FP = |actual - expected|``,
  ``FN = |expected - actual|``;
- duplicates in the inputs are counted but do not change the set-based
  ``TP``/``FP``/``FN`` (two identical occurrences are one set member);
- ``expected`` empty and ``actual`` empty: ``precision = recall = f1 = 1``;
- ``expected`` empty and ``actual`` non-empty: ``precision = 0``,
  ``recall = 1``, ``f1 = 0``;
- ``expected`` non-empty and ``actual`` empty: ``precision = 0``
  (platform convention: no true positives and expected positives exist),
  ``recall = 0``, ``f1 = 0``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Self

from darkula.evaluation.domain import EvalMetric

__all__ = [
    "CategoricalScore",
    "GroundingScore",
    "RubricCoverage",
    "SetMatchScore",
]


def _dedupe(items: Iterable[str]) -> tuple[frozenset[str], int]:
    values = list(items)
    unique = frozenset(values)
    return unique, len(values) - len(unique)


def _normalize(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return min(1.0, numerator / denominator)


@dataclass(frozen=True, slots=True)
class SetMatchScore:
    """Precision/recall/F1 over two string sets with exact counts."""

    name: str
    expected: frozenset[str]
    actual: frozenset[str]
    duplicate_expected: int = 0
    duplicate_actual: int = 0

    @classmethod
    def between(cls, name: str, expected: Iterable[str], actual: Iterable[str]) -> Self:
        """Build a score from iterables, collapsing duplicates deterministically."""
        expected_set, duplicate_expected = _dedupe(expected)
        actual_set, duplicate_actual = _dedupe(actual)
        return cls(
            name=name,
            expected=expected_set,
            actual=actual_set,
            duplicate_expected=duplicate_expected,
            duplicate_actual=duplicate_actual,
        )

    @property
    def true_positives(self) -> int:
        """Members present in both expected and actual."""
        return len(self.expected & self.actual)

    @property
    def false_positives(self) -> int:
        """Members present in actual but not expected."""
        return len(self.actual - self.expected)

    @property
    def false_negatives(self) -> int:
        """Members present in expected but not actual."""
        return len(self.expected - self.actual)

    @property
    def precision(self) -> float:
        """Fraction of actual positives that are expected (0 when undefined)."""
        true_positives = self.true_positives
        denominator = true_positives + self.false_positives
        if denominator == 0:
            return 1.0 if not self.expected else 0.0
        return true_positives / denominator

    @property
    def recall(self) -> float:
        """Fraction of expected positives that were found (1 when none)."""
        true_positives = self.true_positives
        denominator = true_positives + self.false_negatives
        if denominator == 0:
            return 1.0
        return true_positives / denominator

    @property
    def f1(self) -> float:
        """Harmonic mean of precision and recall (0 when either is 0)."""
        precision, recall = self.precision, self.recall
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    def to_metrics(self, evaluator_version: str) -> tuple[EvalMetric, ...]:
        """Return exact-count and normalized metrics for this score."""
        union = len(self.expected | self.actual)
        return (
            EvalMetric(
                name=f"{self.name}.true_positives",
                evaluator_version=evaluator_version,
                value=_normalize(self.true_positives, union),
                numerator=self.true_positives,
                denominator=union,
            ),
            EvalMetric(
                name=f"{self.name}.false_positives",
                evaluator_version=evaluator_version,
                value=_normalize(self.false_positives, len(self.actual)),
                numerator=self.false_positives,
                denominator=len(self.actual),
            ),
            EvalMetric(
                name=f"{self.name}.false_negatives",
                evaluator_version=evaluator_version,
                value=_normalize(self.false_negatives, len(self.expected)),
                numerator=self.false_negatives,
                denominator=len(self.expected),
            ),
            EvalMetric(
                name=f"{self.name}.precision",
                evaluator_version=evaluator_version,
                value=self.precision,
            ),
            EvalMetric(
                name=f"{self.name}.recall",
                evaluator_version=evaluator_version,
                value=self.recall,
            ),
            EvalMetric(
                name=f"{self.name}.f1",
                evaluator_version=evaluator_version,
                value=self.f1,
            ),
        )


@dataclass(frozen=True, slots=True)
class CategoricalScore:
    """Exact categorical comparison (disposition/label correctness)."""

    name: str
    expected: str
    actual: str

    @property
    def matches(self) -> bool:
        """Return whether the categorical value matches exactly."""
        return self.expected == self.actual

    def to_metrics(self, evaluator_version: str) -> tuple[EvalMetric, ...]:
        """Return the normalized correctness metric."""
        return (
            EvalMetric(
                name=f"{self.name}.correct",
                evaluator_version=evaluator_version,
                value=1.0 if self.matches else 0.0,
            ),
        )


@dataclass(frozen=True, slots=True)
class GroundingScore:
    """Evidence-grounding validity ratio over (already-classified) refs."""

    name: str
    valid: int
    invalid: int

    def __post_init__(self) -> None:
        if self.valid < 0 or self.invalid < 0:
            raise ValueError("grounding counts must be non-negative")

    @property
    def total(self) -> int:
        """Total classified references."""
        return self.valid + self.invalid

    @property
    def ratio(self) -> float:
        """Valid fraction; vacuously 1.0 when there are no references."""
        if self.total == 0:
            return 1.0
        return self.valid / self.total

    def to_metrics(self, evaluator_version: str) -> tuple[EvalMetric, ...]:
        """Return the grounding ratio and invalid-count metrics."""
        return (
            EvalMetric(
                name=f"{self.name}.grounding_ratio",
                evaluator_version=evaluator_version,
                value=self.ratio,
                numerator=self.valid,
                denominator=self.total,
            ),
            EvalMetric(
                name=f"{self.name}.invalid_references",
                evaluator_version=evaluator_version,
                value=_normalize(self.invalid, self.total),
                numerator=self.invalid,
                denominator=self.total,
            ),
        )


@dataclass(frozen=True, slots=True)
class RubricCoverage:
    """Finite rubric coverage: how many required items the output covers."""

    name: str
    expected: frozenset[str] = field(default_factory=frozenset)
    covered: frozenset[str] = field(default_factory=frozenset)
    unsupported: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def between(
        cls,
        name: str,
        expected: Iterable[str],
        covered: Iterable[str],
        *,
        unsupported: Iterable[str] = (),
    ) -> Self:
        """Build from required/covered/unsupported iterables."""
        return cls(
            name=name,
            expected=frozenset(expected),
            covered=frozenset(covered),
            unsupported=frozenset(unsupported),
        )

    @property
    def missing(self) -> frozenset[str]:
        """Required rubric items that were not covered."""
        return self.expected - self.covered

    @property
    def coverage(self) -> float:
        """Covered fraction of required items; vacuously 1.0 when none."""
        if not self.expected:
            return 1.0
        return len(self.expected & self.covered) / len(self.expected)

    def to_metrics(self, evaluator_version: str) -> tuple[EvalMetric, ...]:
        """Return coverage metric."""
        return (
            EvalMetric(
                name=f"{self.name}.coverage",
                evaluator_version=evaluator_version,
                value=self.coverage,
                numerator=len(self.expected & self.covered),
                denominator=len(self.expected),
            ),
        )
