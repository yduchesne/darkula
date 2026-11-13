# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic domain tests for PR 19 candidate admission vocabulary.

Pure value-object validation only: no database, network, or model.
"""

from __future__ import annotations

import pytest

from darkula.domain.candidate_admission import (
    ALIAS_REASON_SHARED_PROVIDER_ORIGIN,
    MAX_ADMISSION_ENDPOINT_MATCHES,
    MAX_POSSIBLE_ALIASES,
    AdmissionOutcome,
    AdmissionReason,
    AdmissionResult,
    AliasSuggestionsResult,
    PossibleAliasSuggestion,
)
from darkula.domain.identifiers import (
    DiscoveryObservationId,
    SourceCandidateId,
    SourceEndpointId,
)

CANDIDATE = SourceCandidateId.generate()
OTHER_CANDIDATE = SourceCandidateId.generate()
OBSERVATION = DiscoveryObservationId.generate()
OTHER_OBSERVATION = DiscoveryObservationId.generate()
ENDPOINTS = tuple(SourceEndpointId.generate() for _ in range(3))


class TestAdmissionResult:
    def test_admitted_new_requires_candidate_and_no_endpoints(self) -> None:
        result = AdmissionResult(
            observation_id=OBSERVATION,
            outcome=AdmissionOutcome.ADMITTED_NEW,
            reason=AdmissionReason.NEW_LOCATOR,
            candidate_id=CANDIDATE,
            matched_endpoint_ids=(),
        )
        assert result.candidate_id == CANDIDATE
        assert result.replayed is False

    def test_admitted_existing_locator_slot(self) -> None:
        result = AdmissionResult(
            observation_id=OBSERVATION,
            outcome=AdmissionOutcome.ADMITTED_EXISTING,
            reason=AdmissionReason.EXISTING_LOCATOR_SLOT,
            candidate_id=CANDIDATE,
            matched_endpoint_ids=(),
        )
        assert result.outcome is AdmissionOutcome.ADMITTED_EXISTING

    def test_admitted_existing_legacy_candidate(self) -> None:
        result = AdmissionResult(
            observation_id=OBSERVATION,
            outcome=AdmissionOutcome.ADMITTED_EXISTING,
            reason=AdmissionReason.EXISTING_LEGACY_CANDIDATE,
            candidate_id=CANDIDATE,
            matched_endpoint_ids=(),
        )
        assert result.reason is AdmissionReason.EXISTING_LEGACY_CANDIDATE

    def test_known_endpoint_requires_endpoints_and_no_candidate(self) -> None:
        result = AdmissionResult(
            observation_id=OBSERVATION,
            outcome=AdmissionOutcome.KNOWN_ENDPOINT,
            reason=AdmissionReason.EXACT_MANAGED_ENDPOINT,
            candidate_id=None,
            matched_endpoint_ids=ENDPOINTS,
        )
        assert result.matched_endpoint_ids == ENDPOINTS

    @pytest.mark.parametrize(
        "outcome, reason",
        [
            (AdmissionOutcome.UNRESOLVED_NAME, AdmissionReason.NAME_ONLY),
            (AdmissionOutcome.REVIEW_REQUIRED, AdmissionReason.EXCESS_ENDPOINT_MATCHES),
            (
                AdmissionOutcome.REVIEW_REQUIRED,
                AdmissionReason.MULTIPLE_LEGACY_CANDIDATES,
            ),
        ],
    )
    def test_absence_outcomes_carry_neither(
        self, outcome: AdmissionOutcome, reason: AdmissionReason
    ) -> None:
        result = AdmissionResult(
            observation_id=OBSERVATION,
            outcome=outcome,
            reason=reason,
            candidate_id=None,
            matched_endpoint_ids=(),
        )
        assert result.candidate_id is None
        assert result.matched_endpoint_ids == ()

    def test_admitted_outcome_without_candidate_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            AdmissionResult(
                observation_id=OBSERVATION,
                outcome=AdmissionOutcome.ADMITTED_NEW,
                reason=AdmissionReason.NEW_LOCATOR,
                candidate_id=None,
                matched_endpoint_ids=(),
            )

    def test_admitted_outcome_with_endpoints_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            AdmissionResult(
                observation_id=OBSERVATION,
                outcome=AdmissionOutcome.ADMITTED_EXISTING,
                reason=AdmissionReason.EXISTING_LOCATOR_SLOT,
                candidate_id=CANDIDATE,
                matched_endpoint_ids=ENDPOINTS,
            )

    def test_known_endpoint_without_endpoints_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            AdmissionResult(
                observation_id=OBSERVATION,
                outcome=AdmissionOutcome.KNOWN_ENDPOINT,
                reason=AdmissionReason.EXACT_MANAGED_ENDPOINT,
                candidate_id=None,
                matched_endpoint_ids=(),
            )

    def test_known_endpoint_with_candidate_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            AdmissionResult(
                observation_id=OBSERVATION,
                outcome=AdmissionOutcome.KNOWN_ENDPOINT,
                reason=AdmissionReason.EXACT_MANAGED_ENDPOINT,
                candidate_id=CANDIDATE,
                matched_endpoint_ids=ENDPOINTS,
            )

    def test_reason_must_match_outcome(self) -> None:
        with pytest.raises(ValueError):
            AdmissionResult(
                observation_id=OBSERVATION,
                outcome=AdmissionOutcome.ADMITTED_NEW,
                reason=AdmissionReason.EXISTING_LOCATOR_SLOT,
                candidate_id=CANDIDATE,
                matched_endpoint_ids=(),
            )

    def test_endpoint_tuple_is_bounded(self) -> None:
        too_many = tuple(
            SourceEndpointId.generate()
            for _ in range(MAX_ADMISSION_ENDPOINT_MATCHES + 1)
        )
        with pytest.raises(ValueError):
            AdmissionResult(
                observation_id=OBSERVATION,
                outcome=AdmissionOutcome.KNOWN_ENDPOINT,
                reason=AdmissionReason.EXACT_MANAGED_ENDPOINT,
                candidate_id=None,
                matched_endpoint_ids=too_many,
            )

    def test_endpoint_tuple_must_be_unique(self) -> None:
        with pytest.raises(ValueError):
            AdmissionResult(
                observation_id=OBSERVATION,
                outcome=AdmissionOutcome.KNOWN_ENDPOINT,
                reason=AdmissionReason.EXACT_MANAGED_ENDPOINT,
                candidate_id=None,
                matched_endpoint_ids=(ENDPOINTS[0], ENDPOINTS[0]),
            )

    def test_observation_identity_type_is_enforced(self) -> None:
        with pytest.raises(ValueError):
            AdmissionResult(
                observation_id="not-an-id",  # type: ignore[arg-type]
                outcome=AdmissionOutcome.UNRESOLVED_NAME,
                reason=AdmissionReason.NAME_ONLY,
                candidate_id=None,
                matched_endpoint_ids=(),
            )


class TestPossibleAliasSuggestion:
    def test_valid_suggestion(self) -> None:
        suggestion = PossibleAliasSuggestion(
            left_candidate_id=CANDIDATE,
            right_candidate_id=OTHER_CANDIDATE,
            left_observation_id=OBSERVATION,
            right_observation_id=OTHER_OBSERVATION,
        )
        assert suggestion.reason == ALIAS_REASON_SHARED_PROVIDER_ORIGIN

    def test_same_candidate_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            PossibleAliasSuggestion(
                left_candidate_id=CANDIDATE,
                right_candidate_id=CANDIDATE,
                left_observation_id=OBSERVATION,
                right_observation_id=OTHER_OBSERVATION,
            )

    def test_same_observation_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            PossibleAliasSuggestion(
                left_candidate_id=CANDIDATE,
                right_candidate_id=OTHER_CANDIDATE,
                left_observation_id=OBSERVATION,
                right_observation_id=OBSERVATION,
            )

    def test_arbitrary_reason_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            PossibleAliasSuggestion(
                left_candidate_id=CANDIDATE,
                right_candidate_id=OTHER_CANDIDATE,
                left_observation_id=OBSERVATION,
                right_observation_id=OTHER_OBSERVATION,
                reason="SAME_THING",
            )


class TestAliasSuggestionsResult:
    def _suggestion(self, index: int) -> PossibleAliasSuggestion:
        return PossibleAliasSuggestion(
            left_candidate_id=CANDIDATE,
            right_candidate_id=SourceCandidateId.generate(),
            left_observation_id=OBSERVATION,
            right_observation_id=_observation_id(index),
        )

    def test_overflow_carries_no_partial_list(self) -> None:
        result = AliasSuggestionsResult(items=(), too_many=True)
        assert result.items == ()
        assert result.too_many is True

    def test_overflow_with_items_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            AliasSuggestionsResult(items=(self._suggestion(0),), too_many=True)

    def test_items_are_bounded(self) -> None:
        items = tuple(self._suggestion(index) for index in range(MAX_POSSIBLE_ALIASES))
        result = AliasSuggestionsResult(items=items, too_many=False)
        assert len(result.items) == MAX_POSSIBLE_ALIASES
        with pytest.raises(ValueError):
            AliasSuggestionsResult(items=(*items, self._suggestion(99)), too_many=False)


def _observation_id(index: int) -> DiscoveryObservationId:
    """Return a distinct observation identity for alias-pair fixtures."""
    return DiscoveryObservationId.from_str(f"00000000-0000-4000-8000-{index:012x}")
