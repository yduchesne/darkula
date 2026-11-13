# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted, versioned candidate qualification routing (PR 19).

Qualification is a deterministic policy decision, never an agentic one:

- the registry-policy path is fixed developer-controlled code
  (``candidate-qualification``/``v1``, rule ``RANSOMLOOK_LOCATION_V1``) and is
  **disabled by default**; registry reputation alone is never authorization
  to qualify, crawl, promote, or collect;
- an eligible, complete, uncontested RansomLook location record creates a
  real ``REGISTRY_POLICY`` decision through the existing PR 17 atomic
  primitive (never a fabricated ``ReconAssessment`` and never any LLM or
  crawler call);
- everything else routes to ``RECONNAISSANCE_PENDING`` with at most one
  existing ``NEEDS_MORE_RECON`` event. Pending routing is a *route*, not a
  fake decision;
- agent reconnaissance is dispatched only through an explicit trusted call
  (:meth:`CandidateQualificationService.dispatch_recon`) and always uses the
  existing :class:`~darkula.app.recon.ReconCoordinator`.

Disabling the operator flag prevents *new* registry-policy decisions; it
never deletes or reverses historical decisions or previously qualified
candidates. A policy change needs a new version; ``v1`` is never mutated.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from darkula.app.persistence import (
    AdmissionInvariantError,
    ConflictError,
    DarkulaSpi,
    NotFoundError,
    UnitOfWork,
)
from darkula.app.recon import (
    ReconCoordinator,
    ReconQualificationContext,
    ReconWorkflowResult,
)
from darkula.config.settings import QualificationSettings
from darkula.domain.candidate_admission import (
    AdmissionOutcome,
    AdmissionResult,
)
from darkula.domain.discovery import (
    MAX_EVIDENCE_OBSERVATIONS,
    DiscoveryDetectionMethod,
    DiscoveryObservation,
    DiscoveryReferenceKind,
    DiscoveryValidationStatus,
    QualificationDecision,
    QualificationMethod,
    QualificationOutcome,
    qualification_decision_key,
    qualification_event_type,
    qualification_new_status,
)
from darkula.domain.identifiers import (
    CandidateEventId,
    DiscoveryObservationId,
    QualificationDecisionId,
    SourceCandidateId,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    EndpointType,
    SourceCandidate,
    SourceCandidateEventHistory,
)

#: Frozen, developer-controlled policy identity. A semantic change requires
#: a new version; ``v1`` is never redefined.
QUALIFICATION_POLICY_ID = "candidate-qualification"
QUALIFICATION_POLICY_VERSION = "v1"
QUALIFICATION_RULE_ID = "RANSOMLOOK_LOCATION_V1"
RANSOMLOOK_PROVIDER_ID = "ransomlook"
RANSOMLOOK_RECORD_PREFIX = "location:"
#: Fixed public RansomLook group origin prefix used by the PR 18 adapter.
RANSOMLOOK_PUBLIC_ORIGIN = "https://www.ransomlook.io"
RANSOMLOOK_DETAIL_PREFIX = "/api/group/"
RANSOMLOOK_GROUP_ORIGIN_PREFIX = RANSOMLOOK_PUBLIC_ORIGIN + RANSOMLOOK_DETAIL_PREFIX

_RECORD_DIGEST = re.compile(r"^[0-9a-f]{64}$")

_TERMINAL_OR_BUSY = frozenset(
    {
        CandidateStatus.UNDER_RECONNAISSANCE,
        CandidateStatus.QUALIFIED,
        CandidateStatus.REJECTED,
        CandidateStatus.PROMOTED,
    }
)
_ELIGIBLE_STATUSES = frozenset(
    {CandidateStatus.DISCOVERED, CandidateStatus.RECONNAISSANCE_PENDING}
)


