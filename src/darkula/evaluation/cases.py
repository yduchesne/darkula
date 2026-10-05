# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Stable, explicit evaluation case registry (PR 15).

Cases are registered explicitly by ``(case_id, version)`` — no filesystem
discovery and no ambient ordering. Every case declares its scenario/version,
layer, production operation, evaluator name/version, tags, and whether it
requires a live model or a real browser.

Semantic changes to a case's expectation require a version increment; never
mutate a frozen case in place.
"""

from __future__ import annotations

from dataclasses import dataclass

from darkula.evaluation.domain import EvalCase, EvalCaseId, EvalLayer
from darkula.evaluation.evaluators import (
    GeographyExpectation,
    ReconExpectation,
    RelationshipExpectation,
    SemanticExpectation,
    SourceAnalystExpectation,
)

__all__ = [
    "EvalCaseNotFoundError",
    "EvalCasePlan",
    "get_case_plan",
    "get_eval_case",
    "list_case_plans",
    "list_eval_cases",
]

CROSS_SOURCE = "darkula-cross-source"
BLACKGATE = "blackgate-core"

_RECON_VERSION = "v1"
_SEMANTIC_VERSION = "v1"
_GEOGRAPHY_VERSION = "v1"
_RELATIONSHIP_VERSION = "v1"
_SOURCE_ANALYSIS_VERSION = "v1"


class EvalCaseNotFoundError(KeyError):
    """Deterministic failure for an unknown evaluation case/version."""


@dataclass(frozen=True, slots=True)
class EvalCasePlan:
    """One case plus its expectation views.

    ``expectation`` is the observable-only expectation. Hidden truth is
    represented only through the explicit ``hidden_truth_tokens`` field used
    for leakage detection, never for recall.
    """

    case: EvalCase
    expectation: (
        ReconExpectation
        | SemanticExpectation
        | GeographyExpectation
        | RelationshipExpectation
        | SourceAnalystExpectation
    )


def _recon_cases() -> tuple[EvalCasePlan, ...]:
    return (
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-RECON-001"),
                version=1,
                layer=EvalLayer.RECON,
                scenario_id=BLACKGATE,
                scenario_version=1,
                description="BlackGate baseline recon (public entrypoint/board index).",
                evaluator_name="recon",
                evaluator_version=_RECON_VERSION,
                operation_name="recon.assess",
                tags=("baseline", "forum"),
            ),
            expectation=ReconExpectation(
                source_type="forum",
                useful_paths=frozenset({"/", "/index", "/board/board-announcements"}),
                disposition="QUALIFY",
                allowed_evidence_refs=frozenset({"recon:ev-1", "recon:ev-2"}),
                mirror_hosts=frozenset(),
                hidden_truth_tokens=("actor-001", "zfox", "Edgewater"),
            ),
        ),
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-RECON-002"),
                version=1,
                layer=EvalLayer.RECON,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description="AccessBay gated marketplace recon.",
                evaluator_name="recon",
                evaluator_version=_RECON_VERSION,
                operation_name="recon.assess",
                tags=("marketplace", "gating"),
            ),
            expectation=ReconExpectation(
                source_type="marketplace",
                useful_paths=frozenset({"/", "/listings", "/listing/listing-cred-1"}),
                disposition="QUALIFY",
                allowed_evidence_refs=frozenset({"recon:ab-1", "recon:ab-2"}),
                mirror_hosts=frozenset({"accessbay-mirror.example.test"}),
                hidden_truth_tokens=("actor-001", "zfox_zero", "Edgewater"),
            ),
        ),
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-RECON-003"),
                version=1,
                layer=EvalLayer.RECON,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description="NightLeak leak-site recon including a mirror reference.",
                evaluator_name="recon",
                evaluator_version=_RECON_VERSION,
                operation_name="recon.assess",
                tags=("leak", "mirror"),
            ),
            expectation=ReconExpectation(
                source_type="leak",
                useful_paths=frozenset({"/", "/leaks", "/leak/leak-001"}),
                disposition="QUALIFY",
                allowed_evidence_refs=frozenset({"recon:nl-1", "recon:nl-2"}),
                mirror_hosts=frozenset({"nightleak-mirror.example.test"}),
                hidden_truth_tokens=("actor-001", "ghost_admin", "event-004"),
            ),
        ),
    )


def _semantic_cases() -> tuple[EvalCasePlan, ...]:
    return (
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-SEM-001"),
                version=1,
                layer=EvalLayer.SEMANTIC_EXTRACTION,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description="AccessBay access listing: organization and location.",
                evaluator_name="semantic",
                evaluator_version=_SEMANTIC_VERSION,
                operation_name="semantic.extract",
                tags=("marketplace", "organization"),
            ),
            expectation=SemanticExpectation(
                expected_occurrences=frozenset(
                    {
                        "ORGANIZATION|Mason Creek General Hospital",
                        "LOCATION|Washington",
                    }
                ),
                hidden_truth_tokens=("ghost_admin", "actor-001"),
            ),
        ),
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-SEM-002"),
                version=1,
                layer=EvalLayer.SEMANTIC_EXTRACTION,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description="NightLeak publication: organization/location mentions.",
                evaluator_name="semantic",
                evaluator_version=_SEMANTIC_VERSION,
                operation_name="semantic.extract",
                tags=("leak", "organization"),
            ),
            expectation=SemanticExpectation(
                expected_occurrences=frozenset(
                    {
                        "ORGANIZATION|Mason Creek General Hospital",
                        "LOCATION|Washington",
                    }
                ),
                hidden_truth_tokens=("ghost_admin", "event-004"),
            ),
        ),
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-SEM-003"),
                version=1,
                layer=EvalLayer.SEMANTIC_EXTRACTION,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description="Noisy ShadowTalk text: unsupported claim is not truth.",
                evaluator_name="semantic",
                evaluator_version=_SEMANTIC_VERSION,
                operation_name="semantic.extract",
                tags=("noise", "fp-trap"),
            ),
            expectation=SemanticExpectation(
                expected_occurrences=frozenset(
                    {"ORGANIZATION|Mason Creek General Hospital"}
                ),
                hidden_truth_tokens=("ghost_admin", "event-004"),
            ),
        ),
    )


def _geography_cases() -> tuple[EvalCasePlan, ...]:
    return (
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-GEO-001"),
                version=1,
                layer=EvalLayer.GEOGRAPHY,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description=(
                    "Geographic resolution within the current resolver "
                    "architecture (no live geocoder)."
                ),
                evaluator_name="geography",
                evaluator_version=_GEOGRAPHY_VERSION,
                operation_name="geography.resolve",
                tags=("geography", "fake-resolver"),
            ),
            expectation=GeographyExpectation(
                expected=frozenset(
                    {
                        "Mason Creek, Washington|RESOLVED",
                        "Washington|AMBIGUOUS",
                    }
                ),
                hidden_truth_tokens=("ghost_admin", "event-004"),
            ),
        ),
    )


def _relationship_cases() -> tuple[EvalCasePlan, ...]:
    return (
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-REL-001"),
                version=1,
                layer=EvalLayer.RELATIONSHIP_EXTRACTION,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description="Actor/organization/access relationship assertion.",
                evaluator_name="relationship",
                evaluator_version=_RELATIONSHIP_VERSION,
                operation_name="relationship.extract",
                tags=("relationship", "actor"),
            ),
            expectation=RelationshipExpectation(
                expected=frozenset(
                    {
                        "advertises_access|zfox|Mason Creek General Hospital",
                        "targets_organization|Office 365 global admin|"
                        "Mason Creek General Hospital",
                    }
                ),
                allowed_supports=frozenset({"support:ab-1", "support:bg-1"}),
                hidden_truth_tokens=("ghost_admin", "event-004"),
            ),
        ),
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-REL-002"),
                version=1,
                layer=EvalLayer.RELATIONSHIP_EXTRACTION,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description=(
                    "Noisy/quoted/reposted assertion: quoted claim is not truth."
                ),
                evaluator_name="relationship",
                evaluator_version=_RELATIONSHIP_VERSION,
                operation_name="relationship.extract",
                tags=("noise", "quoted"),
            ),
            expectation=RelationshipExpectation(
                expected=frozenset(
                    {"corroborates|debunk_me|zfox_zero"},
                ),
                allowed_supports=frozenset({"support:st-1"}),
                hidden_truth_tokens=("ghost_admin", "event-004"),
            ),
        ),
    )


def _source_analysis_cases() -> tuple[EvalCasePlan, ...]:
    return (
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-SA-001"),
                version=1,
                layer=EvalLayer.SOURCE_ANALYSIS,
                scenario_id=BLACKGATE,
                scenario_version=1,
                description="BlackGate baseline source analysis.",
                evaluator_name="source_analysis",
                evaluator_version=_SOURCE_ANALYSIS_VERSION,
                operation_name="source_analysis.analyze",
                tags=("baseline", "forum"),
            ),
            expectation=SourceAnalystExpectation(
                required_rubric_items=frozenset({"relevance", "activity", "evidence"}),
                allowed_evidence_refs=frozenset({"evidence:bg-1", "evidence:bg-2"}),
                forbidden_conclusions=frozenset({"hospital_seized"}),
                hidden_truth_tokens=("actor-001", "ghost_admin", "event-004"),
            ),
        ),
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-SA-002"),
                version=1,
                layer=EvalLayer.SOURCE_ANALYSIS,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description="AccessBay activity/relevance source analysis.",
                evaluator_name="source_analysis",
                evaluator_version=_SOURCE_ANALYSIS_VERSION,
                operation_name="source_analysis.analyze",
                tags=("marketplace", "activity"),
            ),
            expectation=SourceAnalystExpectation(
                required_rubric_items=frozenset({"relevance", "activity", "novelty"}),
                allowed_evidence_refs=frozenset({"evidence:ab-1", "evidence:ab-2"}),
                forbidden_conclusions=frozenset({"hospital_seized"}),
                hidden_truth_tokens=("actor-001", "ghost_admin", "event-004"),
            ),
        ),
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-SA-003"),
                version=1,
                layer=EvalLayer.SOURCE_ANALYSIS,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description="NightLeak temporal novelty source analysis.",
                evaluator_name="source_analysis",
                evaluator_version=_SOURCE_ANALYSIS_VERSION,
                operation_name="source_analysis.analyze",
                tags=("leak", "novelty"),
            ),
            expectation=SourceAnalystExpectation(
                required_rubric_items=frozenset({"relevance", "novelty", "evidence"}),
                allowed_evidence_refs=frozenset({"evidence:nl-1", "evidence:nl-2"}),
                forbidden_conclusions=frozenset({"hospital_seized"}),
                hidden_truth_tokens=("actor-001", "ghost_admin", "event-004"),
            ),
        ),
        EvalCasePlan(
            case=EvalCase(
                case_id=EvalCaseId("EV-SA-004"),
                version=1,
                layer=EvalLayer.SOURCE_ANALYSIS,
                scenario_id=CROSS_SOURCE,
                scenario_version=1,
                description=(
                    "ShadowTalk noise: unsupported seizure claim must be rejected."
                ),
                evaluator_name="source_analysis",
                evaluator_version=_SOURCE_ANALYSIS_VERSION,
                operation_name="source_analysis.analyze",
                tags=("noise", "unsupported"),
            ),
            expectation=SourceAnalystExpectation(
                required_rubric_items=frozenset({"relevance", "evidence"}),
                allowed_evidence_refs=frozenset({"evidence:st-1"}),
                forbidden_conclusions=frozenset({"hospital_seized"}),
                hidden_truth_tokens=("actor-001", "ghost_admin", "event-004"),
            ),
        ),
    )


_CASE_PLANS: tuple[EvalCasePlan, ...] = (
    *_recon_cases(),
    *_semantic_cases(),
    *_geography_cases(),
    *_relationship_cases(),
    *_source_analysis_cases(),
)

_PLANS_BY_KEY: dict[tuple[str, int], EvalCasePlan] = {}
for _plan in _CASE_PLANS:
    _key = _plan.case.key
    if _key in _PLANS_BY_KEY:
        raise RuntimeError(f"duplicate evaluation case key: {_key!r}")
    _PLANS_BY_KEY[_key] = _plan


def get_eval_case(case_id: str | EvalCaseId, *, version: int = 1) -> EvalCase:
    """Return the case with the given ID/version or fail closed."""
    return get_case_plan(case_id, version=version).case


def get_case_plan(case_id: str | EvalCaseId, *, version: int = 1) -> EvalCasePlan:
    """Return the case plan with the given ID/version or fail closed."""
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise EvalCaseNotFoundError(
            "evaluation case version must be a positive integer"
        )
    key = (str(case_id), version)
    try:
        return _PLANS_BY_KEY[key]
    except KeyError:
        raise EvalCaseNotFoundError(
            f"unknown evaluation case {key!r}; registered cases are "
            f"{sorted(_PLANS_BY_KEY)}"
        ) from None


def list_eval_cases() -> tuple[EvalCase, ...]:
    """Return all registered cases in deterministic registry order."""
    return tuple(plan.case for plan in _CASE_PLANS)


def list_case_plans() -> tuple[EvalCasePlan, ...]:
    """Return all registered case plans in deterministic registry order."""
    return _CASE_PLANS
