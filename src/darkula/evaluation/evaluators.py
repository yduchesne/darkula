# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic layer evaluators (PR 15).

Each evaluator is narrow and deterministic. Evaluators accept a bounded
production-output view plus a case expectation view and return typed
metrics. They never call an LLM, a crawler, the network, or mutate
production state; there is no generic "one evaluator to score them all".

Observable truth vs hidden truth is explicit: the expectation carries only
facts the evaluated agent could observe. Hidden truth is never required for
recall; when supplied, it is used solely to detect leakage (a safety
invariant).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from darkula.evaluation.domain import EvalMetric, EvalValidationError
from darkula.evaluation.metrics import (
    CategoricalScore,
    GroundingScore,
    RubricCoverage,
    SetMatchScore,
)

#: Semantic version of the deterministic evaluator implementations.
EVALUATOR_VERSION = "v1"

__all__ = [
    "EVALUATOR_VERSION",
    "EvalEvaluatorError",
    "GeographyExpectation",
    "GeographyObservation",
    "ReconExpectation",
    "ReconObservation",
    "RelationshipExpectation",
    "RelationshipObservation",
    "SemanticExpectation",
    "SemanticObservation",
    "SourceAnalystExpectation",
    "SourceAnalystObservation",
    "evaluate_geography",
    "evaluate_recon",
    "evaluate_relationship",
    "evaluate_semantic",
    "evaluate_source_analyst",
]


class EvalEvaluatorError(RuntimeError):
    """Typed evaluator bug/contract violation (mapped to EVALUATOR_ERROR)."""


def _leak_metric(
    *, hidden_tokens: tuple[str, ...], haystacks: tuple[str, ...]
) -> EvalMetric:
    leaked = any(
        token and token in text for token in hidden_tokens for text in haystacks
    )
    return EvalMetric(
        name="hidden_truth.leaked",
        evaluator_version=EVALUATOR_VERSION,
        value=1.0 if leaked else 0.0,
    )


def _combine(
    scorers: tuple[
        SetMatchScore | CategoricalScore | GroundingScore | RubricCoverage, ...
    ],
) -> tuple[EvalMetric, ...]:
    metrics: list[EvalMetric] = []
    for scorer in scorers:
        metrics.extend(scorer.to_metrics(EVALUATOR_VERSION))
    return tuple(metrics)


# ---------------------------------------------------------------------------
# Recon
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconExpectation:
    """Observable recon expectation for one case."""

    source_type: str
    useful_paths: frozenset[str] = field(default_factory=frozenset)
    disposition: str = ""
    allowed_evidence_refs: frozenset[str] = field(default_factory=frozenset)
    mirror_hosts: frozenset[str] = field(default_factory=frozenset)
    hidden_truth_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconObservation:
    """Bounded production-output view of a recon execution."""

    source_type: str
    useful_paths: tuple[str, ...] = ()
    disposition: str = ""
    evidence_refs: tuple[str, ...] = ()
    mirror_hosts: tuple[str, ...] = ()


def evaluate_recon(
    expectation: ReconExpectation, observation: ReconObservation
) -> tuple[EvalMetric, ...]:
    """Score a recon output against an observable expectation."""
    if not expectation.source_type:
        raise EvalEvaluatorError("recon expectation must declare a source type")
    valid_refs = sum(
        1
        for ref in observation.evidence_refs
        if ref in expectation.allowed_evidence_refs
    )
    invalid_refs = len(observation.evidence_refs) - valid_refs
    grounding = GroundingScore(
        name="recon.evidence", valid=valid_refs, invalid=invalid_refs
    )
    scorers = (
        CategoricalScore(
            name="recon.source_type",
            expected=expectation.source_type,
            actual=observation.source_type,
        ),
        SetMatchScore.between(
            "recon.useful_paths", expectation.useful_paths, observation.useful_paths
        ),
        CategoricalScore(
            name="recon.disposition",
            expected=expectation.disposition,
            actual=observation.disposition,
        ),
        SetMatchScore.between(
            "recon.mirrors", expectation.mirror_hosts, observation.mirror_hosts
        ),
        grounding,
    )
    hidden = _leak_metric(
        hidden_tokens=expectation.hidden_truth_tokens,
        haystacks=(
            observation.source_type,
            observation.disposition,
            *observation.useful_paths,
            *observation.mirror_hosts,
        ),
    )
    return (*_combine(scorers), hidden)


# ---------------------------------------------------------------------------
# Semantic extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SemanticExpectation:
    """Observable semantic-extraction expectation.

    ``expected_occurrences`` are ``"TYPE|normalized_value"`` strings at
    observable spans. Extraction confidence never determines correctness.
    """

    expected_occurrences: frozenset[str] = field(default_factory=frozenset)
    hidden_truth_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticOccurrence:
    """One production semantic occurrence (bounded view)."""

    entity_type: str
    normalized_value: str
    start: int
    end: int

    @property
    def key(self) -> str:
        """Stable typed occurrence key."""
        return f"{self.entity_type}|{self.normalized_value}"


@dataclass(frozen=True, slots=True)
class SemanticObservation:
    """Bounded production-output view of a semantic-extraction execution."""

    occurrences: tuple[SemanticOccurrence, ...] = ()