class QualificationRoute(StrEnum):
    """Finite, nonpersistent routing outcomes (never a fake decision)."""

    REGISTRY_QUALIFIED = "REGISTRY_QUALIFIED"
    PENDING_RECON_AUTH_REQUIRED = "PENDING_RECON_AUTH_REQUIRED"
    PENDING_RECON_POLICY_NOT_ELIGIBLE = "PENDING_RECON_POLICY_NOT_ELIGIBLE"
    PENDING_RECON_AMBIGUOUS = "PENDING_RECON_AMBIGUOUS"
    PENDING_RECON_UNAUTHORIZED = "PENDING_RECON_UNAUTHORIZED"
    ALREADY_TERMINAL_OR_BUSY = "ALREADY_TERMINAL_OR_BUSY"
    UNRESOLVED_OR_MANAGED = "UNRESOLVED_OR_MANAGED"
    CHECKPOINT_OR_STATE_RACE = "CHECKPOINT_OR_STATE_RACE"


class QualificationRuleCode(StrEnum):
    """Finite, bounded rule/result codes (safe to log)."""

    RANSOMLOOK_LOCATION_V1 = "RANSOMLOOK_LOCATION_V1"
    TRUST_DISABLED = "TRUST_DISABLED"
    AUTH_CLAIM_PRESENT = "AUTH_CLAIM_PRESENT"
    NO_ADMITTED_CANDIDATE = "NO_ADMITTED_CANDIDATE"
    CANDIDATE_BUSY_OR_TERMINAL = "CANDIDATE_BUSY_OR_TERMINAL"
    CLAIM_COUNT_OUT_OF_BOUNDS = "CLAIM_COUNT_OUT_OF_BOUNDS"
    PROVIDER_MISMATCH = "PROVIDER_MISMATCH"
    RECORD_MISMATCH = "RECORD_MISMATCH"
    ORIGIN_MISMATCH = "ORIGIN_MISMATCH"
    LOCATOR_MISMATCH = "LOCATOR_MISMATCH"
    NOT_REGISTRY_DETECTION = "NOT_REGISTRY_DETECTION"
    INVALID_REFERENCE_KIND = "INVALID_REFERENCE_KIND"
    INVALID_VALIDATION_STATUS = "INVALID_VALIDATION_STATUS"
    SOURCE_KIND_MISMATCH = "SOURCE_KIND_MISMATCH"
    INVALID_RECORD_ID = "INVALID_RECORD_ID"
    INVALID_ORIGIN_REFERENCE = "INVALID_ORIGIN_REFERENCE"
    KNOWN_ENDPOINT_CONFLICT = "KNOWN_ENDPOINT_CONFLICT"
    PRIOR_REVIEW_REQUIRED = "PRIOR_REVIEW_REQUIRED"
    UNVERIFIED_REGISTRY_INGESTION = "UNVERIFIED_REGISTRY_INGESTION"
    CURRENT_MANAGED_ENDPOINT_CONFLICT = "CURRENT_MANAGED_ENDPOINT_CONFLICT"
    MISSING_LOCATOR_SLOT = "MISSING_LOCATOR_SLOT"


@dataclass(frozen=True, slots=True)
class QualificationEvaluation:
    """The pure result of evaluating the fixed v1 policy."""

    eligible: bool
    route: QualificationRoute
    rule_code: QualificationRuleCode
    observation_id: DiscoveryObservationId | None = None
    provider_id: str | None = None
    provider_record_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a bool")
        if not isinstance(self.route, QualificationRoute):
            raise ValueError("route must be a QualificationRoute")
        if not isinstance(self.rule_code, QualificationRuleCode):
            raise ValueError("rule_code must be a QualificationRuleCode")
        if self.eligible:
            if self.observation_id is None:
                raise ValueError("an eligible evaluation requires an observation_id")
            if self.provider_id != RANSOMLOOK_PROVIDER_ID:
                raise ValueError(
                    "an eligible evaluation requires the RansomLook provider"
                )
            if self.provider_record_id is None:
                raise ValueError("an eligible evaluation requires a provider_record_id")
            if self.route is not QualificationRoute.REGISTRY_QUALIFIED:
                raise ValueError(
                    "an eligible evaluation must route to REGISTRY_QUALIFIED"
                )
        else:
            if self.observation_id is not None:
                raise ValueError(
                    "an ineligible evaluation must not select an observation"
                )
            if self.route is QualificationRoute.REGISTRY_QUALIFIED:
                raise ValueError("an ineligible evaluation cannot qualify")


