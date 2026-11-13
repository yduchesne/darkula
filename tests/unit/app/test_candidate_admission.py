# SPDX-License-Identifier: AGPL-3.0-only
"""PR 19 candidate admission unit matrix (U19-01 .. U19-21).

The behavior under test is production code
(:class:`~darkula.app.candidate_admission.CandidateAdmissionService` plus the
domain contracts). Persistence is the composed in-memory admission fake,
which shares commit/rollback semantics with the existing PR 10/17 fakes.
Real PostgreSQL concurrency and rollback proofs live in the integration
suite.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from tests.support.admission_fakes import (
    MemAdmissionRepository,
    MemAdmissionSpi,
    MemAdmissionUnitOfWork,
    build_candidate,
    build_candidate_event,
    build_observation,
    locator_digest,
    seed_candidate,
    seed_endpoint,
    seed_observation,
)

from darkula.app.candidate_admission import (
    AdmissionRaceError,
    CandidateAdmissionService,
)
from darkula.app.persistence import (
    AdmissionInvariantError,
    ConflictError,
    NotFoundError,
)
from darkula.domain.candidate_admission import AdmissionOutcome, AdmissionReason
from darkula.domain.discovery import (
    DiscoveryDetectionMethod,
    DiscoveryReferenceKind,
    DiscoveryValidationStatus,
)
from darkula.domain.identifiers import (
    CandidateEventId,
    SourceCandidateId,
    SourceEndpointId,
)
from darkula.domain.source import CandidateStatus, EndpointType

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _service(spi: MemAdmissionSpi) -> CandidateAdmissionService:
    return CandidateAdmissionService(persistence=spi, clock=lambda: T0)


def _cid(result: Any) -> SourceCandidateId:
    """Narrow an admitted result to its candidate identity (test helper)."""
    assert isinstance(result.candidate_id, SourceCandidateId)
    return result.candidate_id


@pytest_asyncio.fixture
async def spi() -> MemAdmissionSpi:
    return MemAdmissionSpi()


class _CancellingUnitOfWork(MemAdmissionUnitOfWork):
    """Unit of work that cancels immediately before any durable commit."""

    async def commit(self) -> None:
        raise asyncio.CancelledError


class _CancellingSpi(MemAdmissionSpi):
    def unit_of_work(self) -> MemAdmissionUnitOfWork:
        return _CancellingUnitOfWork(self)


@pytest.mark.asyncio
class TestAdmissionMatrix:
    async def test_u19_01_name_only_is_unresolved_without_candidate(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(
            url="SomeGroupName",
            reference_kind=DiscoveryReferenceKind.NAME_ONLY,
            record_id="group:" + "a" * 64,
        )
        await seed_observation(spi, observation)
        result = await _service(spi).admit_observation(observation.observation_id)
        assert result.outcome is AdmissionOutcome.UNRESOLVED_NAME
        assert result.reason is AdmissionReason.NAME_ONLY
        assert result.candidate_id is None
        assert spi.candidates.candidates == {}
        assert spi.candidates.history == {}
        assert spi.admission.ledger[observation.observation_id] == result

    async def test_u19_02_first_locator_creates_candidate_event_slot_ledger(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation()
        await seed_observation(spi, observation)
        before = spi.discovery.observations_by_id[observation.observation_id]

        result = await _service(spi).admit_observation(observation.observation_id)

        assert result.outcome is AdmissionOutcome.ADMITTED_NEW
        assert result.reason is AdmissionReason.NEW_LOCATOR
        assert _cid(result) is not None
        candidate = spi.candidates.candidates[_cid(result)]
        assert candidate.status is CandidateStatus.DISCOVERED
        events = spi.candidates.history[_cid(result)]
        assert len(events) == 1
        assert events[0].event_type.value == "DISCOVERED"
        assert spi.admission.slots[
            locator_digest(observation.normalized_locator or "")
        ][1] == (_cid(result))
        assert spi.admission.ledger[observation.observation_id] == result
        after = spi.discovery.observations_by_id[observation.observation_id]
        assert before == after
        assert after.candidate_id is None

    async def test_u19_03_exact_replay_returns_stored_original(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation()
        await seed_observation(spi, observation)
        service = _service(spi)
        first = await service.admit_observation(observation.observation_id)
        second = await service.admit_observation(observation.observation_id)

        assert second.replayed is True
        assert _cid(second) == _cid(first)
        assert second.outcome is first.outcome
        assert spi.candidates.history[_cid(first)] == [
            spi.candidates.history[_cid(first)][0]
        ]
        assert len(spi.admission.slots) == 1

    async def test_u19_04_new_observation_same_locator_reuses_candidate(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(record_id="location:" + "a" * 64)
        second = build_observation(record_id="location:" + "b" * 64)
        await seed_observation(spi, first)
        await seed_observation(spi, second)
        service = _service(spi)

        one = await service.admit_observation(first.observation_id)
        two = await service.admit_observation(second.observation_id)

        assert two.outcome is AdmissionOutcome.ADMITTED_EXISTING
        assert two.reason is AdmissionReason.EXISTING_LOCATOR_SLOT
        assert _cid(two) == _cid(one)
        assert len(spi.admission.ledger) == 2
        assert len(spi.candidates.history[_cid(one)]) == 1

    async def test_u19_05_equivalent_spellings_share_one_slot(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(url="HTTP://Example-Test.ONION:80/")
        second = build_observation(
            url="http://example-test.onion/", record_id="location:" + "b" * 64
        )
        assert first.normalized_locator == second.normalized_locator
        await seed_observation(spi, first)
        await seed_observation(spi, second)
        service = _service(spi)

        one = await service.admit_observation(first.observation_id)
        two = await service.admit_observation(second.observation_id)

        assert _cid(one) == _cid(two)
        assert len(spi.admission.slots) == 1
        assert len(spi.candidates.candidates) == 1

    async def test_u19_06_distinct_locators_same_origin_stay_distinct(
        self, spi: MemAdmissionSpi
    ) -> None:
        first = build_observation(
            url="http://one.example.test/", record_id="location:" + "a" * 64
        )
        second = build_observation(
            url="http://two.example.test/", record_id="location:" + "b" * 64
        )
        await seed_observation(spi, first)
        await seed_observation(spi, second)
        service = _service(spi)

        one = await service.admit_observation(first.observation_id)
        two = await service.admit_observation(second.observation_id)

        assert _cid(one) != _cid(two)
        assert len(spi.candidates.candidates) == 2
        suggestions = await service.possible_aliases(_cid(one))
        assert suggestions.too_many is False
        assert len(suggestions.items) == 1
        assert suggestions.items[0].right_candidate_id == _cid(two)
        # The suggestion changes nothing durable.
        assert len(spi.admission.slots) == 2
        assert len(spi.candidates.candidates) == 2

    async def test_u19_07_exact_managed_endpoint_is_evidence_only(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(url="http://known.example.test/")
        await seed_observation(spi, observation)
        endpoint_id = SourceEndpointId.generate()
        await seed_endpoint(spi, endpoint_id, observation.normalized_locator or "")

        result = await _service(spi).admit_observation(observation.observation_id)

        assert result.outcome is AdmissionOutcome.KNOWN_ENDPOINT
        assert result.reason is AdmissionReason.EXACT_MANAGED_ENDPOINT
        assert result.candidate_id is None
        assert result.matched_endpoint_ids == (endpoint_id,)
        assert spi.candidates.candidates == {}
        assert spi.admission.slots == {}

    async def test_u19_08_excess_endpoint_matches_are_review(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(url="http://crowded.example.test/")
        await seed_observation(spi, observation)
        for _ in range(33):
            await seed_endpoint(
                spi, SourceEndpointId.generate(), observation.normalized_locator or ""
            )

        result = await _service(spi).admit_observation(observation.observation_id)

        assert result.outcome is AdmissionOutcome.REVIEW_REQUIRED
        assert result.reason is AdmissionReason.EXCESS_ENDPOINT_MATCHES
        assert result.matched_endpoint_ids == ()
        assert result.candidate_id is None

    async def test_u19_09_single_legacy_candidate_is_reused(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(url="http://legacy.example.test/")
        legacy = build_candidate(entrypoint=observation.normalized_locator or "")
        await seed_candidate(spi, legacy, build_candidate_event(legacy))
        await seed_observation(spi, observation)

        result = await _service(spi).admit_observation(observation.observation_id)

        assert result.outcome is AdmissionOutcome.ADMITTED_EXISTING
        assert result.reason is AdmissionReason.EXISTING_LEGACY_CANDIDATE
        assert _cid(result) == legacy.candidate_id
        assert len(spi.candidates.history[legacy.candidate_id]) == 1

    async def test_u19_10_two_legacy_candidates_are_review(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(url="http://legacy.example.test/")
        for _suffix in ("a", "b"):
            legacy = build_candidate(entrypoint=observation.normalized_locator or "")
            await seed_candidate(spi, legacy, build_candidate_event(legacy))
        await seed_observation(spi, observation)

        result = await _service(spi).admit_observation(observation.observation_id)

        assert result.outcome is AdmissionOutcome.REVIEW_REQUIRED
        assert result.reason is AdmissionReason.MULTIPLE_LEGACY_CANDIDATES
        assert spi.admission.slots == {}
        assert len(spi.candidates.candidates) == 2

    async def test_u19_11_promoted_legacy_candidate_is_review(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(url="http://legacy.example.test/")
        legacy = build_candidate(
            entrypoint=observation.normalized_locator or "",
            status=CandidateStatus.PROMOTED,
        )
        await seed_candidate(spi, legacy, build_candidate_event(legacy))
        await seed_observation(spi, observation)

        result = await _service(spi).admit_observation(observation.observation_id)

        assert result.outcome is AdmissionOutcome.REVIEW_REQUIRED
        assert spi.admission.slots == {}
        assert spi.candidates.candidates[legacy.candidate_id].status is (
            CandidateStatus.PROMOTED
        )

    async def test_u19_12_invalid_persisted_state_fails_closed(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation()
        await seed_observation(spi, observation)
        malformed = replace(observation)  # valid object, mutated below
        object.__setattr__(
            malformed, "validation_status", DiscoveryValidationStatus.UNRESOLVED_NAME
        )
        spi.discovery.observations_by_id[observation.observation_id] = malformed

        with pytest.raises(AdmissionInvariantError):
            await _service(spi).admit_observation(observation.observation_id)
        assert spi.admission.ledger == {}
        assert spi.admission.slots == {}
        assert spi.candidates.candidates == {}

    async def test_u19_13_digest_collision_is_fatal_and_leaves_slot(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(url="http://collision.example.test/")
        await seed_observation(spi, observation)
        existing_candidate = SourceCandidateId.generate()
        key = locator_digest(observation.normalized_locator or "")
        spi.admission.slots[key] = (
            "http://different.example.test/",
            existing_candidate,
            observation.observation_id,
        )

        with pytest.raises(AdmissionInvariantError):
            await _service(spi).admit_observation(observation.observation_id)
        assert spi.admission.slots[key] == (
            "http://different.example.test/",
            existing_candidate,
            observation.observation_id,
        )
        assert spi.admission.ledger == {}

    async def test_u19_14_cancellation_persists_nothing(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation()
        await seed_observation(spi, observation)
        cancelling = _CancellingSpi()
        cancelling.discovery = spi.discovery
        cancelling.candidates = spi.candidates
        cancelling.admission = spi.admission

        with pytest.raises(asyncio.CancelledError):
            await _service(cancelling).admit_observation(observation.observation_id)

        assert spi.candidates.candidates == {}
        assert spi.admission.slots == {}
        assert spi.admission.ledger == {}

    async def test_u19_15_race_reuses_the_winner(self, spi: MemAdmissionSpi) -> None:
        observation = build_observation()
        await seed_observation(spi, observation)
        service = _service(spi)

        first = await service.admit_observation(observation.observation_id)
        async with spi.unit_of_work() as uow:
            second = await uow.admission.admit(
                observation.observation_id,
                SourceCandidateId.generate(),
                CandidateEventId.generate(),
                processed_at=T0,
            )
            await uow.commit()

        assert second.replayed is True
        assert _cid(second) == _cid(first)
        assert len(spi.candidates.candidates) == 1

    async def test_u19_16_auth_claim_records_pending_evidence(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(requires_auth_claim=True)
        await seed_observation(spi, observation)

        result = await _service(spi).admit_observation(observation.observation_id)

        evidence = await _access(spi, _cid(result))
        assert len(evidence) == 1
        assert evidence[0].status.value == "CREDENTIALS_PENDING"
        assert evidence[0].reason_code == "PROVIDER_AUTH_CLAIM"
        assert spi.candidates.candidates[_cid(result)].status is (
            CandidateStatus.DISCOVERED
        )

    async def test_u19_17_auth_evidence_replay_is_idempotent(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(requires_auth_claim=True)
        await seed_observation(spi, observation)
        service = _service(spi)

        first = await service.admit_observation(observation.observation_id)
        first_evidence = await _access(spi, _cid(first))
        second = await service.admit_observation(observation.observation_id)
        second_evidence = await _access(spi, _cid(second))

        assert len(second_evidence) == 1
        assert second_evidence[0].access_evidence_id == (
            first_evidence[0].access_evidence_id
        )
        assert second_evidence[0].recorded_at == first_evidence[0].recorded_at

    async def test_u19_18_auth_claim_known_endpoint_creates_no_candidate(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(
            url="http://known-auth.example.test/", requires_auth_claim=True
        )
        await seed_observation(spi, observation)
        await seed_endpoint(
            spi, SourceEndpointId.generate(), observation.normalized_locator or ""
        )

        result = await _service(spi).admit_observation(observation.observation_id)

        assert result.outcome is AdmissionOutcome.KNOWN_ENDPOINT
        assert spi.candidates.candidates == {}
        assert spi.discovery.access == {}
        stored = spi.discovery.observations_by_id[observation.observation_id]
        assert stored.requires_auth_claim is True

    async def test_u19_19_batch_limits_are_bounded(self, spi: MemAdmissionSpi) -> None:
        observations = [
            build_observation(
                url=f"http://batch-{index}.example.test/",
                record_id="location:" + f"{index:064x}",
            )
            for index in range(3)
        ]
        for observation in observations:
            await seed_observation(spi, observation)
        service = _service(spi)

        first = await service.process_pending(provider_id="ransomlook", limit=1)
        assert first.processed == 1
        assert first.admitted_new == 1
        assert len(first.observation_ids) == 1

        rest = await service.process_pending(provider_id="ransomlook", limit=50)
        assert rest.processed == 2
        assert rest.admitted_new == 2

        again = await service.process_pending(provider_id="ransomlook", limit=50)
        assert again.processed == 0

    async def test_u19_20_partial_batch_failure_preserves_prior_entries(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observations = [
            build_observation(
                url=f"http://partial-{index}.example.test/",
                record_id="location:" + f"{index:064x}",
                observed_at=T0 + timedelta(minutes=index),
                ingested_at=T0 + timedelta(minutes=index),
            )
            for index in range(3)
        ]
        for observation in observations:
            await seed_observation(spi, observation)
        failing = observations[1].observation_id
        original = MemAdmissionRepository.admit

        async def _fail_second(
            self: MemAdmissionRepository,
            observation_id: Any,
            candidate_id: Any,
            event_id: Any,
            *,
            processed_at: Any,
        ) -> Any:
            if observation_id == failing:
                raise ConflictError("injected admission persistence failure")
            return await original(
                self,
                observation_id,
                candidate_id,
                event_id,
                processed_at=processed_at,
            )

        monkeypatch.setattr(MemAdmissionRepository, "admit", _fail_second)

        result = await _service(spi).process_pending(provider_id="ransomlook", limit=50)

        assert result.stopped_by_error is True
        assert result.processed == 1
        assert observations[0].observation_id in spi.admission.ledger
        assert failing not in spi.admission.ledger
        assert observations[2].observation_id not in spi.admission.ledger

    async def test_u19_21_alias_overflow_reports_no_partial_list(
        self, spi: MemAdmissionSpi
    ) -> None:
        target = build_observation(
            url="http://alias-target.example.test/",
            record_id="location:" + "f" * 64,
        )
        await seed_observation(spi, target)
        service = _service(spi)
        target_result = await service.admit_observation(target.observation_id)

        for index in range(17):
            observation = build_observation(
                url=f"http://alias-mirror-{index}.example.test/",
                record_id="location:" + f"{index:064x}",
            )
            await seed_observation(spi, observation)
            await service.admit_observation(observation.observation_id)

        suggestions = await service.possible_aliases(_cid(target_result))
        assert suggestions.too_many is True
        assert suggestions.items == ()


async def _access(
    spi: MemAdmissionSpi, candidate_id: SourceCandidateId
) -> tuple[Any, ...]:
    async with spi.unit_of_work() as uow:
        evidence = await uow.access.list_for_candidate(candidate_id)
        await uow.commit()
        return evidence


@pytest.mark.asyncio
class TestAdmissionServiceFailures:
    async def test_unknown_observation_is_not_found(self, spi: MemAdmissionSpi) -> None:
        from darkula.domain.identifiers import DiscoveryObservationId

        with pytest.raises(NotFoundError):
            await _service(spi).admit_observation(DiscoveryObservationId.generate())

    async def test_second_race_raises_bounded_error(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observation = build_observation()
        await seed_observation(spi, observation)
        service = _service(spi)
        await service.admit_observation(observation.observation_id)

        async def _always_conflict(*args: Any, **kwargs: Any) -> Any:
            raise ConflictError("persistent race")

        monkeypatch.setattr(
            "darkula.app.candidate_admission.CandidateAdmissionService._admit_once",
            _always_conflict,
        )
        with pytest.raises(AdmissionRaceError):
            await service.admit_observation(observation.observation_id)


def _unused() -> None:
    """Keep imports meaningful for type checkers (fixtures use them)."""
    _ = (
        DiscoveryDetectionMethod,
        EndpointType,
        MemAdmissionUnitOfWork,
    )


# ---------------------------------------------------------------------------
# PR 19-F1 — approved-ingestion marker semantics (U19F-07 .. U19F-11).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVerifiedMarkerRepository:
    async def test_u19f_07_marker_requires_existing_observation(
        self, spi: MemAdmissionSpi
    ) -> None:
        from darkula.domain.identifiers import DiscoveryObservationId

        with pytest.raises(NotFoundError):
            async with spi.unit_of_work() as uow:
                await uow.admission.record_verified_registry_observation(
                    DiscoveryObservationId.generate(), verified_at=T0
                )

    async def test_u19f_08_marker_rejects_wrong_provider_or_class(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation(
            provider_id="internal",
            record_id="manual:1",
            detection_method=DiscoveryDetectionMethod.MANUAL,
        )
        await seed_observation(spi, observation)
        with pytest.raises(AdmissionInvariantError):
            async with spi.unit_of_work() as uow:
                await uow.admission.record_verified_registry_observation(
                    observation.observation_id, verified_at=T0
                )
        assert spi.admission.verified == {}

    async def test_u19f_09_marker_replay_retains_first_timestamp(
        self, spi: MemAdmissionSpi
    ) -> None:
        observation = build_observation()
        await seed_observation(spi, observation)
        async with spi.unit_of_work() as uow:
            await uow.admission.record_verified_registry_observation(
                observation.observation_id, verified_at=T0
            )
            await uow.commit()
        later = T0 + timedelta(hours=1)
        async with spi.unit_of_work() as uow:
            await uow.admission.record_verified_registry_observation(
                observation.observation_id, verified_at=later
            )
            await uow.commit()
        assert spi.admission.verified == {observation.observation_id: T0}
        assert len(spi.discovery.observations_by_id) == 1

    async def test_u19f_10_marker_failure_leaves_no_observation(
        self, spi: MemAdmissionSpi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from darkula.app.source_discovery import SourceDiscoveryService
        from darkula.domain.discovery import DiscoveryInput

        async def _fail(*args: Any, **kwargs: Any) -> None:
            raise AdmissionInvariantError("injected marker failure")

        monkeypatch.setattr(
            MemAdmissionRepository,
            "record_verified_registry_observation",
            _fail,
        )
        service = SourceDiscoveryService(persistence=spi, clock=lambda: T0)
        claim = DiscoveryInput(
            provider_id="ransomlook",
            provider_record_id="location:" + "a" * 64,
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
            asserted_reference="http://atomic.onion/",
            observed_at=T0,
            evidence_reference="registry://ransomlook/location/a",
            origin_reference="https://www.ransomlook.io/api/group/x",
        )
        with pytest.raises(AdmissionInvariantError):
            await service._ingest_verified_registry_claim(claim)
        assert spi.discovery.observations_by_id == {}
        assert spi.admission.verified == {}

    async def test_u19f_11_cancellation_commits_nothing(
        self, spi: MemAdmissionSpi
    ) -> None:
        from darkula.app.source_discovery import SourceDiscoveryService
        from darkula.domain.discovery import DiscoveryInput

        cancelling = _CancellingSpi()
        cancelling.discovery = spi.discovery
        cancelling.candidates = spi.candidates
        cancelling.admission = spi.admission
        service = SourceDiscoveryService(persistence=cancelling, clock=lambda: T0)
        claim = DiscoveryInput(
            provider_id="ransomlook",
            provider_record_id="location:" + "a" * 64,
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
            asserted_reference="http://cancelled.onion/",
            observed_at=T0,
            evidence_reference="registry://ransomlook/location/a",
            origin_reference="https://www.ransomlook.io/api/group/x",
        )
        with pytest.raises(asyncio.CancelledError):
            await service._ingest_verified_registry_claim(claim)
        assert spi.discovery.observations_by_id == {}
        assert spi.admission.verified == {}
