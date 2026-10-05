# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic layer-evaluator tests (PR 15).

Covers matrices RE15, SE15, GE15, RL15, and SA15.
"""

from __future__ import annotations

import pytest

from darkula.evaluation.domain import EvalMetric
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
    SemanticOccurrence,
    SourceAnalystExpectation,
    SourceAnalystObservation,
    evaluate_geography,
    evaluate_recon,
    evaluate_relationship,
    evaluate_semantic,
    evaluate_source_analyst,
    validate_case_evaluator,
)


def _by_name(metrics: tuple[EvalMetric, ...]) -> dict[str, EvalMetric]:
    return {metric.name: metric for metric in metrics}


class TestReconEvaluator:
    """RE15-1..RE15-10."""

    def _expectation(self) -> ReconExpectation:
        return ReconExpectation(
            source_type="marketplace",
            useful_paths=frozenset({"/", "/listings"}),
            disposition="QUALIFY",
            allowed_evidence_refs=frozenset({"recon:ev-1"}),
            mirror_hosts=frozenset({"mirror.example.test"}),
            hidden_truth_tokens=("ghost_admin",),
        )

    def test_re15_1_correct_source_type(self) -> None:
        metrics = _by_name(
            evaluate_recon(
                self._expectation(),
                ReconObservation(source_type="marketplace"),
            )
        )
        assert metrics["recon.source_type.correct"].value == 1.0

    def test_re15_2_wrong_source_type(self) -> None:
        metrics = _by_name(
            evaluate_recon(self._expectation(), ReconObservation(source_type="forum"))
        )
        assert metrics["recon.source_type.correct"].value == 0.0

    def test_re15_3_useful_path_found(self) -> None:
        metrics = _by_name(
            evaluate_recon(
                self._expectation(),
                ReconObservation(source_type="marketplace", useful_paths=("/",)),
            )
        )
        assert metrics["recon.useful_paths.true_positives"].numerator == 1

    def test_re15_4_invented_path_false_positive(self) -> None:
        metrics = _by_name(
            evaluate_recon(
                self._expectation(),
                ReconObservation(
                    source_type="marketplace", useful_paths=("/", "/invented")
                ),
            )
        )
        assert metrics["recon.useful_paths.false_positives"].numerator == 1

    def test_re15_5_correct_disposition(self) -> None:
        metrics = _by_name(
            evaluate_recon(
                self._expectation(),
                ReconObservation(source_type="marketplace", disposition="QUALIFY"),
            )
        )
        assert metrics["recon.disposition.correct"].value == 1.0

    def test_re15_6_wrong_disposition(self) -> None:
        metrics = _by_name(
            evaluate_recon(
                self._expectation(),
                ReconObservation(source_type="marketplace", disposition="REJECT"),
            )
        )
        assert metrics["recon.disposition.correct"].value == 0.0

    def test_re15_7_hidden_truth_omitted_no_penalty(self) -> None:
        metrics = _by_name(
            evaluate_recon(
                self._expectation(),
                ReconObservation(
                    source_type="marketplace",
                    useful_paths=("/", "/listings"),
                    disposition="QUALIFY",
                    mirror_hosts=("mirror.example.test",),
                ),
            )
        )
        assert metrics["hidden_truth.leaked"].value == 0.0
        assert metrics["recon.useful_paths.f1"].value == 1.0

    def test_re15_8_hidden_truth_leaked_detected(self) -> None:
        metrics = _by_name(
            evaluate_recon(
                self._expectation(),
                ReconObservation(source_type="marketplace", disposition="ghost_admin"),
            )
        )
        assert metrics["hidden_truth.leaked"].value == 1.0

    def test_re15_9_grounded_evidence(self) -> None:
        metrics = _by_name(
            evaluate_recon(
                self._expectation(),
                ReconObservation(
                    source_type="marketplace", evidence_refs=("recon:ev-1",)
                ),
            )
        )
        assert metrics["recon.evidence.grounding_ratio"].value == 1.0

    def test_re15_10_invented_evidence_ref(self) -> None:
        metrics = _by_name(
            evaluate_recon(
                self._expectation(),
                ReconObservation(
                    source_type="marketplace", evidence_refs=("recon:bad",)
                ),
            )
        )
        assert metrics["recon.evidence.invalid_references"].numerator == 1

    def test_missing_source_type_expectation_fails(self) -> None:
        with pytest.raises(EvalEvaluatorError):
            evaluate_recon(ReconExpectation(source_type=""), ReconObservation("x"))


class TestSemanticEvaluator:
    """SE15-1..SE15-10."""

    def _expectation(self) -> SemanticExpectation:
        return SemanticExpectation(
            expected_occurrences=frozenset(
                {"ORGANIZATION|Mason Creek General Hospital", "LOCATION|Washington"}
            ),
            hidden_truth_tokens=("ghost_admin",),
        )

    def _observation(self, *items: SemanticOccurrence) -> SemanticObservation:
        return SemanticObservation(occurrences=items)

    def test_se15_1_exact_typed_occurrences(self) -> None:
        metrics = _by_name(
            evaluate_semantic(
                self._expectation(),
                self._observation(
                    SemanticOccurrence(
                        "ORGANIZATION", "Mason Creek General Hospital", 0, 28
                    ),
                    SemanticOccurrence("LOCATION", "Washington", 30, 40),
                ),
            )
        )
        assert metrics["semantic.occurrences.f1"].value == 1.0

    def test_se15_2_wrong_type_fp_and_fn(self) -> None:
        metrics = _by_name(
            evaluate_semantic(
                self._expectation(),
                self._observation(
                    SemanticOccurrence("LOCATION", "Mason Creek General Hospital", 0, 5)
                ),
            )
        )
        assert metrics["semantic.occurrences.false_positives"].numerator == 1
        assert metrics["semantic.occurrences.false_negatives"].numerator == 2

    def test_se15_3_unsupported_entity_fp(self) -> None:
        metrics = _by_name(
            evaluate_semantic(
                self._expectation(),
                self._observation(SemanticOccurrence("PERSON", "Nobody", 0, 5)),
            )
        )
        assert metrics["semantic.occurrences.false_positives"].numerator == 1

    def test_se15_4_missing_entity_fn(self) -> None:
        metrics = _by_name(evaluate_semantic(self._expectation(), self._observation()))
        assert metrics["semantic.occurrences.false_negatives"].numerator == 2

    def test_se15_5_repeated_occurrence_set_policy(self) -> None:
        metrics = _by_name(
            evaluate_semantic(
                self._expectation(),
                self._observation(
                    SemanticOccurrence("LOCATION", "Washington", 0, 5),
                    SemanticOccurrence("LOCATION", "Washington", 6, 11),
                ),
            )
        )
        assert metrics["semantic.occurrences.true_positives"].numerator == 1

    def test_se15_6_exact_span_valid(self) -> None:
        metrics = _by_name(
            evaluate_semantic(
                self._expectation(),
                self._observation(SemanticOccurrence("LOCATION", "Washington", 1, 5)),
            )
        )
        assert metrics["semantic.spans.grounding_ratio"].value == 1.0

    def test_se15_7_invalid_span_fails(self) -> None:
        metrics = _by_name(
            evaluate_semantic(
                self._expectation(),
                self._observation(SemanticOccurrence("LOCATION", "Washington", 5, 5)),
            )
        )
        assert metrics["semantic.spans.invalid_references"].numerator == 1

    def test_se15_8_confidence_not_present_in_key(self) -> None:
        # Correctness keys never include extraction confidence.
        metrics = _by_name(
            evaluate_semantic(
                self._expectation(),
                self._observation(
                    SemanticOccurrence("LOCATION", "Washington", 0, 5),
                ),
            )
        )
        assert metrics["semantic.occurrences.true_positives"].numerator == 1

    def test_se15_9_hidden_entity_omitted_no_penalty(self) -> None:
        metrics = _by_name(evaluate_semantic(self._expectation(), self._observation()))
        assert metrics["hidden_truth.leaked"].value == 0.0

    def test_se15_10_per_type_aggregation(self) -> None:
        metrics = _by_name(
            evaluate_semantic(
                self._expectation(),
                self._observation(
                    SemanticOccurrence(
                        "ORGANIZATION", "Mason Creek General Hospital", 0, 28
                    )
                ),
            )
        )
        assert metrics["semantic.occurrences.recall"].value == 0.5


class TestGeographyEvaluator:
    """GE15-1..GE15-6."""

    def _expectation(self) -> GeographyExpectation:
        return GeographyExpectation(
            expected=frozenset({"Mason Creek|RESOLVED", "Washington|AMBIGUOUS"}),
            hidden_truth_tokens=("ghost_admin",),
        )

    def test_ge15_1_resolved_correct(self) -> None:
        metrics = _by_name(
            evaluate_geography(
                self._expectation(),
                GeographyObservation(
                    resolutions=("Mason Creek|RESOLVED", "Washington|AMBIGUOUS"),
                    canonical_fields=("country=United States",),
                ),
            )
        )
        assert metrics["geography.resolutions.f1"].value == 1.0

    def test_ge15_2_resolved_wrong(self) -> None:
        metrics = _by_name(
            evaluate_geography(
                self._expectation(),
                GeographyObservation(resolutions=("Mason Creek|UNRESOLVED",)),
            )
        )
        assert metrics["geography.resolutions.false_positives"].numerator == 1

    def test_ge15_3_ambiguous(self) -> None:
        metrics = _by_name(
            evaluate_geography(
                self._expectation(),
                GeographyObservation(resolutions=("Washington|AMBIGUOUS",)),
            )
        )
        assert metrics["geography.resolutions.true_positives"].numerator == 1

    def test_ge15_4_unresolved(self) -> None:
        metrics = _by_name(
            evaluate_geography(
                GeographyExpectation(expected=frozenset({"Mystery|UNRESOLVED"})),
                GeographyObservation(resolutions=("Mystery|UNRESOLVED",)),
            )
        )
        assert metrics["geography.resolutions.f1"].value == 1.0

    def test_ge15_5_extraction_confidence_separate(self) -> None:
        # Confidence is not part of the resolution key or metrics.
        metrics = _by_name(
            evaluate_geography(
                self._expectation(),
                GeographyObservation(resolutions=("Washington|AMBIGUOUS",)),
            )
        )
        assert "confidence" not in " ".join(metrics)

    def test_ge15_6_no_live_provider_required(self) -> None:
        # The evaluator is pure and requires no resolver/provider.
        metrics = evaluate_geography(
            GeographyExpectation(expected=frozenset()), GeographyObservation()
        )
        assert metrics

    def test_hidden_truth_leak(self) -> None:
        metrics = _by_name(
            evaluate_geography(
                self._expectation(),
                GeographyObservation(resolutions=("ghost_admin|RESOLVED",)),
            )
        )
        assert metrics["hidden_truth.leaked"].value == 1.0


class TestRelationshipEvaluator:
    """RL15-1..RL15-8."""

    def _expectation(self) -> RelationshipExpectation:
        return RelationshipExpectation(
            expected=frozenset({"advertises_access|zfox|Mason Creek General Hospital"}),
            allowed_supports=frozenset({"support:1"}),
            hidden_truth_tokens=("ghost_admin",),
        )

    def test_rl15_1_exact_assertion(self) -> None:
        metrics = _by_name(
            evaluate_relationship(
                self._expectation(),
                RelationshipObservation(
                    assertions=("advertises_access|zfox|Mason Creek General Hospital",),
                    supports=("support:1",),
                ),
            )
        )
        assert metrics["relationship.assertions.f1"].value == 1.0

    def test_rl15_2_wrong_predicate(self) -> None:
        metrics = _by_name(
            evaluate_relationship(
                self._expectation(),
                RelationshipObservation(
                    assertions=("partners_with|zfox|Mason Creek General Hospital",)
                ),
            )
        )
        assert metrics["relationship.assertions.false_positives"].numerator == 1
        assert metrics["relationship.assertions.false_negatives"].numerator == 1

    def test_rl15_3_wrong_endpoint(self) -> None:
        metrics = _by_name(
            evaluate_relationship(
                self._expectation(),
                RelationshipObservation(
                    assertions=("advertises_access|zfox|Wrong Org",)
                ),
            )
        )
        assert metrics["relationship.assertions.false_positives"].numerator == 1

    def test_rl15_4_unsupported_assertion(self) -> None:
        metrics = _by_name(
            evaluate_relationship(
                self._expectation(),
                RelationshipObservation(
                    assertions=("advertises_access|zfox|Mason Creek General Hospital",)
                ),
            )
        )
        assert metrics["relationship.supports.invalid_references"].numerator == 0

    def test_rl15_5_missing(self) -> None:
        metrics = _by_name(
            evaluate_relationship(self._expectation(), RelationshipObservation())
        )
        assert metrics["relationship.assertions.false_negatives"].numerator == 1

    def test_rl15_6_invalid_support(self) -> None:
        metrics = _by_name(
            evaluate_relationship(
                self._expectation(),
                RelationshipObservation(supports=("support:bad",)),
            )
        )
        assert metrics["relationship.supports.invalid_references"].numerator == 1

    def test_rl15_7_hidden_relation_omitted_no_penalty(self) -> None:
        metrics = _by_name(
            evaluate_relationship(
                self._expectation(),
                RelationshipObservation(
                    assertions=("advertises_access|zfox|Mason Creek General Hospital",)
                ),
            )
        )
        assert metrics["hidden_truth.leaked"].value == 0.0

    def test_rl15_8_quoted_noise_precision(self) -> None:
        metrics = _by_name(
            evaluate_relationship(
                self._expectation(),
                RelationshipObservation(
                    assertions=(
                        "advertises_access|zfox|Mason Creek General Hospital",
                        "seized_by|broker_north|Mason Creek General Hospital",
                    )
                ),
            )
        )
        assert metrics["relationship.assertions.precision"].value == 0.5


class TestSourceAnalystEvaluator:
    """SA15-1..SA15-10."""

    def _expectation(self) -> SourceAnalystExpectation:
        return SourceAnalystExpectation(
            required_rubric_items=frozenset({"relevance", "activity", "evidence"}),
            allowed_evidence_refs=frozenset({"evidence:1", "evidence:2"}),
            forbidden_conclusions=frozenset({"hospital_seized"}),
            hidden_truth_tokens=("ghost_admin",),
        )

    def test_sa15_1_required_relevance(self) -> None:
        metrics = _by_name(
            evaluate_source_analyst(
                self._expectation(),
                SourceAnalystObservation(
                    covered_rubric_items=("relevance", "activity", "evidence")
                ),
            )
        )
        assert metrics["source_analysis.rubric.coverage"].value == 1.0

    def test_sa15_2_wrong_activity(self) -> None:
        metrics = _by_name(
            evaluate_source_analyst(
                self._expectation(),
                SourceAnalystObservation(covered_rubric_items=("relevance",)),
            )
        )
        assert metrics["source_analysis.rubric.coverage"].value == pytest.approx(1 / 3)

    def test_sa15_3_novelty_with_evidence(self) -> None:
        metrics = _by_name(
            evaluate_source_analyst(
                self._expectation(),
                SourceAnalystObservation(
                    covered_rubric_items=("relevance", "activity", "evidence"),
                    evidence_refs=("evidence:1",),
                ),
            )
        )
        assert metrics["source_analysis.evidence.grounding_ratio"].value == 1.0

    def test_sa15_4_unsupported_conclusion(self) -> None:
        metrics = _by_name(
            evaluate_source_analyst(
                self._expectation(),
                SourceAnalystObservation(
                    covered_rubric_items=("relevance",),
                    conclusions=("hospital_seized",),
                ),
            )
        )
        assert metrics["source_analysis.unsupported_conclusions"].value == 1.0

    def test_sa15_5_valid_evidence_refs(self) -> None:
        metrics = _by_name(
            evaluate_source_analyst(
                self._expectation(),
                SourceAnalystObservation(evidence_refs=("evidence:1", "evidence:2")),
            )
        )
        assert metrics["source_analysis.evidence.invalid_references"].numerator == 0

    def test_sa15_6_unknown_ref_failure(self) -> None:
        metrics = _by_name(
            evaluate_source_analyst(
                self._expectation(),
                SourceAnalystObservation(evidence_refs=("evidence:unknown",)),
            )
        )
        assert metrics["source_analysis.evidence.invalid_references"].numerator == 1

    def test_sa15_7_evidence_free_inactivity_forbidden(self) -> None:
        metrics = _by_name(
            evaluate_source_analyst(
                SourceAnalystExpectation(
                    required_rubric_items=frozenset({"evidence"}),
                    forbidden_conclusions=frozenset({"inactive"}),
                ),
                SourceAnalystObservation(conclusions=("inactive",)),
            )
        )
        assert metrics["source_analysis.unsupported_conclusions"].value == 1.0

    def test_sa15_8_hidden_truth_omission_no_penalty(self) -> None:
        metrics = _by_name(
            evaluate_source_analyst(self._expectation(), SourceAnalystObservation())
        )
        assert metrics["hidden_truth.leaked"].value == 0.0

    def test_sa15_9_source_local_truth_no_penalty(self) -> None:
        # A correct source-local analysis is not penalised for lacking
        # hidden cross-source truth.
        metrics = _by_name(
            evaluate_source_analyst(
                self._expectation(),
                SourceAnalystObservation(
                    covered_rubric_items=("relevance", "activity", "evidence")
                ),
            )
        )
        assert metrics["source_analysis.rubric.coverage"].value == 1.0

    def test_sa15_10_same_output_same_score(self) -> None:
        first = evaluate_source_analyst(self._expectation(), SourceAnalystObservation())
        second = evaluate_source_analyst(
            self._expectation(), SourceAnalystObservation()
        )
        assert [m.value for m in first] == [m.value for m in second]


def test_validate_case_evaluator() -> None:
    assert validate_case_evaluator("recon") == "recon"
    with pytest.raises(ValueError, match="unknown evaluator"):
        validate_case_evaluator("nope")