@dataclass(frozen=True, slots=True)
class QualificationResult:
    """The durable outcome of one evaluate-and-apply call."""

    candidate_id: SourceCandidateId
    route: QualificationRoute
    rule_code: QualificationRuleCode
    status: CandidateStatus
    decision_id: QualificationDecisionId | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ReconDispatchResult:
    """The outcome of one explicit trusted agent-dispatch request.

    ``blocked_route`` is set only when no reconnaissance was started; a
    dispatched run reports its real workflow result instead of inventing a
    routing code.
    """

    dispatched: bool
    blocked_route: QualificationRoute | None = None
    workflow: ReconWorkflowResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dispatched, bool):
            raise ValueError("dispatched must be a bool")
        if self.dispatched and self.blocked_route is not None:
            raise ValueError("a dispatched result must not carry a blocked route")
        if not self.dispatched and self.workflow is not None:
            raise ValueError("a blocked result must not carry a workflow result")


class CandidateQualificationPolicy:
    """The fixed, default-deny registry qualification policy (v1).

    Pure: no persistence, no I/O, no model, no crawler, no randomness.
    """

    @classmethod
    def evaluate(
        cls,
        candidate: SourceCandidate,
        admitted_claims: Sequence[DiscoveryObservation],
        operator_settings: QualificationSettings,
        *,
        admission_results: Sequence[AdmissionResult] = (),
        verified_observation_ids: frozenset[DiscoveryObservationId] = frozenset(),
        current_endpoint_match: tuple[bool, bool] = (False, False),
        has_credentials_pending: bool = False,
    ) -> QualificationEvaluation:
        """Return the deterministic v1 evaluation for one candidate.

        Inputs describing trusted persisted evidence are validated before
        evaluation; malformed values raise a bounded ``ValueError`` rather
        than being coerced into a permissive state.
        """
        _validate_policy_inputs(
            admitted_claims=admitted_claims,
            verified_observation_ids=verified_observation_ids,
            current_endpoint_match=current_endpoint_match,
            has_credentials_pending=has_credentials_pending,
        )
        if candidate.status in _TERMINAL_OR_BUSY:
            return _blocked(
                QualificationRoute.ALREADY_TERMINAL_OR_BUSY,
                QualificationRuleCode.CANDIDATE_BUSY_OR_TERMINAL,
            )
        if candidate.status not in _ELIGIBLE_STATUSES:
            return _blocked(
                QualificationRoute.ALREADY_TERMINAL_OR_BUSY,
                QualificationRuleCode.CANDIDATE_BUSY_OR_TERMINAL,
            )
        if not operator_settings.ransomlook_location_trusted:
            return _blocked(
                QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                QualificationRuleCode.TRUST_DISABLED,
            )
        if has_credentials_pending:
            return _blocked(
                QualificationRoute.PENDING_RECON_AUTH_REQUIRED,
                QualificationRuleCode.AUTH_CLAIM_PRESENT,
            )
        if any(
            result.outcome is AdmissionOutcome.KNOWN_ENDPOINT
            for result in admission_results
        ):
            return _blocked(
                QualificationRoute.PENDING_RECON_AMBIGUOUS,
                QualificationRuleCode.KNOWN_ENDPOINT_CONFLICT,
            )
        if any(
            result.outcome is AdmissionOutcome.REVIEW_REQUIRED
            for result in admission_results
        ):
            return _blocked(
                QualificationRoute.PENDING_RECON_AMBIGUOUS,
                QualificationRuleCode.PRIOR_REVIEW_REQUIRED,
            )
        claims = tuple(admitted_claims)
        if not claims:
            return _blocked(
                QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                QualificationRuleCode.NO_ADMITTED_CANDIDATE,
            )
        if len(claims) > MAX_EVIDENCE_OBSERVATIONS:
            return _blocked(
                QualificationRoute.PENDING_RECON_AMBIGUOUS,
                QualificationRuleCode.CLAIM_COUNT_OUT_OF_BOUNDS,
            )
        slot_found, endpoint_present = current_endpoint_match
        if slot_found is False:
            return _blocked(
                QualificationRoute.PENDING_RECON_AMBIGUOUS,
                QualificationRuleCode.MISSING_LOCATOR_SLOT,
            )
        if endpoint_present is True:
            return _blocked(
                QualificationRoute.PENDING_RECON_AMBIGUOUS,
                QualificationRuleCode.CURRENT_MANAGED_ENDPOINT_CONFLICT,
            )
        claim_ids = {claim.observation_id for claim in claims}
        if not all(
            claim.observation_id in verified_observation_ids for claim in claims
        ):
            return _blocked(
                QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                QualificationRuleCode.UNVERIFIED_REGISTRY_INGESTION,
            )
        if not verified_observation_ids.issubset(claim_ids):
            raise ValueError(
                "verified_observation_ids must belong to the admitted claims"
            )

        reference = claims[0]
        for claim in claims:
            if claim.provider_id != RANSOMLOOK_PROVIDER_ID:
                return _blocked(
                    QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                    QualificationRuleCode.PROVIDER_MISMATCH,
                )
            if claim.detection_method is not DiscoveryDetectionMethod.REGISTRY:
                return _blocked(
                    QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                    QualificationRuleCode.NOT_REGISTRY_DETECTION,
                )
            if claim.provider_record_id != reference.provider_record_id:
                return _blocked(
                    QualificationRoute.PENDING_RECON_AMBIGUOUS,
                    QualificationRuleCode.RECORD_MISMATCH,
                )
            if claim.origin_reference != reference.origin_reference:
                return _blocked(
                    QualificationRoute.PENDING_RECON_AMBIGUOUS,
                    QualificationRuleCode.ORIGIN_MISMATCH,
                )
            if claim.normalized_locator != reference.normalized_locator:
                return _blocked(
                    QualificationRoute.PENDING_RECON_AMBIGUOUS,
                    QualificationRuleCode.LOCATOR_MISMATCH,
                )

        for claim in claims:
            if claim.reference_kind is not DiscoveryReferenceKind.LOCATOR:
                return _blocked(
                    QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                    QualificationRuleCode.INVALID_REFERENCE_KIND,
                )
            if claim.validation_status is not DiscoveryValidationStatus.VALID_LOCATOR:
                return _blocked(
                    QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                    QualificationRuleCode.INVALID_VALIDATION_STATUS,
                )
            if claim.normalized_locator is None:
                return _blocked(
                    QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                    QualificationRuleCode.INVALID_VALIDATION_STATUS,
                )
            if claim.requires_auth_claim is not None:
                return _blocked(
                    QualificationRoute.PENDING_RECON_AUTH_REQUIRED,
                    QualificationRuleCode.AUTH_CLAIM_PRESENT,
                )
            if not _source_kind_matches(claim):
                return _blocked(
                    QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                    QualificationRuleCode.SOURCE_KIND_MISMATCH,
                )
            if not _record_id_is_valid(claim.provider_record_id):
                return _blocked(
                    QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                    QualificationRuleCode.INVALID_RECORD_ID,
                )
            if not _origin_is_valid(claim.origin_reference):
                return _blocked(
                    QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                    QualificationRuleCode.INVALID_ORIGIN_REFERENCE,
                )

        selected = max(
            claims,
            key=lambda item: (item.ingested_at, str(item.observation_id)),
        )
        if selected.normalized_locator is None:  # defensive; validated above
            return _blocked(
                QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE,
                QualificationRuleCode.INVALID_VALIDATION_STATUS,
            )
        return QualificationEvaluation(
            eligible=True,
            route=QualificationRoute.REGISTRY_QUALIFIED,
            rule_code=QualificationRuleCode.RANSOMLOOK_LOCATION_V1,
            observation_id=selected.observation_id,
            provider_id=selected.provider_id,
            provider_record_id=selected.provider_record_id,
        )


