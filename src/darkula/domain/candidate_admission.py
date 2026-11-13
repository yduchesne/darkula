# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic candidate admission vocabulary (PR 19).

Admission is the explicit, trusted post-ingest step that reconciles one
immutable :class:`~darkula.domain.discovery.DiscoveryObservation` against
already persisted state. It is deliberately conservative:

- a normalized locator is a *syntactic* lookup key, never a logical source
  identity, so equal URLs never merge Sources;
- a locator admission slot suppresses duplicate candidate creation for the
  same exact normalized locator only;
- an existing managed ``SourceEndpoint`` match is evidence, never a new
  Source, endpoint, or collection authorization;
- ambiguous or oversized match sets stay ``REVIEW_REQUIRED`` instead of
  picking an apparently definitive winner;
- a possible mirror/alias is a read-only claim, never a source merge.

Nothing here performs I/O, normalization, matching, policy evaluation, or
persistence. The finite vocabularies are persisted verbatim by migration
``0011_candidate_admission.sql`` and must stay in parity with its check
constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from darkula.domain.identifiers import (
    DiscoveryObservationId,
    SourceCandidateId,
    SourceEndpointId,
)

#: Maximum exact managed-endpoint matches admitted for one observation. A
#: larger exact match set is ambiguous and becomes ``REVIEW_REQUIRED``.
MAX_ADMISSION_ENDPOINT_MATCHES = 32

#: Maximum possible-alias suggestions returned for one candidate. A larger
#: set is an explicit ``too_many`` result rather than a truncated claim
#: list.
MAX_POSSIBLE_ALIASES = 16

#: The single bounded possible-alias rationale (never a semantic merge).
ALIAS_REASON_SHARED_PROVIDER_ORIGIN = "SHARED_PROVIDER_ORIGIN"


class AdmissionOutcome(StrEnum):
    """What one admission attempt durably decided."""

    UNRESOLVED_NAME = "UNRESOLVED_NAME"
    KNOWN_ENDPOINT = "KNOWN_ENDPOINT"
    ADMITTED_NEW = "ADMITTED_NEW"
    ADMITTED_EXISTING = "ADMITTED_EXISTING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class AdmissionReason(StrEnum):
    """The bounded, deterministic reason for an admission outcome."""

    NAME_ONLY = "NAME_ONLY"
    EXACT_MANAGED_ENDPOINT = "EXACT_MANAGED_ENDPOINT"
    NEW_LOCATOR = "NEW_LOCATOR"
    EXISTING_LOCATOR_SLOT = "EXISTING_LOCATOR_SLOT"
    EXISTING_LEGACY_CANDIDATE = "EXISTING_LEGACY_CANDIDATE"
    MULTIPLE_LEGACY_CANDIDATES = "MULTIPLE_LEGACY_CANDIDATES"
    EXCESS_ENDPOINT_MATCHES = "EXCESS_ENDPOINT_MATCHES"


#: The exhaustive outcome -> allowed-reason vocabulary. Any other pairing is
#: a programming error and is rejected before persistence.
_ALLOWED_REASONS: dict[AdmissionOutcome, frozenset[AdmissionReason]] = {
    AdmissionOutcome.UNRESOLVED_NAME: frozenset({AdmissionReason.NAME_ONLY}),
    AdmissionOutcome.KNOWN_ENDPOINT: frozenset(
        {AdmissionReason.EXACT_MANAGED_ENDPOINT}
    ),
    AdmissionOutcome.ADMITTED_NEW: frozenset({AdmissionReason.NEW_LOCATOR}),
    AdmissionOutcome.ADMITTED_EXISTING: frozenset(
        {
            AdmissionReason.EXISTING_LOCATOR_SLOT,
            AdmissionReason.EXISTING_LEGACY_CANDIDATE,
        }
    ),
    AdmissionOutcome.REVIEW_REQUIRED: frozenset(
        {
            AdmissionReason.MULTIPLE_LEGACY_CANDIDATES,
            AdmissionReason.EXCESS_ENDPOINT_MATCHES,
        }
    ),
}