def evaluate_semantic(
    expectation: SemanticExpectation, observation: SemanticObservation
) -> tuple[EvalMetric, ...]:
    """Score typed occurrences and per-occurrence span validity."""
    invalid_span = sum(1 for item in observation.occurrences if item.end <= item.start)
    valid_span = len(observation.occurrences) - invalid_span
    scorers = (
        SetMatchScore.between(
            "semantic.occurrences",
            expectation.expected_occurrences,
            tuple(item.key for item in observation.occurrences),
        ),
        GroundingScore(name="semantic.spans", valid=valid_span, invalid=invalid_span),
    )
    hidden = _leak_metric(
        hidden_tokens=expectation.hidden_truth_tokens,
        haystacks=tuple(item.normalized_value for item in observation.occurrences),
    )
    return (*_combine(scorers), hidden)


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeographyExpectation:
    """Observable geographic-resolution expectation."""

    expected: frozenset[str] = field(default_factory=frozenset)
    hidden_truth_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GeographyObservation:
    """One bounded resolution observation: ``"mention|status"`` plus fields."""

    resolutions: tuple[str, ...] = ()
    canonical_fields: tuple[str, ...] = ()


def evaluate_geography(
    expectation: GeographyExpectation, observation: GeographyObservation
) -> tuple[EvalMetric, ...]:
    """Score expected resolution statuses and canonical field presence."""
    scorers = (
        SetMatchScore.between(
            "geography.resolutions", expectation.expected, observation.resolutions
        ),
        GroundingScore(
            name="geography.canonical_fields",
            valid=len(observation.canonical_fields),
            invalid=0,
        ),
    )
    hidden = _leak_metric(
        hidden_tokens=expectation.hidden_truth_tokens,
        haystacks=observation.resolutions + observation.canonical_fields,
    )
    return (*_combine(scorers), hidden)


# ---------------------------------------------------------------------------
# Relationship extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationshipExpectation:
    """Observable relationship-assertion expectation."""

    expected: frozenset[str] = field(default_factory=frozenset)
    allowed_supports: frozenset[str] = field(default_factory=frozenset)
    hidden_truth_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RelationshipObservation:
    """One bounded assertion: ``"predicate|source|target"`` plus support refs."""

    assertions: tuple[str, ...] = ()
    supports: tuple[str, ...] = ()


def evaluate_relationship(
    expectation: RelationshipExpectation, observation: RelationshipObservation
) -> tuple[EvalMetric, ...]:
    """Score observable relationship assertions and exact-support grounding."""
    valid_support = sum(
        1 for ref in observation.supports if ref in expectation.allowed_supports
    )
    invalid_support = len(observation.supports) - valid_support
    scorers = (
        SetMatchScore.between(
            "relationship.assertions", expectation.expected, observation.assertions
        ),
        GroundingScore(
            name="relationship.supports", valid=valid_support, invalid=invalid_support
        ),
    )
    hidden = _leak_metric(
        hidden_tokens=expectation.hidden_truth_tokens,
        haystacks=observation.assertions + observation.supports,
    )
    return (*_combine(scorers), hidden)


# ---------------------------------------------------------------------------
# Source analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceAnalystExpectation:
    """Explicit finite rubric expectation (never substring heuristics)."""

    required_rubric_items: frozenset[str] = field(default_factory=frozenset)
    allowed_evidence_refs: frozenset[str] = field(default_factory=frozenset)
    forbidden_conclusions: frozenset[str] = field(default_factory=frozenset)
    hidden_truth_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceAnalystObservation:
    """Bounded production-output view of a source-analysis execution."""

    covered_rubric_items: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    conclusions: tuple[str, ...] = ()


def evaluate_source_analyst(
    expectation: SourceAnalystExpectation,
    observation: SourceAnalystObservation,
) -> tuple[EvalMetric, ...]:
    """Score finite rubric coverage, evidence grounding, and unsupported claims."""
    valid_refs = sum(
        1
        for ref in observation.evidence_refs
        if ref in expectation.allowed_evidence_refs
    )
    invalid_refs = len(observation.evidence_refs) - valid_refs
    unsupported = frozenset(observation.conclusions) & expectation.forbidden_conclusions
    coverage = RubricCoverage.between(
        "source_analysis.rubric",
        expectation.required_rubric_items,
        observation.covered_rubric_items,
        unsupported=unsupported,
    )
    scorers = (
        coverage,
        GroundingScore(
            name="source_analysis.evidence", valid=valid_refs, invalid=invalid_refs
        ),
    )
    hidden = _leak_metric(
        hidden_tokens=expectation.hidden_truth_tokens,
        haystacks=observation.conclusions + observation.evidence_refs,
    )
    unsupported_metric = EvalMetric(
        name="source_analysis.unsupported_conclusions",
        evaluator_version=EVALUATOR_VERSION,
        value=0.0 if not unsupported else 1.0,
        numerator=len(unsupported),
        denominator=max(1, len(expectation.forbidden_conclusions)),
    )
    return (*_combine(scorers), unsupported_metric, hidden)


def validate_case_evaluator(case_evaluator: str) -> str:
    """Fail closed on an unknown evaluator name."""
    if case_evaluator not in {
        "recon",
        "semantic",
        "geography",
        "relationship",
        "source_analysis",
    }:
        raise EvalValidationError(f"unknown evaluator: {case_evaluator!r}")
    return case_evaluator