def _pending_reason_code(route: QualificationRoute) -> str:
    """Return one bounded, content-free pending-event reason code.

    Rule codes are intentionally not persisted verbatim: some contain
    credential-like substrings that the shared event-context validator
    rejects (and that must never enter durable metadata). The route is the
    nonpersistent, precise diagnostic.
    """
    if route is QualificationRoute.PENDING_RECON_AMBIGUOUS:
        return "PROVENANCE_AMBIGUOUS"
    if route is QualificationRoute.PENDING_RECON_AUTH_REQUIRED:
        return "ACCESS_PENDING"
    return "POLICY_NOT_ELIGIBLE"


def _registry_decision_replay_result(
    candidate: SourceCandidate,
    decision: QualificationDecision,
) -> QualificationResult:
    """Return an accurate replay result for one historical decision.

    Historical decision identity/metadata is preserved; the reported
    ``status`` is the candidate's *current* persisted status. Impossible
    combinations raise a typed invariant error instead of a false success,
    and never write a new decision or event.
    """
    if decision.candidate_id != candidate.candidate_id:
        raise AdmissionInvariantError("registry decision belongs to another candidate")
    if (
        decision.method is not QualificationMethod.REGISTRY_POLICY
        or decision.outcome is not QualificationOutcome.QUALIFIED
    ):
        raise AdmissionInvariantError(
            "stored registry decision has invalid method/outcome"
        )
    if candidate.status not in (CandidateStatus.QUALIFIED, CandidateStatus.PROMOTED):
        raise AdmissionInvariantError(
            "stored registry qualification conflicts with current candidate state"
        )
    return QualificationResult(
        candidate_id=candidate.candidate_id,
        route=QualificationRoute.REGISTRY_QUALIFIED,
        rule_code=QualificationRuleCode.RANSOMLOOK_LOCATION_V1,
        status=candidate.status,
        decision_id=decision.decision_id,
        replayed=True,
    )


