# SPDX-License-Identifier: AGPL-3.0-only
"""PR 19 trusted qualification routing unit matrix (U19-22 .. U19-33).

The policy and routing under test are production code. Persistence is the
composed in-memory admission fake; qualification decisions are written
through the same PR 17 atomic primitive contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from tests.support.admission_fakes import (
    RANSOMLOOK_ORIGIN,
    T0,
    MemAdmissionSpi,
    build_candidate,
    build_candidate_event,
    build_observation,
    seed_candidate,
    seed_observation,
    seed_verified_observation,
)

from darkula.app.candidate_admission import CandidateAdmissionService
from darkula.app.candidate_qualification import (
    QUALIFICATION_POLICY_ID,
    QUALIFICATION_POLICY_VERSION,
    QUALIFICATION_RULE_ID,
    CandidateQualificationPolicy,
    CandidateQualificationService,
    QualificationRoute,
    QualificationRuleCode,
)
from darkula.config.settings import QualificationSettings
from darkula.domain.discovery import (
    DiscoveryDetectionMethod,
    QualificationDecision,
    QualificationMethod,
    QualificationOutcome,
    qualification_decision_key,
    qualification_new_status,
)
from darkula.domain.identifiers import (
    CandidateEventId,
    DiscoveryObservationId,
    QualificationDecisionId,
    ReconAssessmentId,
    SourceCandidateId,
)
from darkula.domain.source import CandidateStatus, EndpointType

TRUSTED = QualificationSettings(ransomlook_location_trusted=True)
UNTRUSTED = QualificationSettings(ransomlook_location_trusted=False)


@pytest_asyncio.fixture
async def spi() -> MemAdmissionSpi:
    return MemAdmissionSpi()


def _admission(spi: MemAdmissionSpi) -> CandidateAdmissionService:
    return CandidateAdmissionService(persistence=spi, clock=lambda: T0)


def _qualification(
    spi: MemAdmissionSpi, settings: QualificationSettings
) -> CandidateQualificationService:
    return CandidateQualificationService(
        persistence=spi, settings=settings, clock=lambda: T0
    )


async def _admit(
    spi: MemAdmissionSpi, observation: Any, *, verified: bool = False
) -> SourceCandidateId:
    await seed_observation(spi, observation)
    if verified:
        seed_verified_observation(spi, observation.observation_id)
    result = await _admission(spi).admit_observation(observation.observation_id)
    assert isinstance(result.candidate_id, SourceCandidateId)
    return result.candidate_id


def _decision_events(spi: MemAdmissionSpi) -> list[object]:
    return list(spi.discovery.candidate_events)


def _pending_events(
    spi: MemAdmissionSpi, candidate_id: SourceCandidateId
) -> list[object]:
    return list(spi.candidates.history.get(candidate_id, []))


@pytest.mark.asyncio
class TestQualificationMatrix:
    async def test_u19_22_default_off_routes_pending_without_decision(
        self, spi: MemAdmissionSpi
    ) -> None:
        result = await _admit(spi, build_observation(url="http://off.example.test/"))
        outcome = await _qualification(spi, UNTRUSTED).evaluate_and_apply(result)
        assert outcome.route is QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE
        assert outcome.rule_code is QualificationRuleCode.TRUST_DISABLED
        assert outcome.decision_id is None
        assert spi.discovery.decisions_by_key == {}
        assert spi.latest_status(result) is (CandidateStatus.RECONNAISSANCE_PENDING)
        pending = _pending_events(spi, result)
        assert len(pending) == 2  # seeded DISCOVERED + one NEEDS_MORE_RECON

    async def test_u19_23_trusted_complete_record_qualifies_without_recon(
        self, spi: MemAdmissionSpi
    ) -> None:
        result = await _admit(
            spi,
            build_observation(url="http://good.example.test/"),
            verified=True,
        )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(result)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED
        assert outcome.rule_code is QualificationRuleCode.RANSOMLOOK_LOCATION_V1
        assert outcome.decision_id is not None
        decisions = list(spi.discovery.decisions_by_key.values())
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.method is QualificationMethod.REGISTRY_POLICY
        assert decision.policy_id == QUALIFICATION_POLICY_ID
        assert decision.policy_version == QUALIFICATION_POLICY_VERSION
        assert spi.latest_status(result) is CandidateStatus.QUALIFIED
        assert len(_decision_events(spi)) == 1
        assert spi.candidates.assessments == {}

    async def test_u19_24_name_only_never_qualifies(self, spi: MemAdmissionSpi) -> None:
        from darkula.domain.discovery import DiscoveryReferenceKind

        observation = build_observation(
            url="GroupName",
            record_id="group:" + "a" * 64,
            reference_kind=DiscoveryReferenceKind.NAME_ONLY,
        )
        await seed_observation(spi, observation)
        result = await _admission(spi).admit_observation(observation.observation_id)
        assert result.candidate_id is None
        assert spi.candidates.candidates == {}
        assert spi.discovery.decisions_by_key == {}

    async def test_u19_25_unknown_provider_or_record_class_stays_pending(
        self, spi: MemAdmissionSpi
    ) -> None:
        manual = await _admit(
            spi,
            build_observation(
                url="http://manual.example.test/",
                provider_id="internal",
                record_id="manual:1",
                detection_method=DiscoveryDetectionMethod.MANUAL,
                origin_reference=None,
            ),
        )
        service = _qualification(spi, TRUSTED)
        manual_outcome = await service.evaluate_and_apply(manual)
        assert manual_outcome.route is (
            QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE
        )
        # Generic ingestion is unverified, so an untrusted provider can never
        # reach the provider-identity rule; the fail-closed code is returned.
        assert manual_outcome.rule_code is (
            QualificationRuleCode.UNVERIFIED_REGISTRY_INGESTION
        )

        group = await _admit(
            spi,
            build_observation(
                url="http://group-record.example.test/",
                record_id="group:" + "b" * 64,
            ),
            verified=True,
        )
        group_outcome = await service.evaluate_and_apply(group)
        assert group_outcome.route is (
            QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE
        )
        assert group_outcome.rule_code is QualificationRuleCode.INVALID_RECORD_ID
        assert spi.discovery.decisions_by_key == {}

    async def test_u19_26_conflicting_provenance_is_pending(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://conflict.example.test/",
            record_id="location:" + "a" * 64,
            origin_reference=RANSOMLOOK_ORIGIN,
        )
        first_result = await _admit(spi, first, verified=True)
        second = build_observation(
            url="http://conflict.example.test/",
            record_id="location:" + "b" * 64,
            origin_reference="https://www.ransomlook.io/api/group/other",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await seed_observation(spi, second)
        seed_verified_observation(spi, second.observation_id)
        second_result = await _admission(spi).admit_observation(second.observation_id)
        assert second_result.candidate_id == first_result

        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(first_result)
        assert outcome.route is QualificationRoute.PENDING_RECON_AMBIGUOUS
        assert outcome.rule_code is QualificationRuleCode.RECORD_MISMATCH
        assert spi.discovery.decisions_by_key == {}

    async def test_u19_27_latest_version_is_selected(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://versioned.example.test/",
            record_id="location:" + "a" * 64,
            record_version="v1",
            observed_at=T0,
            ingested_at=T0,
        )
        first_result = await _admit(spi, first, verified=True)
        second = build_observation(
            url="http://versioned.example.test/",
            record_id="location:" + "a" * 64,
            record_version="v2",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await seed_observation(spi, second)
        seed_verified_observation(spi, second.observation_id)
        await _admission(spi).admit_observation(second.observation_id)

        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(first_result)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED
        decision = next(iter(spi.discovery.decisions_by_key.values()))
        assert decision.evidence_observation_ids == (second.observation_id,)
        # Historical evidence is retained in the admission ledger.
        claims = await _claims(spi, first_result)
        assert {claim.observation_id for claim in claims} == {
            first.observation_id,
            second.observation_id,
        }

    async def test_u19_28_source_kind_hint_mismatch_stays_pending(
        self, spi: MemAdmissionSpi
    ) -> None:
        result = await _admit(
            spi,
            build_observation(
                url="http://kind-mismatch.onion/",
                source_kind_hint=EndpointType.CLEARNET,
            ),
            verified=True,
        )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(result)
        assert outcome.route is (QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE)
        assert outcome.rule_code is QualificationRuleCode.SOURCE_KIND_MISMATCH
        assert spi.discovery.decisions_by_key == {}

    async def test_u19_29_disabling_after_success_preserves_history(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = await _admit(
            spi,
            build_observation(url="http://kept.example.test/"),
            verified=True,
        )
        qualified = await _qualification(spi, TRUSTED).evaluate_and_apply(first)
        assert qualified.route is QualificationRoute.REGISTRY_QUALIFIED

        replay = await _qualification(spi, UNTRUSTED).evaluate_and_apply(first)
        assert replay.replayed is True
        assert len(spi.discovery.decisions_by_key) == 1

        second = await _admit(
            spi,
            build_observation(
                url="http://fresh.example.test/",
                record_id="location:" + "b" * 64,
            ),
        )
        fresh = await _qualification(spi, UNTRUSTED).evaluate_and_apply(second)
        assert fresh.route is QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE
        assert len(spi.discovery.decisions_by_key) == 1

    async def test_u19_30_pending_then_enabled_qualifies_once(
        self, spi: MemAdmissionSpi
    ) -> None:
        result = await _admit(
            spi,
            build_observation(url="http://later.example.test/"),
            verified=True,
        )
        pending = await _qualification(spi, UNTRUSTED).evaluate_and_apply(result)
        assert pending.route is QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE
        pending_events = _pending_events(spi, result)
        assert len(pending_events) == 2

        qualified = await _qualification(spi, TRUSTED).evaluate_and_apply(result)
        assert qualified.route is QualificationRoute.REGISTRY_QUALIFIED
        assert spi.latest_status(result) is CandidateStatus.QUALIFIED
        assert len(_pending_events(spi, result)) == 2
        again = await _qualification(spi, TRUSTED).evaluate_and_apply(result)
        assert again.replayed is True
        assert len(spi.discovery.decisions_by_key) == 1

    async def test_u19_31_concurrent_qualification_yields_one_winner(
        self, spi: MemAdmissionSpi
    ) -> None:
        result = await _admit(
            spi,
            build_observation(url="http://race.example.test/"),
            verified=True,
        )
        service = _qualification(spi, TRUSTED)
        first = await service.evaluate_and_apply(result)
        second = await service.evaluate_and_apply(result)
        assert first.decision_id is not None
        assert second.decision_id == first.decision_id
        assert len(spi.discovery.decisions_by_key) == 1
        assert len(_decision_events(spi)) == 1

    async def test_u19_32_exact_decision_replay_retains_identity(
        self, spi: MemAdmissionSpi
    ) -> None:
        result = await _admit(
            spi,
            build_observation(url="http://replay.example.test/"),
            verified=True,
        )
        service = _qualification(spi, TRUSTED)
        first = await service.evaluate_and_apply(result)
        decision = next(iter(spi.discovery.decisions_by_key.values()))
        stored_event = _decision_events(spi)[0]

        second = await service.evaluate_and_apply(result)

        assert second.replayed is True
        assert second.decision_id == first.decision_id
        assert next(iter(spi.discovery.decisions_by_key.values())) == decision
        assert _decision_events(spi)[0] == stored_event
        assert len(_decision_events(spi)) == 1

    async def test_u19_33_terminal_candidate_is_untouched(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = build_candidate(
            entrypoint="http://busy.example.test/",
            status=CandidateStatus.UNDER_RECONNAISSANCE,
        )
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        history_before = list(spi.candidates.history[candidate.candidate_id])

        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(
            candidate.candidate_id
        )

        assert outcome.route is QualificationRoute.ALREADY_TERMINAL_OR_BUSY
        assert outcome.decision_id is None
        assert spi.discovery.decisions_by_key == {}
        assert list(spi.candidates.history[candidate.candidate_id]) == history_before
        assert spi.candidates.assessments == {}


@pytest.mark.asyncio
class TestPolicyPurity:
    async def test_rule_identity_is_frozen(self) -> None:
        assert QUALIFICATION_POLICY_ID == "candidate-qualification"
        assert QUALIFICATION_POLICY_VERSION == "v1"
        assert QUALIFICATION_RULE_ID == "RANSOMLOOK_LOCATION_V1"

    async def test_policy_is_pure_for_ineligible_inputs(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = build_candidate()
        evaluation = CandidateQualificationPolicy.evaluate(candidate, (), UNTRUSTED)
        assert evaluation.eligible is False
        assert evaluation.rule_code is QualificationRuleCode.TRUST_DISABLED


async def _claims(
    spi: MemAdmissionSpi, candidate_id: SourceCandidateId
) -> tuple[Any, ...]:
    async with spi.unit_of_work() as uow:
        claims = await uow.admission.claims_for_candidate(candidate_id)
        await uow.commit()
        return claims


# ---------------------------------------------------------------------------
# PR 19-F1 — trusted-provenance/current-state/replay/bounded-access matrix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFixupPolicyGates:
    """U19F-12 .. U19F-27: verified provenance, endpoint guard, replay."""

    async def test_u19f_12_unverified_cannot_qualify(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = await _admit(
            spi, build_observation(url="http://forged.example.test/")
        )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE
        assert outcome.rule_code is QualificationRuleCode.UNVERIFIED_REGISTRY_INGESTION
        assert spi.discovery.decisions_by_key == {}

    async def test_u19f_13_verified_qualifies_once(self, spi: MemAdmissionSpi) -> None:
        candidate = await _admit(
            spi, build_observation(url="http://verified.example.test/"), verified=True
        )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED
        assert len(spi.discovery.decisions_by_key) == 1
        assert spi.candidates.assessments == {}

    async def test_u19f_14_mixed_provenance_fails_closed(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://mixed.example.test/",
            record_id="location:" + "a" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        second = build_observation(
            url="http://mixed.example.test/",
            record_id="location:" + "a" * 64,
            record_version="v2",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await seed_observation(spi, second)
        result = await _admission(spi).admit_observation(second.observation_id)
        assert result.candidate_id == candidate

        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.rule_code is QualificationRuleCode.UNVERIFIED_REGISTRY_INGESTION
        assert spi.discovery.decisions_by_key == {}

    async def test_u19f_15_all_verified_qualifies_with_latest(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://all-verified.example.test/",
            record_id="location:" + "a" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        second = build_observation(
            url="http://all-verified.example.test/",
            record_id="location:" + "a" * 64,
            record_version="v2",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await seed_observation(spi, second)
        seed_verified_observation(spi, second.observation_id)
        await _admission(spi).admit_observation(second.observation_id)

        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED
        decision = next(iter(spi.discovery.decisions_by_key.values()))
        assert decision.evidence_observation_ids == (second.observation_id,)

    async def test_u19f_16_verified_but_trust_off(self, spi: MemAdmissionSpi) -> None:
        candidate = await _admit(
            spi, build_observation(url="http://trust-off.example.test/"), verified=True
        )
        outcome = await _qualification(spi, UNTRUSTED).evaluate_and_apply(candidate)
        assert outcome.rule_code is QualificationRuleCode.TRUST_DISABLED
        assert spi.discovery.decisions_by_key == {}

    async def test_u19f_17_post_admission_endpoint_blocks(
        self, spi: MemAdmissionSpi
    ) -> None:
        from tests.support.admission_fakes import seed_endpoint

        from darkula.domain.identifiers import SourceEndpointId

        observation = build_observation(url="http://late-endpoint.example.test/")
        candidate = await _admit(spi, observation, verified=True)
        await seed_endpoint(
            spi, SourceEndpointId.generate(), observation.normalized_locator or ""
        )
        before = len(spi.admission.endpoints)
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.PENDING_RECON_AMBIGUOUS
        assert (
            outcome.rule_code is QualificationRuleCode.CURRENT_MANAGED_ENDPOINT_CONFLICT
        )
        assert len(spi.admission.endpoints) == before
        assert spi.candidates.candidates[candidate].status is (
            CandidateStatus.RECONNAISSANCE_PENDING
        )

    async def test_u19f_18_two_endpoints_still_block(
        self, spi: MemAdmissionSpi
    ) -> None:
        from tests.support.admission_fakes import seed_endpoint

        from darkula.domain.identifiers import SourceEndpointId

        observation = build_observation(url="http://two-endpoints.example.test/")
        candidate = await _admit(spi, observation, verified=True)
        for _ in range(2):
            await seed_endpoint(
                spi,
                SourceEndpointId.generate(),
                observation.normalized_locator or "",
            )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert (
            outcome.rule_code is QualificationRuleCode.CURRENT_MANAGED_ENDPOINT_CONFLICT
        )
        assert spi.discovery.decisions_by_key == {}

    async def test_u19f_19_missing_slot_blocks(self, spi: MemAdmissionSpi) -> None:
        from darkula.domain.candidate_admission import (
            AdmissionOutcome,
            AdmissionReason,
            AdmissionResult,
        )

        observation = build_observation(url="http://no-slot.example.test/")
        candidate = build_candidate(entrypoint=observation.normalized_locator or "")
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        await seed_observation(spi, observation)
        seed_verified_observation(spi, observation.observation_id)
        spi.admission.ledger[observation.observation_id] = AdmissionResult(
            observation_id=observation.observation_id,
            outcome=AdmissionOutcome.ADMITTED_EXISTING,
            reason=AdmissionReason.EXISTING_LEGACY_CANDIDATE,
            candidate_id=candidate.candidate_id,
            matched_endpoint_ids=(),
        )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(
            candidate.candidate_id
        )
        assert outcome.route is QualificationRoute.PENDING_RECON_AMBIGUOUS
        assert outcome.rule_code is QualificationRuleCode.MISSING_LOCATOR_SLOT
        assert spi.discovery.decisions_by_key == {}

    async def test_u19f_20_near_match_endpoint_does_not_block(
        self, spi: MemAdmissionSpi
    ) -> None:
        from tests.support.admission_fakes import seed_endpoint

        from darkula.domain.identifiers import SourceEndpointId

        observation = build_observation(url="http://near-match.example.test/")
        candidate = await _admit(spi, observation, verified=True)
        await seed_endpoint(
            spi,
            SourceEndpointId.generate(),
            "http://near-match.example.test/other-path",
        )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED

    async def test_u19f_21_pending_credentials_block(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = await _admit(
            spi,
            build_observation(
                url="http://pending-auth.example.test/",
                requires_auth_claim=True,
            ),
            verified=True,
        )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.PENDING_RECON_AUTH_REQUIRED
        assert outcome.rule_code is QualificationRuleCode.AUTH_CLAIM_PRESENT
        assert spi.discovery.decisions_by_key == {}

    async def test_u19f_22_malformed_policy_inputs_raise(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(url="http://malformed-input.example.test/")
        candidate = build_candidate()
        with pytest.raises(ValueError):
            CandidateQualificationPolicy.evaluate(
                candidate,
                (observation,),
                TRUSTED,
                verified_observation_ids=frozenset({"not-an-id"}),  # type: ignore[arg-type]
                current_endpoint_match=(True, False),
            )
        with pytest.raises(ValueError):
            CandidateQualificationPolicy.evaluate(
                candidate,
                (observation,),
                TRUSTED,
                verified_observation_ids=frozenset({observation.observation_id}),
                current_endpoint_match=(True,),  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError):
            CandidateQualificationPolicy.evaluate(
                candidate,
                (observation,),
                TRUSTED,
                verified_observation_ids=frozenset({observation.observation_id}),
                current_endpoint_match=(True, 1),  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError):
            CandidateQualificationPolicy.evaluate(
                candidate,
                (observation,),
                TRUSTED,
                has_credentials_pending="yes",  # type: ignore[arg-type]
            )

    async def test_u19f_23_seven_claims_over_bound(self, spi: MemAdmissionSpi) -> None:
        candidate: SourceCandidateId | None = None
        for index in range(65):
            observation = build_observation(
                url="http://too-many.example.test/",
                record_id="location:" + f"{index:064x}",
                observed_at=T0 + timedelta(seconds=index),
                ingested_at=T0 + timedelta(seconds=index),
            )
            candidate = await _admit(spi, observation, verified=True)
        assert candidate is not None
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.rule_code is QualificationRuleCode.CLAIM_COUNT_OUT_OF_BOUNDS
        assert spi.discovery.decisions_by_key == {}

    async def test_u19f_24_qualified_replay_current_status(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = await _admit(
            spi,
            build_observation(url="http://replay-status.example.test/"),
            verified=True,
        )
        first = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert first.decision_id is not None
        assert replay.decision_id == first.decision_id
        assert replay.status is CandidateStatus.QUALIFIED
        assert replay.replayed is True

    async def test_u19f_25_promoted_replay_reports_promoted(
        self, spi: MemAdmissionSpi
    ) -> None:
        from darkula.domain.identifiers import CandidateEventId
        from darkula.domain.source import CandidateEventType

        candidate = await _admit(
            spi, build_observation(url="http://promoted.example.test/"), verified=True
        )
        first = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        async with spi.unit_of_work() as uow:
            stored = await uow.source_candidates.get(candidate)
            assert stored is not None
            await uow.source_candidates.transition(
                candidate,
                expected=CandidateStatus.QUALIFIED,
                new_status=CandidateStatus.PROMOTED,
                event=build_candidate_event(
                    stored, event_type=CandidateEventType.PROMOTED
                ),
            )
            await uow.commit()
        del CandidateEventId

        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == first.decision_id
        assert replay.status is CandidateStatus.PROMOTED
        assert replay.replayed is True

    async def test_u19f_26_impossible_status_is_invariant_error(
        self, spi: MemAdmissionSpi
    ) -> None:
        from dataclasses import replace as dataclass_replace

        from darkula.app.persistence import AdmissionInvariantError

        candidate = await _admit(
            spi, build_observation(url="http://corrupt.example.test/"), verified=True
        )
        await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        stored = spi.candidates.candidates[candidate]
        spi.candidates.candidates[candidate] = dataclass_replace(
            stored, status=CandidateStatus.REJECTED
        )
        with pytest.raises(AdmissionInvariantError):
            await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)

    async def test_u19f_27_race_replay_reports_current_status(
        self, spi: MemAdmissionSpi
    ) -> None:
        from darkula.domain.source import CandidateEventType

        candidate = await _admit(
            spi,
            build_observation(url="http://race-promoted.example.test/"),
            verified=True,
        )
        first = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        async with spi.unit_of_work() as uow:
            stored = await uow.source_candidates.get(candidate)
            assert stored is not None
            await uow.source_candidates.transition(
                candidate,
                expected=CandidateStatus.QUALIFIED,
                new_status=CandidateStatus.PROMOTED,
                event=build_candidate_event(
                    stored, event_type=CandidateEventType.PROMOTED
                ),
            )
            await uow.commit()
        service = _qualification(spi, TRUSTED)
        result = await service._resolve_race(candidate)
        assert result.decision_id == first.decision_id
        assert result.status is CandidateStatus.PROMOTED


@pytest.mark.asyncio
class TestFixupBoundedReads:
    """U19F-28 .. U19F-36: bounded access probe and query-shape guards."""

    async def test_u19f_28_boolean_evidence_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.support.discovery_fakes import MemAccessRepository

        from darkula.domain.discovery import AccessEvidence, AccessStatus
        from darkula.domain.identifiers import AccessEvidenceId, DiscoveryObservationId

        async def _forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("list_for_candidate must not be used")

        monkeypatch.setattr(MemAccessRepository, "list_for_candidate", _forbidden)
        for count, expected in ((0, False), (1, True), (64, True), (512, True)):
            spi = MemAdmissionSpi()
            candidate = SourceCandidateId.generate()
            for index in range(count):
                observation_id = DiscoveryObservationId.generate()
                evidence = AccessEvidence(
                    access_evidence_id=AccessEvidenceId.generate(),
                    observation_id=observation_id,
                    candidate_id=candidate,
                    source_endpoint_id=None,
                    status=AccessStatus.CREDENTIALS_PENDING,
                    reason_code="PROVIDER_AUTH_CLAIM",
                    recorded_at=T0,
                    evidence_reference="registry://ransomlook/location/" + str(index),
                )
                spi.discovery.access[
                    (observation_id, str(candidate), evidence.status.value)
                ] = evidence
            async with spi.unit_of_work() as uow:
                result = await uow.access.has_credentials_pending_for_candidate(
                    candidate
                )
            assert result is expected

    async def test_u19f_29_load_uses_only_boolean_probe(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.support.discovery_fakes import MemAccessRepository

        candidate = await _admit(
            spi,
            build_observation(url="http://load-bounded.example.test/"),
            verified=True,
        )

        async def _forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("list_for_candidate must not be used")

        monkeypatch.setattr(MemAccessRepository, "list_for_candidate", _forbidden)
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED

    async def test_u19f_30_dispatch_uses_only_boolean_probe(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.support.discovery_fakes import MemAccessRepository

        class _Coordinator:
            def __init__(self) -> None:
                self.calls = 0

            async def reconnoiter(
                self, candidate_id: Any, *, qualification_context: Any = None
            ) -> Any:
                del candidate_id, qualification_context
                self.calls += 1
                return object()

        async def _forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("list_for_candidate must not be used")

        monkeypatch.setattr(MemAccessRepository, "list_for_candidate", _forbidden)
        candidate = await _admit(
            spi,
            build_observation(url="http://dispatch-bounded.example.test/"),
            verified=True,
        )
        coordinator = _Coordinator()
        outcome = await _qualification(spi, TRUSTED).dispatch_recon(
            candidate,
            coordinator=coordinator,  # type: ignore[arg-type]
            authorized=True,
        )
        assert outcome.dispatched is True
        assert coordinator.calls == 1

        pending = await _admit(
            spi,
            build_observation(
                url="http://dispatch-auth.example.test/",
                record_id="location:" + "b" * 64,
                requires_auth_claim=True,
            ),
            verified=True,
        )
        blocked = await _qualification(spi, TRUSTED).dispatch_recon(
            pending,
            coordinator=_Coordinator(),  # type: ignore[arg-type]
            authorized=True,
        )
        assert blocked.dispatched is False
        assert blocked.blocked_route is (QualificationRoute.PENDING_RECON_AUTH_REQUIRED)

    async def test_u19f_33_disabled_after_history(self, spi: MemAdmissionSpi) -> None:
        candidate = await _admit(
            spi, build_observation(url="http://revoked.example.test/"), verified=True
        )
        first = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        replay = await _qualification(spi, UNTRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == first.decision_id
        assert replay.replayed is True
        assert len(spi.discovery.decisions_by_key) == 1

        fresh = await _admit(
            spi,
            build_observation(
                url="http://revoked-fresh.example.test/",
                record_id="location:" + "c" * 64,
            ),
            verified=True,
        )
        blocked = await _qualification(spi, UNTRUSTED).evaluate_and_apply(fresh)
        assert blocked.rule_code is QualificationRuleCode.TRUST_DISABLED
        assert len(spi.discovery.decisions_by_key) == 1

    async def test_u19f_34_current_endpoint_probe_is_one_boolean_call(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.support.admission_fakes import MemAdmissionRepository

        candidate = await _admit(
            spi,
            build_observation(url="http://one-probe.example.test/"),
            verified=True,
        )
        calls: list[tuple[bool, bool]] = []
        original = MemAdmissionRepository.current_endpoint_match

        async def _spy(self: Any, candidate_id: Any) -> tuple[bool, bool]:
            result = await original(self, candidate_id)
            calls.append(result)
            return result

        monkeypatch.setattr(MemAdmissionRepository, "current_endpoint_match", _spy)
        await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert len(calls) == 1
        assert isinstance(calls[0][0], bool)
        assert isinstance(calls[0][1], bool)

    async def test_u19f_35_verified_id_limits(self, spi: MemAdmissionSpi) -> None:
        candidate = await _admit(
            spi, build_observation(url="http://limits.example.test/"), verified=True
        )
        for bad in (0, 66, -1):
            with pytest.raises(ValueError):
                async with spi.unit_of_work() as uow:
                    await uow.admission.verified_registry_observation_ids_for_candidate(
                        candidate, limit=bad
                    )
        async with spi.unit_of_work() as uow:
            ids = await uow.admission.verified_registry_observation_ids_for_candidate(
                candidate, limit=65
            )
        assert len(ids) == 1

    async def test_u19f_36_access_probe_returns_bool(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = SourceCandidateId.generate()
        async with spi.unit_of_work() as uow:
            result = await uow.access.has_credentials_pending_for_candidate(candidate)
        assert result is False


# ---------------------------------------------------------------------------
# PR 19-F2 — stable historical registry qualification replay (U19F2-01..20).
# ---------------------------------------------------------------------------


def _make_registry_decision(
    candidate_id: SourceCandidateId,
    evidence_observation_id: DiscoveryObservationId,
    *,
    outcome: QualificationOutcome = QualificationOutcome.QUALIFIED,
    policy_id: str = QUALIFICATION_POLICY_ID,
    policy_version: str = QUALIFICATION_POLICY_VERSION,
    evaluated_at: datetime = T0,
    provider_record_id: str = "location:" + "a" * 64,
) -> QualificationDecision:
    """Build one valid historical REGISTRY_POLICY decision fixture."""
    method = QualificationMethod.REGISTRY_POLICY
    evidence = (evidence_observation_id,)
    return QualificationDecision(
        decision_id=QualificationDecisionId.generate(),
        candidate_id=candidate_id,
        method=method,
        outcome=outcome,
        evaluated_at=evaluated_at,
        policy_id=policy_id,
        policy_version=policy_version,
        evidence_observation_ids=evidence,
        event_id=CandidateEventId.generate(),
        expected_status=CandidateStatus.DISCOVERED,
        new_status=qualification_new_status(outcome),
        decision_key=qualification_decision_key(
            candidate_id=candidate_id,
            method=method,
            policy_id=policy_id,
            policy_version=policy_version,
            evidence_observation_ids=evidence,
            recon_assessment_id=None,
        ),
        provider_id="ransomlook",
        provider_record_id=provider_record_id,
        recon_assessment_id=None,
    )


@pytest.mark.asyncio
class TestRegistryReplayAfterNewObservations:
    """U19F2-01..20: candidate-level historic replay across new observations."""

    async def test_u19f2_01_replay_after_new_version(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://f2-replay.example.test/",
            record_id="location:" + "a" * 64,
            record_version="v1",
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        original = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert original.decision_id is not None
        second = build_observation(
            url="http://f2-replay.example.test/",
            record_id="location:" + "a" * 64,
            record_version="v2",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        assert await _admit(spi, second, verified=True) == candidate
        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == original.decision_id
        assert replay.status is CandidateStatus.QUALIFIED
        assert replay.replayed is True
        assert len(spi.discovery.decisions_by_key) == 1
        assert len(_decision_events(spi)) == 1

    async def test_u19f2_02_promoted_replay_after_new_version(
        self, spi: MemAdmissionSpi
    ) -> None:
        from darkula.domain.source import CandidateEventType

        first = build_observation(
            url="http://f2-promoted.example.test/",
            record_id="location:" + "b" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        original = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        async with spi.unit_of_work() as uow:
            stored = await uow.source_candidates.get(candidate)
            assert stored is not None
            await uow.source_candidates.transition(
                candidate,
                expected=CandidateStatus.QUALIFIED,
                new_status=CandidateStatus.PROMOTED,
                event=build_candidate_event(
                    stored, event_type=CandidateEventType.PROMOTED
                ),
            )
            await uow.commit()
        second = build_observation(
            url="http://f2-promoted.example.test/",
            record_id="location:" + "b" * 64,
            record_version="v2",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await _admit(spi, second, verified=True)
        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == original.decision_id
        assert replay.status is CandidateStatus.PROMOTED
        assert replay.replayed is True
        assert len(spi.discovery.decisions_by_key) == 1

    async def test_u19f2_03_unverified_new_observation_does_not_block_replay(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://f2-unverified.example.test/",
            record_id="location:" + "c" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        original = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        second = build_observation(
            url="http://f2-unverified.example.test/",
            record_id="location:" + "c" * 64,
            record_version="v2",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await _admit(spi, second, verified=False)
        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == original.decision_id
        assert second.observation_id not in spi.admission.verified

    async def test_u19f2_04_conflicting_later_claim_does_not_block_replay(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://f2-conflict.example.test/",
            record_id="location:" + "d" * 64,
            origin_reference=RANSOMLOOK_ORIGIN,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        original = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        second = build_observation(
            url="http://f2-conflict.example.test/",
            record_id="location:" + "e" * 64,
            origin_reference="https://www.ransomlook.io/api/group/other",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await _admit(spi, second, verified=True)
        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == original.decision_id
        assert len(spi.discovery.decisions_by_key) == 1

    async def test_u19f2_05_more_than_64_claims_replays(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://f2-many.example.test/",
            record_id="location:" + "f" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        original = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        for index in range(65):
            observation = build_observation(
                url="http://f2-many.example.test/",
                record_id="location:" + "f" * 64,
                record_version=f"v{index + 2}",
                observed_at=T0 + timedelta(seconds=index + 1),
                ingested_at=T0 + timedelta(seconds=index + 1),
            )
            await _admit(spi, observation, verified=True)
        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == original.decision_id
        assert replay.replayed is True
        assert len(spi.discovery.decisions_by_key) == 1

    async def test_u19f2_06_same_instant_new_observation_replays(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://f2-tie.example.test/",
            record_id="location:" + "1" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        original = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        second = build_observation(
            url="http://f2-tie.example.test/",
            record_id="location:" + "1" * 64,
            record_version="v2",
            observed_at=T0,
            ingested_at=T0,
        )
        await _admit(spi, second, verified=True)
        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == original.decision_id

    async def test_u19f2_07_trust_off_still_replays_history(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://f2-trust-off.example.test/",
            record_id="location:" + "2" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        original = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        second = build_observation(
            url="http://f2-trust-off.example.test/",
            record_id="location:" + "2" * 64,
            record_version="v2",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await _admit(spi, second, verified=True)
        replay = await _qualification(spi, UNTRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == original.decision_id
        assert replay.replayed is True

    async def test_u19f2_08_auth_claim_new_observation_replays(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://f2-auth.example.test/",
            record_id="location:" + "3" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        original = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        second = build_observation(
            url="http://f2-auth.example.test/",
            record_id="location:" + "3" * 64,
            record_version="v2",
            requires_auth_claim=True,
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await _admit(spi, second, verified=True)
        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == original.decision_id
        assert replay.replayed is True

    async def test_u19f2_09_agent_recon_terminal_is_not_registry_replay(
        self, spi: MemAdmissionSpi
    ) -> None:
        from darkula.domain.discovery import (
            QualificationMethod,
            qualification_decision_key,
        )
        from darkula.domain.identifiers import (
            CandidateEventId,
            QualificationDecisionId,
        )

        candidate = build_candidate(
            entrypoint="http://f2-agent.example.test/",
            status=CandidateStatus.QUALIFIED,
        )
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        assessment_id = ReconAssessmentId.generate()
        evidence = (
            build_observation(url="http://f2-agent.example.test/").observation_id,
        )
        key = qualification_decision_key(
            candidate_id=candidate.candidate_id,
            method=QualificationMethod.AGENT_RECON,
            policy_id="candidate-qualification",
            policy_version="v1",
            evidence_observation_ids=evidence,
            recon_assessment_id=assessment_id,
        )
        decision = QualificationDecision(
            decision_id=QualificationDecisionId.generate(),
            candidate_id=candidate.candidate_id,
            method=QualificationMethod.AGENT_RECON,
            outcome=QualificationOutcome.QUALIFIED,
            evaluated_at=T0,
            policy_id="candidate-qualification",
            policy_version="v1",
            evidence_observation_ids=evidence,
            event_id=CandidateEventId.generate(),
            expected_status=CandidateStatus.UNDER_RECONNAISSANCE,
            new_status=CandidateStatus.QUALIFIED,
            decision_key=key,
            recon_assessment_id=assessment_id,
        )
        spi.discovery.decisions_by_key[key] = decision
        spi.discovery.decisions_by_id[decision.decision_id] = decision
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(
            candidate.candidate_id
        )
        assert outcome.route is QualificationRoute.ALREADY_TERMINAL_OR_BUSY
        assert outcome.decision_id is None

    async def test_u19f2_10_discovered_no_decision_trust_off(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = await _admit(
            spi, build_observation(url="http://f2-none-off.example.test/")
        )
        outcome = await _qualification(spi, UNTRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE
        assert outcome.decision_id is None
        assert spi.discovery.decisions_by_key == {}

    async def test_u19f2_11_prospective_qualification_unchanged(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = await _admit(
            spi,
            build_observation(url="http://f2-prospective.example.test/"),
            verified=True,
        )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED
        assert outcome.replayed is False
        assert len(spi.discovery.decisions_by_key) == 1

    async def test_u19f2_12_two_registry_decisions_fail_closed(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from darkula.app.persistence import AdmissionInvariantError

        candidate = build_candidate(
            entrypoint="http://f2-duplicate.example.test/",
            status=CandidateStatus.QUALIFIED,
        )
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        two = (
            _make_registry_decision(
                candidate.candidate_id,
                build_observation(
                    url="http://f2-duplicate.example.test/"
                ).observation_id,
                provider_record_id="location:" + "a" * 64,
            ),
            _make_registry_decision(
                candidate.candidate_id,
                build_observation(
                    url="http://f2-duplicate.example.test/"
                ).observation_id,
                provider_record_id="location:" + "a" * 64,
                evaluated_at=T0 + timedelta(minutes=1),
            ),
        )
        for decision in two:
            spi.discovery.decisions_by_key[decision.decision_key] = decision
            spi.discovery.decisions_by_id[decision.decision_id] = decision
        with pytest.raises(AdmissionInvariantError):
            await _qualification(spi, TRUSTED).evaluate_and_apply(
                candidate.candidate_id
            )
        assert len(spi.discovery.decisions_by_key) == 2

    async def test_u19f2_13_incompatible_status_fails_closed(
        self, spi: MemAdmissionSpi
    ) -> None:
        from darkula.app.persistence import AdmissionInvariantError

        candidate = build_candidate(
            entrypoint="http://f2-rejected.example.test/",
            status=CandidateStatus.REJECTED,
        )
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        decision = _make_registry_decision(
            candidate.candidate_id,
            build_observation(url="http://f2-rejected.example.test/").observation_id,
            provider_record_id="location:" + "b" * 64,
        )
        spi.discovery.decisions_by_key[decision.decision_key] = decision
        spi.discovery.decisions_by_id[decision.decision_id] = decision
        with pytest.raises(AdmissionInvariantError):
            await _qualification(spi, TRUSTED).evaluate_and_apply(
                candidate.candidate_id
            )

    async def test_u19f2_14_non_qualified_outcome_fails_closed(
        self, spi: MemAdmissionSpi
    ) -> None:
        from darkula.app.persistence import AdmissionInvariantError

        candidate = build_candidate(
            entrypoint="http://f2-outcome.example.test/",
            status=CandidateStatus.REJECTED,
        )
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        decision = _make_registry_decision(
            candidate.candidate_id,
            build_observation(url="http://f2-outcome.example.test/").observation_id,
            provider_record_id="location:" + "c" * 64,
            outcome=QualificationOutcome.REJECTED,
        )
        spi.discovery.decisions_by_key[decision.decision_key] = decision
        spi.discovery.decisions_by_id[decision.decision_id] = decision
        with pytest.raises(AdmissionInvariantError):
            await _qualification(spi, TRUSTED).evaluate_and_apply(
                candidate.candidate_id
            )

    async def test_u19f2_15_wrong_candidate_or_policy_fails_closed(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.support.discovery_fakes import MemQualificationRepository

        from darkula.app.persistence import AdmissionInvariantError

        candidate = build_candidate(
            entrypoint="http://f2-wrong.example.test/",
            status=CandidateStatus.QUALIFIED,
        )
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        other_candidate = SourceCandidateId.generate()
        wrong = _make_registry_decision(
            other_candidate,
            build_observation(url="http://f2-other.example.test/").observation_id,
            provider_record_id="location:" + "d" * 64,
        )

        async def _wrong_candidate(
            self: MemQualificationRepository, candidate_id: SourceCandidateId
        ) -> tuple[QualificationDecision, ...]:
            del candidate_id
            return (wrong,)

        monkeypatch.setattr(
            MemQualificationRepository,
            "registry_v1_decisions_for_candidate",
            _wrong_candidate,
        )
        with pytest.raises(AdmissionInvariantError):
            await _qualification(spi, TRUSTED).evaluate_and_apply(
                candidate.candidate_id
            )

        wrong_policy = _make_registry_decision(
            candidate.candidate_id,
            build_observation(url="http://f2-wrong.example.test/").observation_id,
            provider_record_id="location:" + "d" * 64,
            policy_id="other-policy",
            policy_version="v9",
        )

        async def _wrong_policy(
            self: MemQualificationRepository, candidate_id: SourceCandidateId
        ) -> tuple[QualificationDecision, ...]:
            del candidate_id
            return (wrong_policy,)

        monkeypatch.setattr(
            MemQualificationRepository,
            "registry_v1_decisions_for_candidate",
            _wrong_policy,
        )
        with pytest.raises(AdmissionInvariantError):
            await _qualification(spi, TRUSTED).evaluate_and_apply(
                candidate.candidate_id
            )

    async def test_u19f2_16_other_policy_and_agent_not_registry_replay(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = build_candidate(
            entrypoint="http://f2-other-policy.example.test/",
            status=CandidateStatus.QUALIFIED,
        )
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        other = _make_registry_decision(
            candidate.candidate_id,
            build_observation(
                url="http://f2-other-policy.example.test/"
            ).observation_id,
            provider_record_id="location:" + "e" * 64,
            policy_id="legacy-policy",
            policy_version="v0",
        )
        spi.discovery.decisions_by_key[other.decision_key] = other
        spi.discovery.decisions_by_id[other.decision_id] = other
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(
            candidate.candidate_id
        )
        assert outcome.route is QualificationRoute.ALREADY_TERMINAL_OR_BUSY
        assert outcome.decision_id is None

    async def test_u19f2_17_race_retry_finds_historic_decision(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.support.discovery_fakes import MemQualificationRepository

        from darkula.app.persistence import ConflictError

        first = build_observation(
            url="http://f2-race.example.test/",
            record_id="location:" + "4" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        second = build_observation(
            url="http://f2-race.example.test/",
            record_id="location:" + "4" * 64,
            record_version="v2",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await _admit(spi, second, verified=True)

        async def _lose(
            self: MemQualificationRepository,
            decision: QualificationDecision,
            event: Any,
        ) -> Any:
            # Simulate a concurrent winner persisting D before our conflict.
            from dataclasses import replace as _replace

            self._state.decisions_by_key[decision.decision_key] = decision
            self._state.decisions_by_id[decision.decision_id] = decision
            self._state.candidate_status[decision.candidate_id] = decision.new_status
            stored = spi.candidates.candidates.get(decision.candidate_id)
            if stored is not None:
                spi.candidates.candidates[decision.candidate_id] = _replace(
                    stored, status=decision.new_status
                )
            del event
            raise ConflictError("simulated concurrent qualification winner")

        monkeypatch.setattr(MemQualificationRepository, "decide_and_transition", _lose)
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED
        assert outcome.replayed is True
        assert outcome.decision_id is not None
        assert len(self._decisions(spi)) == 1

    async def test_u19f2_18_race_with_two_decisions_fails_closed(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.support.discovery_fakes import MemQualificationRepository

        from darkula.app.persistence import AdmissionInvariantError

        candidate = build_candidate(
            entrypoint="http://f2-race-two.example.test/",
            status=CandidateStatus.QUALIFIED,
        )
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        two = (
            _make_registry_decision(
                candidate.candidate_id,
                build_observation(
                    url="http://f2-race-two.example.test/"
                ).observation_id,
                provider_record_id="location:" + "f" * 64,
            ),
            _make_registry_decision(
                candidate.candidate_id,
                build_observation(
                    url="http://f2-race-two.example.test/"
                ).observation_id,
                provider_record_id="location:" + "f" * 64,
                evaluated_at=T0 + timedelta(minutes=1),
            ),
        )
        for decision in two:
            spi.discovery.decisions_by_key[decision.decision_key] = decision
            spi.discovery.decisions_by_id[decision.decision_id] = decision

        async def _unused(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("decide_and_transition must not run")

        monkeypatch.setattr(
            MemQualificationRepository, "decide_and_transition", _unused
        )
        with pytest.raises(AdmissionInvariantError):
            await _qualification(spi, TRUSTED)._resolve_race(candidate.candidate_id)

    async def test_u19f2_19_replay_uses_only_bounded_lookup(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.support.admission_fakes import MemAdmissionRepository
        from tests.support.discovery_fakes import (
            MemQualificationRepository,
        )

        first = build_observation(
            url="http://f2-bound.example.test/",
            record_id="location:" + "5" * 64,
            observed_at=T0,
            ingested_at=T0,
        )
        candidate = await _admit(spi, first, verified=True)
        original = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)

        async def _forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("unbounded replay read must not be used")

        monkeypatch.setattr(MemAdmissionRepository, "claims_for_candidate", _forbidden)
        monkeypatch.setattr(
            MemQualificationRepository, "list_for_candidate", _forbidden
        )
        calls: list[SourceCandidateId] = []
        bounded = MemQualificationRepository.registry_v1_decisions_for_candidate

        async def _spy(
            self: MemQualificationRepository, candidate_id: SourceCandidateId
        ) -> tuple[QualificationDecision, ...]:
            calls.append(candidate_id)
            return await bounded(self, candidate_id)

        monkeypatch.setattr(
            MemQualificationRepository,
            "registry_v1_decisions_for_candidate",
            _spy,
        )
        replay = await _qualification(spi, TRUSTED).evaluate_and_apply(candidate)
        assert replay.decision_id == original.decision_id
        assert calls == [candidate]

    async def test_u19f2_20_fake_bounded_lookup_counts_and_order(
        self, spi: MemAdmissionSpi
    ) -> None:
        candidate = build_candidate(
            entrypoint="http://f2-counts.example.test/",
            status=CandidateStatus.QUALIFIED,
        )
        await seed_candidate(spi, candidate, build_candidate_event(candidate))
        decisions = [
            _make_registry_decision(
                candidate.candidate_id,
                build_observation(url="http://f2-counts.example.test/").observation_id,
                provider_record_id="location:" + "6" * 64,
                evaluated_at=T0 + timedelta(seconds=index),
            )
            for index in range(3)
        ]
        for decision in decisions:
            spi.discovery.decisions_by_key[decision.decision_key] = decision
            spi.discovery.decisions_by_id[decision.decision_id] = decision
        async with spi.unit_of_work() as uow:
            result = await uow.qualification.registry_v1_decisions_for_candidate(
                candidate.candidate_id
            )
        assert len(result) == 2
        ordered = sorted(
            decisions, key=lambda item: (item.evaluated_at, str(item.decision_id))
        )
        assert result == tuple(ordered[:2])
        assert all(
            decision.policy_id == QUALIFICATION_POLICY_ID
            and decision.policy_version == QUALIFICATION_POLICY_VERSION
            for decision in result
        )

    @staticmethod
    def _decisions(spi: MemAdmissionSpi) -> list[QualificationDecision]:
        return list(spi.discovery.decisions_by_key.values())
