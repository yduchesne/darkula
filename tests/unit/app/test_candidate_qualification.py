# SPDX-License-Identifier: AGPL-3.0-only
"""PR 19 trusted qualification routing unit matrix (U19-22 .. U19-33).

The policy and routing under test are production code. Persistence is the
composed in-memory admission fake; qualification decisions are written
through the same PR 17 atomic primitive contract.
"""

from __future__ import annotations

from datetime import timedelta
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
    QualificationMethod,
)
from darkula.domain.identifiers import SourceCandidateId
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


async def _admit(spi: MemAdmissionSpi, observation: Any) -> SourceCandidateId:
    await seed_observation(spi, observation)
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
        result = await _admit(spi, build_observation(url="http://good.example.test/"))
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
        assert manual_outcome.rule_code is QualificationRuleCode.PROVIDER_MISMATCH

        group = await _admit(
            spi,
            build_observation(
                url="http://group-record.example.test/",
                record_id="group:" + "b" * 64,
            ),
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
        first_result = await _admit(spi, first)
        second = build_observation(
            url="http://conflict.example.test/",
            record_id="location:" + "b" * 64,
            origin_reference="https://www.ransomlook.io/api/group/other",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await seed_observation(spi, second)
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
        first_result = await _admit(spi, first)
        second = build_observation(
            url="http://versioned.example.test/",
            record_id="location:" + "a" * 64,
            record_version="v2",
            observed_at=T0 + timedelta(minutes=1),
            ingested_at=T0 + timedelta(minutes=1),
        )
        await seed_observation(spi, second)
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
        )
        outcome = await _qualification(spi, TRUSTED).evaluate_and_apply(result)
        assert outcome.route is (QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE)
        assert outcome.rule_code is QualificationRuleCode.SOURCE_KIND_MISMATCH
        assert spi.discovery.decisions_by_key == {}

    async def test_u19_29_disabling_after_success_preserves_history(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = await _admit(spi, build_observation(url="http://kept.example.test/"))
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
        result = await _admit(spi, build_observation(url="http://later.example.test/"))
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
        result = await _admit(spi, build_observation(url="http://race.example.test/"))
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
        result = await _admit(spi, build_observation(url="http://replay.example.test/"))
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