def _validate_policy_inputs(
    *,
    admitted_claims: Sequence[DiscoveryObservation],
    verified_observation_ids: frozenset[DiscoveryObservationId],
    current_endpoint_match: tuple[bool, bool],
    has_credentials_pending: bool,
) -> None:
    """Reject malformed trusted-evidence inputs before evaluation."""
    if not isinstance(verified_observation_ids, frozenset):
        raise ValueError("verified_observation_ids must be a frozenset")
    for item in verified_observation_ids:
        if not isinstance(item, DiscoveryObservationId):
            raise ValueError(
                "verified_observation_ids must contain DiscoveryObservationId values"
            )
    if not isinstance(has_credentials_pending, bool):
        raise ValueError("has_credentials_pending must be a bool")
    if (
        not isinstance(current_endpoint_match, tuple)
        or len(current_endpoint_match) != 2
        or not all(isinstance(value, bool) for value in current_endpoint_match)
    ):
        raise ValueError("current_endpoint_match must be two booleans")
    for claim in admitted_claims:
        if not isinstance(claim, DiscoveryObservation):
            raise ValueError("admitted_claims must contain DiscoveryObservation values")


def _blocked(
    route: QualificationRoute, rule_code: QualificationRuleCode
) -> QualificationEvaluation:
    return QualificationEvaluation(
        eligible=False,
        route=route,
        rule_code=rule_code,
    )


def _source_kind_matches(claim: DiscoveryObservation) -> bool:
    if claim.normalized_locator is None:
        return False
    host = urlsplit(claim.normalized_locator).hostname or ""
    expected = EndpointType.ONION if host.endswith(".onion") else EndpointType.CLEARNET
    return claim.source_kind_hint is expected