_ADMITTED_OUTCOMES = frozenset(
    {AdmissionOutcome.ADMITTED_NEW, AdmissionOutcome.ADMITTED_EXISTING}
)


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """The immutable result of one admission attempt.

    Shape invariants mirror the persisted ledger:

    - ``ADMITTED_NEW``/``ADMITTED_EXISTING`` carry a candidate and no
      endpoint matches;
    - ``KNOWN_ENDPOINT`` carries at least one (bounded) endpoint match and
      no candidate;
    - ``UNRESOLVED_NAME``/``REVIEW_REQUIRED`` carry neither.

    ``replayed`` reports that the ledger already held this observation and
    the stored original result was returned without new durable effects.
    """

    observation_id: DiscoveryObservationId
    outcome: AdmissionOutcome
    reason: AdmissionReason
    candidate_id: SourceCandidateId | None
    matched_endpoint_ids: tuple[SourceEndpointId, ...]
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, DiscoveryObservationId):
            raise ValueError("observation_id must be a DiscoveryObservationId")
        if not isinstance(self.outcome, AdmissionOutcome):
            raise ValueError("outcome must be an AdmissionOutcome")
        if not isinstance(self.reason, AdmissionReason):
            raise ValueError("reason must be an AdmissionReason")
        if self.reason not in _ALLOWED_REASONS[self.outcome]:
            raise ValueError(
                f"reason {self.reason.value} is not valid for outcome "
                f"{self.outcome.value}"
            )
        if not isinstance(self.replayed, bool):
            raise ValueError("replayed must be a bool")
        endpoints = self.matched_endpoint_ids
        if not isinstance(endpoints, tuple):
            raise ValueError("matched_endpoint_ids must be a tuple")
        if len(endpoints) > MAX_ADMISSION_ENDPOINT_MATCHES:
            raise ValueError(
                "matched_endpoint_ids must not exceed "
                f"{MAX_ADMISSION_ENDPOINT_MATCHES} entries"
            )
        for endpoint_id in endpoints:
            if not isinstance(endpoint_id, SourceEndpointId):
                raise ValueError(
                    "matched_endpoint_ids must contain SourceEndpointId values"
                )
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("matched_endpoint_ids must be unique")
        if self.outcome in _ADMITTED_OUTCOMES:
            if self.candidate_id is None:
                raise ValueError("an admitted outcome requires a candidate_id")
            if not isinstance(self.candidate_id, SourceCandidateId):
                raise ValueError("candidate_id must be a SourceCandidateId")
            if endpoints:
                raise ValueError("an admitted outcome carries no endpoint matches")
        elif self.outcome is AdmissionOutcome.KNOWN_ENDPOINT:
            if self.candidate_id is not None:
                raise ValueError("KNOWN_ENDPOINT must not carry a candidate_id")
            if not endpoints:
                raise ValueError("KNOWN_ENDPOINT requires endpoint matches")
        else:
            if self.candidate_id is not None:
                raise ValueError(f"{self.outcome.value} must not carry a candidate_id")
            if endpoints:
                raise ValueError(
                    f"{self.outcome.value} must not carry endpoint matches"
                )


@dataclass(frozen=True, slots=True)
class PossibleAliasSuggestion:
    """One read-only possible mirror/alias claim.

    This is *evidence only*: it never merges Sources, rotates endpoints,
    admits one candidate as another, or implies trust. Both sides share the
    same provider and origin reference while keeping different normalized
    locators, so they remain distinct admission identities.
    """

    left_candidate_id: SourceCandidateId
    right_candidate_id: SourceCandidateId
    left_observation_id: DiscoveryObservationId
    right_observation_id: DiscoveryObservationId
    reason: str = ALIAS_REASON_SHARED_PROVIDER_ORIGIN

    def __post_init__(self) -> None:
        for field_name, value, expected in (
            ("left_candidate_id", self.left_candidate_id, SourceCandidateId),
            ("right_candidate_id", self.right_candidate_id, SourceCandidateId),
            ("left_observation_id", self.left_observation_id, DiscoveryObservationId),
            ("right_observation_id", self.right_observation_id, DiscoveryObservationId),
        ):
            if not isinstance(value, expected):
                raise ValueError(f"{field_name} must be a {expected.__name__}")
        if self.left_candidate_id == self.right_candidate_id:
            raise ValueError("a possible alias relates two distinct candidates")
        if self.left_observation_id == self.right_observation_id:
            raise ValueError("a possible alias relates two distinct observations")
        if self.reason != ALIAS_REASON_SHARED_PROVIDER_ORIGIN:
            raise ValueError("reason must be SHARED_PROVIDER_ORIGIN")


@dataclass(frozen=True, slots=True)
class AliasSuggestionsResult:
    """Bounded possible-alias suggestions with an explicit overflow flag."""

    items: tuple[PossibleAliasSuggestion, ...]
    too_many: bool

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise ValueError("items must be a tuple")
        if not isinstance(self.too_many, bool):
            raise ValueError("too_many must be a bool")
        if self.too_many:
            if self.items:
                raise ValueError(
                    "an overflow result carries no partial alias claim list"
                )
            return
        if len(self.items) > MAX_POSSIBLE_ALIASES:
            raise ValueError(
                f"items must not exceed {MAX_POSSIBLE_ALIASES} suggestions"
            )
        seen: set[tuple[SourceCandidateId, SourceCandidateId]] = set()
        for item in self.items:
            if not isinstance(item, PossibleAliasSuggestion):
                raise ValueError("items must contain PossibleAliasSuggestion values")
            key = (
                min(item.left_candidate_id, item.right_candidate_id, key=str),
                max(item.left_candidate_id, item.right_candidate_id, key=str),
            )
            if key in seen:
                raise ValueError("items must not repeat a candidate pair")
            seen.add(key)


__all__ = [
    "ALIAS_REASON_SHARED_PROVIDER_ORIGIN",
    "MAX_ADMISSION_ENDPOINT_MATCHES",
    "MAX_POSSIBLE_ALIASES",
    "AdmissionOutcome",
    "AdmissionReason",
    "AdmissionResult",
    "AliasSuggestionsResult",
    "PossibleAliasSuggestion",
]