def _record_id_is_valid(provider_record_id: str) -> bool:
    if not provider_record_id.startswith(RANSOMLOOK_RECORD_PREFIX):
        return False
    digest = provider_record_id[len(RANSOMLOOK_RECORD_PREFIX) :]
    return _RECORD_DIGEST.fullmatch(digest) is not None


def _origin_is_valid(origin_reference: str | None) -> bool:
    if origin_reference is None:
        return False
    if not origin_reference.startswith(RANSOMLOOK_GROUP_ORIGIN_PREFIX):
        return False
    parts = urlsplit(origin_reference)
    if parts.netloc != "www.ransomlook.io":
        return False
    return parts.username is None and parts.password is None


class CandidateQualificationService:
    """Apply the fixed policy and dispatch explicitly authorized recon (PR 19).

    ``evaluate_and_apply`` never starts an agent, crawler, or model. Any agent
    work requires :meth:`dispatch_recon` with explicit ``authorized=True`` and
    runs through the existing :class:`ReconCoordinator`.
    """

    def __init__(
        self,
        *,
        persistence: DarkulaSpi,
        settings: QualificationSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._persistence = persistence
        self._settings = settings
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    async def evaluate_and_apply(
        self, candidate_id: SourceCandidateId
    ) -> QualificationResult:
        """Evaluate the fixed policy and apply the bounded durable result."""
        try:
            return await self._apply_once(candidate_id)
        except ConflictError:
            return await self._resolve_race(candidate_id)

    async def _load(
        self, uow: UnitOfWork, candidate_id: SourceCandidateId
    ) -> tuple[
        SourceCandidate, tuple[DiscoveryObservation, ...], QualificationEvaluation
    ]:
        candidate = await uow.source_candidates.get(candidate_id)
        if candidate is None:
            raise NotFoundError("the candidate does not exist")
        claims = await uow.admission.claims_for_candidate(candidate_id, limit=65)
        results: list[AdmissionResult] = []
        for claim in claims:
            stored = await uow.admission.get(claim.observation_id)
            if stored is not None:
                results.append(stored)
        verified_ids = (
            await uow.admission.verified_registry_observation_ids_for_candidate(
                candidate_id, limit=65
            )
        )
        current_endpoint = await uow.admission.current_endpoint_match(candidate_id)
        has_pending = await uow.access.has_credentials_pending_for_candidate(
            candidate_id
        )
        evaluation = CandidateQualificationPolicy.evaluate(
            candidate,
            claims,
            self._settings,
            admission_results=tuple(results),
            verified_observation_ids=frozenset(verified_ids),
            current_endpoint_match=current_endpoint,
            has_credentials_pending=has_pending,
        )
        return candidate, claims, evaluation

    def _build_decision(
        self,
        candidate: SourceCandidate,
        evaluation: QualificationEvaluation,
    ) -> tuple[QualificationDecision, SourceCandidateEventHistory]:
        if evaluation.observation_id is None or evaluation.provider_record_id is None:
            raise ValueError("an eligible evaluation must select one observation")
        event_id = CandidateEventId.generate()
        event = SourceCandidateEventHistory(
            event_id=event_id,
            candidate_id=candidate.candidate_id,
            event_type=qualification_event_type(QualificationOutcome.QUALIFIED),
            occurred_at=self._clock(),
            context={
                "policy_id": QUALIFICATION_POLICY_ID,
                "policy_version": QUALIFICATION_POLICY_VERSION,
                "rule_id": QUALIFICATION_RULE_ID,
                "observation_id": str(evaluation.observation_id),
            },
            reason="registry policy qualified the candidate",
        )
        outcome = QualificationOutcome.QUALIFIED
        evidence = (evaluation.observation_id,)
        decision = QualificationDecision(
            decision_id=QualificationDecisionId.generate(),
            candidate_id=candidate.candidate_id,
            method=QualificationMethod.REGISTRY_POLICY,
            outcome=outcome,
            evaluated_at=self._clock(),
            policy_id=QUALIFICATION_POLICY_ID,
            policy_version=QUALIFICATION_POLICY_VERSION,
            evidence_observation_ids=evidence,
            event_id=event_id,
            expected_status=candidate.status,
            new_status=qualification_new_status(outcome),
            decision_key=qualification_decision_key(
                candidate_id=candidate.candidate_id,
                method=QualificationMethod.REGISTRY_POLICY,
                policy_id=QUALIFICATION_POLICY_ID,
                policy_version=QUALIFICATION_POLICY_VERSION,
                evidence_observation_ids=evidence,
                recon_assessment_id=None,
            ),
            provider_id=evaluation.provider_id,
            provider_record_id=evaluation.provider_record_id,
            recon_assessment_id=None,
        )
        return decision, event

    async def _existing_registry_decision(
        self,
        uow: UnitOfWork,
        candidate_id: SourceCandidateId,
        claims: Sequence[DiscoveryObservation],
    ) -> QualificationDecision | None:
        """Return the exact stored v1 decision for the latest claim, if any.

        A replay is recognized *before* any status gate so an already
        qualified candidate reports its immutable original decision instead
        of a terminal-state outcome or a duplicate event.
        """
        if not claims:
            return None
        selected = max(
            claims, key=lambda item: (item.ingested_at, str(item.observation_id))
        )
        evidence = (selected.observation_id,)
        key = qualification_decision_key(
            candidate_id=candidate_id,
            method=QualificationMethod.REGISTRY_POLICY,
            policy_id=QUALIFICATION_POLICY_ID,
            policy_version=QUALIFICATION_POLICY_VERSION,
            evidence_observation_ids=evidence,
            recon_assessment_id=None,
        )
        return await uow.qualification.get_by_key(key)

    async def _apply_once(self, candidate_id: SourceCandidateId) -> QualificationResult:
        async with self._persistence.unit_of_work() as uow:
            stored_candidate = await uow.source_candidates.get(candidate_id)
            if stored_candidate is None:
                raise NotFoundError("the candidate does not exist")
            stored_claims = await uow.admission.claims_for_candidate(
                candidate_id, limit=65
            )
            replay = await self._existing_registry_decision(
                uow, candidate_id, stored_claims
            )
            if replay is not None:
                await uow.commit()
                return _registry_decision_replay_result(stored_candidate, replay)
            candidate, _claims, evaluation = await self._load(uow, candidate_id)
            if evaluation.eligible:
                decision, event = self._build_decision(candidate, evaluation)
                existing = await uow.qualification.get_by_key(decision.decision_key)
                if existing is not None:
                    await uow.commit()
                    return _registry_decision_replay_result(candidate, existing)
                stored = await uow.qualification.decide_and_transition(decision, event)
                await uow.commit()
                return QualificationResult(
                    candidate_id=candidate_id,
                    route=QualificationRoute.REGISTRY_QUALIFIED,
                    rule_code=evaluation.rule_code,
                    status=CandidateStatus.QUALIFIED,
                    decision_id=stored.decision_id,
                )
            if candidate.status is CandidateStatus.DISCOVERED:
                event = SourceCandidateEventHistory(
                    event_id=CandidateEventId.generate(),
                    candidate_id=candidate_id,
                    event_type=CandidateEventType.NEEDS_MORE_RECON,
                    occurred_at=self._clock(),
                    context={
                        "policy_id": QUALIFICATION_POLICY_ID,
                        "policy_version": QUALIFICATION_POLICY_VERSION,
                        "rule_id": QUALIFICATION_RULE_ID,
                        "reason_code": _pending_reason_code(evaluation.route),
                    },
                    reason="POLICY_NOT_ELIGIBLE",
                )
                await uow.source_candidates.transition(
                    candidate_id,
                    expected=CandidateStatus.DISCOVERED,
                    new_status=CandidateStatus.RECONNAISSANCE_PENDING,
                    event=event,
                )
                await uow.commit()
                return QualificationResult(
                    candidate_id=candidate_id,
                    route=evaluation.route,
                    rule_code=evaluation.rule_code,
                    status=CandidateStatus.RECONNAISSANCE_PENDING,
                )
            await uow.commit()
            return QualificationResult(
                candidate_id=candidate_id,
                route=evaluation.route,
                rule_code=evaluation.rule_code,
                status=candidate.status,
            )

    async def _resolve_race(
        self, candidate_id: SourceCandidateId
    ) -> QualificationResult:
        """Re-read once after a controlled conflict; never duplicate events."""
        async with self._persistence.unit_of_work() as uow:
            stored_candidate = await uow.source_candidates.get(candidate_id)
            if stored_candidate is None:
                raise NotFoundError("the candidate does not exist")
            stored_claims = await uow.admission.claims_for_candidate(
                candidate_id, limit=65
            )
            replay = await self._existing_registry_decision(
                uow, candidate_id, stored_claims
            )
            if replay is not None:
                await uow.commit()
                return _registry_decision_replay_result(stored_candidate, replay)
            candidate, _claims, evaluation = await self._load(uow, candidate_id)
            if evaluation.eligible:
                decision, _event = self._build_decision(candidate, evaluation)
                existing = await uow.qualification.get_by_key(decision.decision_key)
                if existing is not None:
                    await uow.commit()
                    return _registry_decision_replay_result(candidate, existing)
            await uow.commit()
            return QualificationResult(
                candidate_id=candidate_id,
                route=QualificationRoute.CHECKPOINT_OR_STATE_RACE,
                rule_code=evaluation.rule_code,
                status=candidate.status,
            )

    async def dispatch_recon(
        self,
        candidate_id: SourceCandidateId,
        *,
        coordinator: ReconCoordinator,
        authorized: bool,
    ) -> ReconDispatchResult:
        """Dispatch explicitly authorized reconnaissance through the existing
        coordinator. An unauthorized or blocked request performs no agent,
        crawler, or lifecycle work.
        """
        if authorized is not True:
            return ReconDispatchResult(
                dispatched=False,
                blocked_route=QualificationRoute.PENDING_RECON_UNAUTHORIZED,
            )
        async with self._persistence.unit_of_work() as uow:
            candidate = await uow.source_candidates.get(candidate_id)
            if candidate is None:
                raise NotFoundError("the candidate does not exist")
            claims = await uow.admission.claims_for_candidate(candidate_id, limit=65)
            has_pending = await uow.access.has_credentials_pending_for_candidate(
                candidate_id
            )
            await uow.commit()
        if candidate.status not in _ELIGIBLE_STATUSES:
            return ReconDispatchResult(
                dispatched=False,
                blocked_route=QualificationRoute.ALREADY_TERMINAL_OR_BUSY,
            )
        if not claims or len(claims) > MAX_EVIDENCE_OBSERVATIONS:
            return ReconDispatchResult(
                dispatched=False,
                blocked_route=QualificationRoute.CHECKPOINT_OR_STATE_RACE,
            )
        if has_pending:
            return ReconDispatchResult(
                dispatched=False,
                blocked_route=QualificationRoute.PENDING_RECON_AUTH_REQUIRED,
            )
        context = ReconQualificationContext(
            candidate_id=candidate_id,
            policy_id=QUALIFICATION_POLICY_ID,
            policy_version=QUALIFICATION_POLICY_VERSION,
            evidence_observation_ids=tuple(claim.observation_id for claim in claims),
        )
        workflow = await coordinator.reconnoiter(
            candidate_id, qualification_context=context
        )
        return ReconDispatchResult(dispatched=True, workflow=workflow)


__all__ = [
    "QUALIFICATION_POLICY_ID",
    "QUALIFICATION_POLICY_VERSION",
    "QUALIFICATION_RULE_ID",
    "RANSOMLOOK_GROUP_ORIGIN_PREFIX",
    "RANSOMLOOK_PROVIDER_ID",
    "RANSOMLOOK_RECORD_PREFIX",
    "CandidateQualificationPolicy",
    "CandidateQualificationService",
    "QualificationEvaluation",
    "QualificationResult",
    "QualificationRoute",
    "QualificationRuleCode",
    "ReconDispatchResult",
]
