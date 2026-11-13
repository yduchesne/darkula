# SPDX-License-Identifier: AGPL-3.0-only
"""PR 19 real-PostgreSQL candidate admission vertical slices (I19-01 .. I19-16).

The real ``PostgresDarkulaSpi``, PR 17 ingest, PR 19 repositories/services,
versioned stored functions, PR 10 ``ReconCoordinator`` and PostgreSQL all run
unchanged. Only the external registry HTTP transport and the LLM/crawler
boundaries are faked. Direct SQL appears only in setup/verification helpers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from darkula.app.candidate_admission import CandidateAdmissionService
from darkula.app.candidate_qualification import (
    CandidateQualificationService,
    QualificationRoute,
)
from darkula.app.recon import (
    ReconAction,
    ReconAgentDecision,
    ReconCompletion,
    ReconCoordinator,
    ReconExecutionError,
    ReconInspection,
)
from darkula.app.registry_polling import RegistryPoller
from darkula.app.source_discovery import SourceDiscoveryService
from darkula.config.settings import (
    DatabaseSettings,
    QualificationSettings,
    ReconSettings,
)
from darkula.domain.candidate_admission import AdmissionOutcome, AdmissionResult
from darkula.domain.discovery import (
    AccessEvidence,
    AccessStatus,
    DiscoveryDetectionMethod,
    DiscoveryInput,
    DiscoveryObservation,
    DiscoveryReferenceKind,
)
from darkula.domain.identifiers import (
    AccessEvidenceId,
    CandidateEventId,
    SourceCandidateId,
    SourceEndpointId,
    SourceId,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    EndpointStatus,
    EndpointType,
    ReconDisposition,
    Source,
    SourceCandidate,
    SourceCandidateEventHistory,
    SourceEndpoint,
    SourceStatus,
)
from darkula.infrastructure.discovery.ransomlook import RansomLookDiscoveryAdapter
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_llm import FakeLlmClient
from darkula.testing.fake_world.source_registry import FakeSourceRegistry
from tests.integration.conftest import _raw_connect
from tests.support.collection_fakes import FakeCrawler

pytestmark = pytest.mark.integration

BASE_TIME = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
ORIGIN = "https://www.ransomlook.io/api/group/example"
FORBIDDEN_AFTER_ADMISSION = (
    "source",
    "source_endpoint",
    "collection_policy",
    "collection_run",
    "message_outbox",
    "recon_assessment",
)


def _count(database_settings: DatabaseSettings, table: str) -> int:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _scalar(
    database_settings: DatabaseSettings, sql: str, params: tuple[Any, ...] = ()
) -> Any:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0]


def _observation_snapshot(
    database_settings: DatabaseSettings,
) -> list[tuple[Any, ...]]:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT observation_id, reference_kind, normalized_locator, "
                "validation_status, detected_at FROM ("
                " SELECT observation_id, reference_kind, normalized_locator, "
                "        validation_status, observed_at AS detected_at "
                "   FROM discovery_observation) o "
                "ORDER BY observation_id"
            )
            return [tuple(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _locator_input(
    *,
    record: str = "a",
    url: str = "http://admission.onion/",
    version: str = "v1",
    requires_auth_claim: bool | None = None,
    observed_at: datetime = BASE_TIME,
) -> DiscoveryInput:
    return DiscoveryInput(
        provider_id="ransomlook",
        provider_record_id="location:" + record * 64,
        provider_record_version=version,
        detection_method=DiscoveryDetectionMethod.REGISTRY,
        reference_kind=DiscoveryReferenceKind.LOCATOR,
        asserted_reference=url,
        observed_at=observed_at,
        evidence_reference="registry://ransomlook/location/" + record,
        source_kind_hint=EndpointType.ONION,
        requires_auth_claim=requires_auth_claim,
        origin_reference=ORIGIN,
    )


async def _ingest(
    spi: PostgresDarkulaSpi, source: DiscoveryInput
) -> DiscoveryObservation:
    return await SourceDiscoveryService(
        persistence=spi, clock=lambda: BASE_TIME
    ).ingest(source)


async def _admit(spi: PostgresDarkulaSpi, observation_id: Any) -> AdmissionResult:
    return await CandidateAdmissionService(
        persistence=spi, clock=lambda: BASE_TIME
    ).admit_observation(observation_id)


async def _mark_verified(spi: PostgresDarkulaSpi, observation_id: Any) -> None:
    """Test-only precondition: record the approved-ingestion marker."""
    async with spi.unit_of_work() as uow:
        await uow.admission.record_verified_registry_observation(
            observation_id, verified_at=BASE_TIME
        )
        await uow.commit()


async def _admitted_candidate(
    database_settings: DatabaseSettings, observation_id: Any
) -> SourceCandidateId:
    return SourceCandidateId.from_str(
        str(
            _scalar(
                database_settings,
                "SELECT candidate_id FROM discovery_candidate_admission "
                "WHERE observation_id = %s",
                (str(observation_id),),
            )
        )
    )


@pytest_asyncio.fixture
async def second_spi(
    database_settings: DatabaseSettings,
) -> AsyncIterator[PostgresDarkulaSpi]:
    instance = PostgresDarkulaSpi.from_settings(database_settings)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()


class TestAdmissionSchema:
    """I19-01: additive migration 0011 and its function surface."""

    def test_schema_head_and_functions(
        self, database_settings: DatabaseSettings
    ) -> None:
        assert _scalar(
            database_settings,
            "SELECT version FROM darkula_schema_migrations "
            "ORDER BY version DESC LIMIT 1",
        ).startswith("0012_candidate_qualification_fixup.sql")
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT proname FROM pg_proc "
                    "WHERE pronamespace = 'public'::regnamespace"
                )
                functions = {row[0] for row in cur.fetchall()}
                cur.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
                tables = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()
        assert {
            "discovery_admission_pending_v1",
            "discovery_admission_get_v1",
            "discovery_admission_endpoints_v1",
            "discovery_admission_claims_for_candidate_v1",
            "discovery_possible_aliases_v1",
            "discovery_admit_observation_v1",
        } <= functions
        assert {
            "candidate_locator_slot",
            "discovery_candidate_admission",
            "discovery_endpoint_match",
        } <= tables
        for legacy in ("0009_discovery_foundation.sql", "0010_registry_polling.sql"):
            assert (
                _scalar(
                    database_settings,
                    "SELECT count(*) FROM darkula_schema_migrations WHERE version = %s",
                    (legacy,),
                )
                == 1
            )


class TestAdmissionVerticalSlice:
    """I19-02..I19-07: real ingest, admission, ledger, rollback."""

    @pytest.mark.asyncio
    async def test_i19_02_registry_ingest_then_admission(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        registry = FakeSourceRegistry(index=("blackgate",))
        adapter = RansomLookDiscoveryAdapter(
            transport=registry, request_interval_seconds=0, sleep=_noop
        )
        poller = RegistryPoller(
            persistence=spi,
            discovery_service=SourceDiscoveryService(
                persistence=spi, clock=lambda: BASE_TIME
            ),
            clock=lambda: BASE_TIME,
        )
        await poller.poll_once(adapter)
        assert _count(database_settings, "source_candidate") == 0

        service = CandidateAdmissionService(persistence=spi, clock=lambda: BASE_TIME)
        batch = await service.process_pending(provider_id="ransomlook", limit=50)

        assert batch.processed == 3
        assert batch.admitted_new == 1
        assert batch.unresolved_name == 2
        assert _count(database_settings, "source_candidate") == 1
        assert _count(database_settings, "source_candidate_event_history") == 1
        assert _count(database_settings, "candidate_locator_slot") == 1
        assert _count(database_settings, "discovery_candidate_admission") == 3
        # Observations remain immutable (candidate_id is never linked).
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM discovery_observation "
                "WHERE candidate_id IS NOT NULL",
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_i19_03_replay_and_new_version_share_one_candidate(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        first = await _ingest(spi, _locator_input(record="a"))
        one = await _admit(spi, first.observation_id)
        replay = await _admit(spi, first.observation_id)
        second = await _ingest(
            spi, _locator_input(record="a", version="v2", observed_at=BASE_TIME)
        )
        two = await _admit(spi, second.observation_id)

        assert replay.replayed is True
        assert one.candidate_id == replay.candidate_id == two.candidate_id
        assert _count(database_settings, "source_candidate") == 1
        assert _count(database_settings, "source_candidate_event_history") == 1
        assert _count(database_settings, "candidate_locator_slot") == 1
        assert _count(database_settings, "discovery_candidate_admission") == 2

    @pytest.mark.asyncio
    async def test_i19_04_concurrent_admission_yields_one_candidate(
        self,
        spi: PostgresDarkulaSpi,
        second_spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
    ) -> None:
        first = await _ingest(spi, _locator_input(record="a"))
        second = await _ingest(spi, _locator_input(record="b"))

        results = await asyncio.gather(
            _admit(spi, first.observation_id),
            _admit(second_spi, second.observation_id),
        )

        assert results[0].candidate_id == results[1].candidate_id
        assert _count(database_settings, "source_candidate") == 1
        assert _count(database_settings, "source_candidate_event_history") == 1
        assert _count(database_settings, "candidate_locator_slot") == 1
        assert _count(database_settings, "discovery_candidate_admission") == 2
        assert (
            _scalar(
                database_settings,
                "SELECT count(DISTINCT candidate_id) "
                "FROM discovery_candidate_admission",
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_i19_05_known_endpoint_has_no_source_side_effects(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        async with spi.unit_of_work() as uow:
            source = Source(
                source_id=SourceId.generate(),
                status=SourceStatus.ACTIVE,
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
                name="seeded",
            )
            await uow.sources.create(source)
            endpoint = SourceEndpoint(
                endpoint_id=SourceEndpointId.generate(),
                source_id=source.source_id,
                uri="http://known.onion/",
                endpoint_type=EndpointType.ONION,
                status=EndpointStatus.ACTIVE,
                first_observed_at=BASE_TIME,
                last_observed_at=BASE_TIME,
            )
            await uow.sources.add_endpoint(endpoint)
            await uow.commit()
        sources_before = _count(database_settings, "source")
        endpoints_before = _count(database_settings, "source_endpoint")

        observation = await _ingest(
            spi, _locator_input(record="a", url="http://known.onion/")
        )
        result = await _admit(spi, observation.observation_id)

        assert result.outcome is AdmissionOutcome.KNOWN_ENDPOINT
        assert result.matched_endpoint_ids == (endpoint.endpoint_id,)
        assert result.candidate_id is None
        assert _count(database_settings, "source_candidate") == 0
        assert _count(database_settings, "source") == sources_before
        assert _count(database_settings, "source_endpoint") == endpoints_before
        assert _count(database_settings, "discovery_endpoint_match") == 1

    @pytest.mark.asyncio
    async def test_i19_06_duplicate_legacy_candidates_are_review(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        for _ in range(2):
            candidate = SourceCandidate(
                candidate_id=SourceCandidateId.generate(),
                discovered_at=BASE_TIME,
                discovery_method="seed",
                entrypoint="http://legacy-duplicate.onion/",
                status=CandidateStatus.DISCOVERED,
            )
            async with spi.unit_of_work() as uow:
                await uow.source_candidates.create(
                    candidate,
                    event=SourceCandidateEventHistory(
                        event_id=CandidateEventId.generate(),
                        candidate_id=candidate.candidate_id,
                        event_type=CandidateEventType.DISCOVERED,
                        occurred_at=BASE_TIME,
                    ),
                )
                await uow.commit()
        observation = await _ingest(
            spi, _locator_input(record="a", url="http://legacy-duplicate.onion/")
        )
        result = await _admit(spi, observation.observation_id)

        assert result.outcome is AdmissionOutcome.REVIEW_REQUIRED
        assert _count(database_settings, "source_candidate") == 2
        assert _count(database_settings, "candidate_locator_slot") == 0
        assert _count(database_settings, "discovery_candidate_admission") == 1

    @pytest.mark.asyncio
    async def test_i19_07_auth_claim_and_rollback_are_atomic(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        observation = await _ingest(
            spi, _locator_input(record="a", requires_auth_claim=True)
        )
        result = await _admit(spi, observation.observation_id)
        assert result.candidate_id is not None
        assert _count(database_settings, "source_candidate") == 1
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM access_evidence "
                "WHERE status = 'CREDENTIALS_PENDING' "
                "AND reason_code = 'PROVIDER_AUTH_CLAIM'",
            )
            == 1
        )

        # Rolling back the admission unit of work removes candidate, event,
        # slot, ledger row and access evidence together.
        second = await _ingest(spi, _locator_input(record="b"))
        try:
            async with spi.unit_of_work() as uow:
                stored = await uow.discovery.get(second.observation_id)
                assert stored is not None
                staged = await uow.admission.admit(
                    second.observation_id,
                    SourceCandidateId.generate(),
                    CandidateEventId.generate(),
                    processed_at=BASE_TIME,
                )
                assert staged.candidate_id is not None
                await uow.access.append_evidence(
                    AccessEvidence(
                        access_evidence_id=AccessEvidenceId.generate(),
                        observation_id=second.observation_id,
                        candidate_id=staged.candidate_id,
                        source_endpoint_id=None,
                        status=AccessStatus.CREDENTIALS_PENDING,
                        reason_code="PROVIDER_AUTH_CLAIM",
                        recorded_at=BASE_TIME,
                        evidence_reference=stored.evidence_reference,
                    )
                )
                raise RuntimeError("injected rollback")
        except RuntimeError:
            pass
        assert _count(database_settings, "source_candidate") == 1
        assert _count(database_settings, "discovery_candidate_admission") == 1
        assert _count(database_settings, "candidate_locator_slot") == 1


class TestQualificationVerticalSlice:
    """I19-08..I19-10: default-deny trust, real atomic qualification."""

    @pytest.mark.asyncio
    async def test_i19_08_trust_off_then_on_qualifies_once(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        observation = await _ingest(spi, _locator_input(record="a"))
        await _mark_verified(spi, observation.observation_id)
        admitted = await _admit(spi, observation.observation_id)
        candidate_id = admitted.candidate_id
        assert candidate_id is not None

        off = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(),
            clock=lambda: BASE_TIME,
        ).evaluate_and_apply(candidate_id)
        assert off.route is QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE
        assert _count(database_settings, "qualification_decision") == 0
        assert (
            _scalar(
                database_settings,
                "SELECT status FROM source_candidate WHERE id = %s",
                (str(candidate_id),),
            )
            == "RECONNAISSANCE_PENDING"
        )

        on = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        ).evaluate_and_apply(candidate_id)
        assert on.route is QualificationRoute.REGISTRY_QUALIFIED
        assert _count(database_settings, "qualification_decision") == 1
        assert (
            _scalar(
                database_settings,
                "SELECT status FROM source_candidate WHERE id = %s",
                (str(candidate_id),),
            )
            == "QUALIFIED"
        )
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM source_candidate_event_history "
                "WHERE event_type = 'QUALIFIED' AND candidate_id = %s",
                (str(candidate_id),),
            )
            == 1
        )
        assert _count(database_settings, "recon_assessment") == 0

    @pytest.mark.asyncio
    async def test_i19_09_conflicting_provenance_never_qualifies(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        first = await _ingest(spi, _locator_input(record="a"))
        await _mark_verified(spi, first.observation_id)
        one = await _admit(spi, first.observation_id)
        second = await _ingest(
            spi,
            DiscoveryInput(
                provider_id="ransomlook",
                provider_record_id="location:" + "b" * 64,
                provider_record_version="v1",
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=DiscoveryReferenceKind.LOCATOR,
                asserted_reference="http://admission.onion/",
                observed_at=BASE_TIME + timedelta(minutes=1),
                evidence_reference="registry://ransomlook/location/b",
                source_kind_hint=EndpointType.ONION,
                origin_reference="https://www.ransomlook.io/api/group/other",
            ),
        )
        await _mark_verified(spi, second.observation_id)
        await _admit(spi, second.observation_id)
        assert one.candidate_id is not None

        outcome = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        ).evaluate_and_apply(one.candidate_id)
        assert outcome.route is QualificationRoute.PENDING_RECON_AMBIGUOUS
        assert _count(database_settings, "qualification_decision") == 0
        assert _count(database_settings, "source") == 0

    @pytest.mark.asyncio
    async def test_i19_10_concurrent_qualification_one_winner(
        self,
        spi: PostgresDarkulaSpi,
        second_spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
    ) -> None:
        observation = await _ingest(spi, _locator_input(record="a"))
        await _mark_verified(spi, observation.observation_id)
        admitted = await _admit(spi, observation.observation_id)
        candidate_id = admitted.candidate_id
        assert candidate_id is not None
        trusted = QualificationSettings(ransomlook_location_trusted=True)

        first, second = await asyncio.gather(
            CandidateQualificationService(
                persistence=spi, settings=trusted, clock=lambda: BASE_TIME
            ).evaluate_and_apply(candidate_id),
            CandidateQualificationService(
                persistence=second_spi, settings=trusted, clock=lambda: BASE_TIME
            ).evaluate_and_apply(candidate_id),
        )
        assert _count(database_settings, "qualification_decision") == 1
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM source_candidate_event_history "
                "WHERE event_type = 'QUALIFIED' AND candidate_id = %s",
                (str(candidate_id),),
            )
            == 1
        )
        assert {first.route, second.route} <= {
            QualificationRoute.REGISTRY_QUALIFIED,
            QualificationRoute.CHECKPOINT_OR_STATE_RACE,
            QualificationRoute.ALREADY_TERMINAL_OR_BUSY,
        }


def _recon_settings() -> ReconSettings:
    return ReconSettings()


def _agent_decision(disposition: ReconDisposition) -> ReconAgentDecision:
    return ReconAgentDecision(
        action=ReconAction.COMPLETE,
        completion=ReconCompletion(
            disposition=disposition,
            confidence=0.9,
            evidence_references=[],
            characteristics={},
        ),
    )


def _inspect_decision() -> ReconAgentDecision:
    return ReconAgentDecision(
        action=ReconAction.INSPECT,
        inspection=ReconInspection(target="/", purpose="inspect"),
    )


class _StatusFlipCrawler(FakeCrawler):
    """Fake crawler that flips the candidate out of UNDER_RECONNAISSANCE."""

    def __init__(self, settings: DatabaseSettings, candidate_id: SourceCandidateId):
        super().__init__()
        self._settings = settings
        self._candidate_id = candidate_id

    async def crawl(self, request: Any) -> Any:
        conn = _raw_connect(self._settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE source_candidate SET status = 'QUALIFIED' WHERE id = %s",
                    (str(self._candidate_id),),
                )
            conn.commit()
        finally:
            conn.close()
        return await super().crawl(request)


async def _admit_ransomlook_observation(spi: PostgresDarkulaSpi) -> SourceCandidateId:
    observation = await _ingest(
        spi, _locator_input(record="a", url="http://recon-admission.onion/")
    )
    admitted = await _admit(spi, observation.observation_id)
    assert admitted.candidate_id is not None
    return admitted.candidate_id


class TestAgentReconVerticalSlice:
    """I19-11..I19-13: real coordinator with explicit admission provenance."""

    @pytest.mark.asyncio
    async def test_i19_11_authorized_recon_commits_assessment_and_decision(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        candidate_id = await _admit_ransomlook_observation(spi)
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_agent_decision(ReconDisposition.QUALIFY))
        coordinator = ReconCoordinator(
            spi=spi,
            agent=self._agent(llm),
            crawler=FakeCrawler(),
            settings=_recon_settings(),
            clock=lambda: BASE_TIME,
        )
        outcome = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        ).dispatch_recon(candidate_id, coordinator=coordinator, authorized=True)

        assert outcome.dispatched is True
        assert outcome.workflow is not None
        async with spi.unit_of_work() as uow:
            assessments = await uow.source_candidates.list_recon_assessments(
                candidate_id
            )
            decisions = await uow.qualification.list_for_candidate(candidate_id)
            history = await uow.source_candidates.list_history(candidate_id)
        assert len(assessments) == 1
        assert len(decisions) == 1
        assert decisions[0].method.value == "AGENT_RECON"
        assert decisions[0].recon_assessment_id == assessments[0].assessment_id
        assert [event.event_type for event in history].count(
            CandidateEventType.QUALIFIED
        ) == 1

    @pytest.mark.asyncio
    async def test_i19_12_final_conflict_rolls_back_assessment_and_decision(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        candidate_id = await _admit_ransomlook_observation(spi)
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_inspect_decision())
        llm.enqueue(_agent_decision(ReconDisposition.QUALIFY))
        crawler = _StatusFlipCrawler(database_settings, candidate_id)
        coordinator = ReconCoordinator(
            spi=spi,
            agent=self._agent(llm),
            crawler=crawler,
            settings=_recon_settings(),
            clock=lambda: BASE_TIME,
        )
        with pytest.raises(ReconExecutionError):
            await CandidateQualificationService(
                persistence=spi,
                settings=QualificationSettings(ransomlook_location_trusted=True),
                clock=lambda: BASE_TIME,
            ).dispatch_recon(candidate_id, coordinator=coordinator, authorized=True)

        assert _count(database_settings, "recon_assessment") == 0
        assert _count(database_settings, "qualification_decision") == 0
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM source_candidate_event_history "
                "WHERE event_type = 'QUALIFIED' AND candidate_id = %s",
                (str(candidate_id),),
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_i19_13_cancellation_commits_nothing(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        observation = await _ingest(spi, _locator_input(record="a"))
        try:
            async with spi.unit_of_work() as uow:
                await uow.admission.admit(
                    observation.observation_id,
                    SourceCandidateId.generate(),
                    CandidateEventId.generate(),
                    processed_at=BASE_TIME,
                )
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            pass
        assert _count(database_settings, "source_candidate") == 0
        assert _count(database_settings, "discovery_candidate_admission") == 0
        assert _count(database_settings, "candidate_locator_slot") == 0

    @staticmethod
    def _agent(llm: FakeLlmClient) -> Any:
        from darkula.app.recon_agent import ReconAgent
        from darkula.infrastructure.observability import NoOpAgentObservability

        return ReconAgent(
            llm=llm,
            observability=NoOpAgentObservability(),
            settings=_recon_settings(),
        )


class TestPersistenceAndAudit:
    """I19-14..I19-16: restart replay, forbidden effects, PR18 isolation."""

    @pytest.mark.asyncio
    async def test_i19_14_restart_preserves_and_replays(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
    ) -> None:
        observation = await _ingest(spi, _locator_input(record="a"))
        first = await _admit(spi, observation.observation_id)
        assert first.candidate_id is not None

        restarted = PostgresDarkulaSpi.from_settings(database_settings)
        await restarted.start()
        try:
            replay = await _admit(restarted, observation.observation_id)
            async with restarted.unit_of_work() as uow:
                claims = await uow.admission.claims_for_candidate(
                    first.candidate_id, limit=65
                )
                await uow.commit()
        finally:
            await restarted.close()

        assert replay.replayed is True
        assert replay.candidate_id == first.candidate_id
        assert len(claims) == 1

    @pytest.mark.asyncio
    async def test_i19_15_no_forbidden_managed_effects(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        await _admit_ransomlook_observation(spi)
        for table in FORBIDDEN_AFTER_ADMISSION:
            assert _count(database_settings, table) == 0

    @pytest.mark.asyncio
    async def test_i19_16_registry_only_still_creates_no_candidates(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        registry = FakeSourceRegistry(index=("blackgate",))
        adapter = RansomLookDiscoveryAdapter(
            transport=registry, request_interval_seconds=0, sleep=_noop
        )
        poller = RegistryPoller(
            persistence=spi,
            discovery_service=SourceDiscoveryService(
                persistence=spi, clock=lambda: BASE_TIME
            ),
            clock=lambda: BASE_TIME,
        )
        await poller.poll_once(adapter)
        assert _count(database_settings, "source_candidate") == 0
        assert _count(database_settings, "discovery_candidate_admission") == 0
        assert _count(database_settings, "candidate_locator_slot") == 0
        assert _count(database_settings, "qualification_decision") == 0


async def _noop(seconds: float) -> None:
    del seconds


# ---------------------------------------------------------------------------
# PR 19-F1 — trusted provenance, current endpoint guard, replay, bounded access.
# ---------------------------------------------------------------------------


_APPROVED_INDEX = b'["blackgate"]'


def _approved_detail() -> bytes:
    import json

    return json.dumps(
        [
            {
                "name": "blackgate",
                "locations": [{"slug": "http://approved-intg.onion/"}],
            },
            [],
        ]
    ).encode("utf-8")


def _patch_approved_http(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from darkula.infrastructure.discovery import http as http_module
    from darkula.infrastructure.discovery.http import RegistryHttpResponse

    seen: list[str] = []

    async def _fake_get(
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        transport: Any = None,
    ) -> RegistryHttpResponse:
        del timeout_seconds, max_response_bytes, transport
        assert url.startswith("https://www.ransomlook.io:443/")
        seen.append(url)
        path = url.removeprefix("https://www.ransomlook.io:443")
        if path == "/api/groups":
            return RegistryHttpResponse(status_code=200, body=_APPROVED_INDEX)
        if path == "/api/group/blackgate":
            return RegistryHttpResponse(status_code=200, body=_approved_detail())
        raise AssertionError("unexpected approved URL")

    monkeypatch.setattr(http_module, "_async_get_url", _fake_get)
    return seen


def _approved_adapter() -> RansomLookDiscoveryAdapter:
    from darkula.infrastructure.discovery.http import StdlibRegistryHttpTransport

    return RansomLookDiscoveryAdapter(
        transport=StdlibRegistryHttpTransport(),
        request_interval_seconds=0,
        sleep=_noop,
    )


async def _seed_candidate_db(
    spi: PostgresDarkulaSpi, entrypoint: str
) -> SourceCandidateId:
    candidate = SourceCandidate(
        candidate_id=SourceCandidateId.generate(),
        discovered_at=BASE_TIME,
        discovery_method="seed",
        entrypoint=entrypoint,
        status=CandidateStatus.DISCOVERED,
    )
    async with spi.unit_of_work() as uow:
        await uow.source_candidates.create(
            candidate,
            event=SourceCandidateEventHistory(
                event_id=CandidateEventId.generate(),
                candidate_id=candidate.candidate_id,
                event_type=CandidateEventType.DISCOVERED,
                occurred_at=BASE_TIME,
            ),
        )
        await uow.commit()
    return candidate.candidate_id


def _seed_pending_access(
    database_settings: DatabaseSettings, candidate_id: SourceCandidateId, count: int
) -> None:
    """Test-only fixture: insert ``count`` distinct pending evidence rows."""
    import uuid as uuid_module

    from darkula.domain.discovery import (
        DiscoveryDetectionMethod,
        DiscoveryReferenceKind,
        discovery_semantic_key,
    )

    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            for index in range(count):
                observation_id = uuid_module.uuid4()
                record_id = f"bench-{candidate_id}-{index}"
                semantic_key = discovery_semantic_key(
                    provider_id="ransomlook",
                    provider_record_id=record_id,
                    provider_record_version="v1",
                    detection_method=DiscoveryDetectionMethod.REGISTRY,
                    reference_kind=DiscoveryReferenceKind.NAME_ONLY,
                )
                cur.execute(
                    "INSERT INTO discovery_observation ("
                    "observation_id, provider_id, provider_record_id, "
                    "provider_record_version, detection_method, reference_kind, "
                    "asserted_reference, normalized_locator, validation_status, "
                    "source_kind_hint, requires_auth_claim, origin_reference, "
                    "evidence_reference, evidence_excerpt, observed_at, "
                    "ingested_at, candidate_id, semantic_key) VALUES ("
                    "%s,'ransomlook',%s,'v1','REGISTRY','NAME_ONLY',%s,NULL,"
                    "'UNRESOLVED_NAME',NULL,TRUE,NULL,%s,NULL,%s,%s,NULL,%s)",
                    (
                        str(observation_id),
                        record_id,
                        record_id,
                        "registry://ransomlook/bench/" + record_id,
                        BASE_TIME,
                        BASE_TIME,
                        semantic_key,
                    ),
                )
                cur.execute(
                    "INSERT INTO access_evidence ("
                    "access_evidence_id, observation_id, candidate_id, "
                    "source_endpoint_id, status, reason_code, recorded_at, "
                    "evidence_reference) VALUES ("
                    "%s,%s,%s,NULL,'CREDENTIALS_PENDING','PROVIDER_AUTH_CLAIM',"
                    "%s,%s)",
                    (
                        str(uuid_module.uuid4()),
                        str(observation_id),
                        str(candidate_id),
                        BASE_TIME,
                        "registry://ransomlook/bench/" + record_id,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


class TestFixupMigrationAndProvenance:
    """I19F-01 .. I19F-06."""

    def test_i19f_01_fixup_schema(self, database_settings: DatabaseSettings) -> None:
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT proname FROM pg_proc "
                    "WHERE pronamespace = 'public'::regnamespace"
                )
                functions = {row[0] for row in cur.fetchall()}
                cur.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
                )
                indexes = {row[0] for row in cur.fetchall()}
                cur.execute(
                    "SELECT conname FROM pg_constraint "
                    "WHERE connamespace = 'public'::regnamespace"
                )
                constraints = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()
        assert {
            "trusted_registry_observation_record_v1",
            "trusted_registry_candidate_ids_v1",
            "candidate_current_endpoint_match_v1",
            "access_evidence_has_pending_v1",
        } <= functions
        assert "access_evidence_pending_candidate_idx" in indexes
        assert "trusted_registry_observation_pkey" in constraints
        assert "trusted_registry_observation_observation_id_fkey" in constraints
        assert _scalar(
            database_settings,
            "SELECT version FROM darkula_schema_migrations "
            "ORDER BY version DESC LIMIT 1",
        ).startswith("0012_candidate_qualification_fixup.sql")

    @pytest.mark.asyncio
    async def test_i19f_02_forged_generic_provenance(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        observation = await _ingest(spi, _locator_input(record="a"))
        admitted = await _admit(spi, observation.observation_id)
        assert admitted.candidate_id is not None
        assert _count(database_settings, "trusted_registry_observation") == 0

        outcome = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        ).evaluate_and_apply(admitted.candidate_id)
        assert outcome.route is QualificationRoute.PENDING_RECON_POLICY_NOT_ELIGIBLE
        assert _count(database_settings, "qualification_decision") == 0
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM source_candidate_event_history "
                "WHERE event_type = 'QUALIFIED'",
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_i19f_03_fake_adapter_cannot_attest(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        registry = FakeSourceRegistry(index=("blackgate",))
        adapter = RansomLookDiscoveryAdapter(
            transport=registry, request_interval_seconds=0, sleep=_noop
        )
        poller = RegistryPoller(
            persistence=spi,
            discovery_service=SourceDiscoveryService(
                persistence=spi, clock=lambda: BASE_TIME
            ),
            clock=lambda: BASE_TIME,
        )
        result = await poller.poll_once(adapter)
        assert result.observations_added == 3
        assert _count(database_settings, "trusted_registry_observation") == 0
        assert _count(database_settings, "source_candidate") == 0

    @pytest.mark.asyncio
    async def test_i19f_04_approved_transport_vertical_slice(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = _patch_approved_http(monkeypatch)
        adapter = _approved_adapter()
        assert adapter.uses_fixed_origin_transport is True
        poller = RegistryPoller(
            persistence=spi,
            discovery_service=SourceDiscoveryService(
                persistence=spi, clock=lambda: BASE_TIME
            ),
            clock=lambda: BASE_TIME,
        )
        result = await poller.poll_once(adapter)
        assert result.observations_added == 2
        assert len(seen) == 2
        assert _count(database_settings, "trusted_registry_observation") == 2

        async with spi.unit_of_work() as uow:
            observations = await uow.discovery.list_for_provider("ransomlook", limit=50)
        locator = next(
            item
            for item in observations
            if item.normalized_locator == "http://approved-intg.onion/"
        )
        admitted = await _admit(spi, locator.observation_id)
        assert admitted.candidate_id is not None
        outcome = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        ).evaluate_and_apply(admitted.candidate_id)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED
        assert _count(database_settings, "qualification_decision") == 1
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM source_candidate_event_history "
                "WHERE event_type = 'QUALIFIED'",
            )
            == 1
        )
        assert _count(database_settings, "recon_assessment") == 0
        assert _count(database_settings, "source") == 0
        assert _count(database_settings, "source_endpoint") == 0
        assert _count(database_settings, "collection_policy") == 0

    @pytest.mark.asyncio
    async def test_i19f_05_retroactive_verification_on_replay(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_approved_http(monkeypatch)
        adapter = _approved_adapter()
        provider = adapter.for_entry("blackgate", observed_at=BASE_TIME)
        claims = [claim async for claim in provider.observations()]
        locator_claim = next(
            claim for claim in claims if claim.asserted_reference.startswith("http")
        )
        generic = await SourceDiscoveryService(
            persistence=spi, clock=lambda: BASE_TIME
        ).ingest(locator_claim)
        first_ingested_at = generic.ingested_at

        poller = RegistryPoller(
            persistence=spi,
            discovery_service=SourceDiscoveryService(
                persistence=spi, clock=lambda: BASE_TIME
            ),
            clock=lambda: BASE_TIME,
        )
        await poller.poll_once(_approved_adapter())

        async with spi.unit_of_work() as uow:
            reread = await uow.discovery.get_by_key(
                provider_id=locator_claim.provider_id,
                provider_record_id=locator_claim.provider_record_id,
                provider_record_version=locator_claim.provider_record_version,
                detection_method=locator_claim.detection_method,
                reference_kind=locator_claim.reference_kind,
            )
        assert reread is not None
        assert reread.observation_id == generic.observation_id
        assert reread.ingested_at == first_ingested_at
        assert _count(database_settings, "trusted_registry_observation") == 2
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM trusted_registry_observation "
                "WHERE observation_id = %s",
                (str(generic.observation_id),),
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_i19f_06_marker_rollback_leaves_no_observation(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from darkula.app.persistence import AdmissionInvariantError
        from darkula.infrastructure.persistence.postgresql.admission_repository import (
            PostgresAdmissionRepository,
        )

        async def _fail(*args: Any, **kwargs: Any) -> None:
            raise AdmissionInvariantError("injected marker failure")

        monkeypatch.setattr(
            PostgresAdmissionRepository,
            "record_verified_registry_observation",
            _fail,
        )
        service = SourceDiscoveryService(persistence=spi, clock=lambda: BASE_TIME)
        with pytest.raises(AdmissionInvariantError):
            await service._ingest_verified_registry_claim(
                _locator_input(record="a", url="http://rollback-intg.onion/")
            )
        assert _count(database_settings, "discovery_observation") == 0
        assert _count(database_settings, "trusted_registry_observation") == 0


class TestFixupEndpointAndBoundedAccess:
    """I19F-07 .. I19F-10."""

    @pytest.mark.asyncio
    async def test_i19f_07_live_endpoint_after_admission_blocks(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        observation = await _ingest(
            spi, _locator_input(record="a", url="http://live-endpoint-intg.onion/")
        )
        await _mark_verified(spi, observation.observation_id)
        admitted = await _admit(spi, observation.observation_id)
        candidate_id = admitted.candidate_id
        assert candidate_id is not None

        async with spi.unit_of_work() as uow:
            source = Source(
                source_id=SourceId.generate(),
                status=SourceStatus.ACTIVE,
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
                name="seeded",
            )
            await uow.sources.create(source)
            endpoint = SourceEndpoint(
                endpoint_id=SourceEndpointId.generate(),
                source_id=source.source_id,
                uri=observation.normalized_locator or "",
                endpoint_type=EndpointType.ONION,
                status=EndpointStatus.ACTIVE,
                first_observed_at=BASE_TIME,
                last_observed_at=BASE_TIME,
            )
            await uow.sources.add_endpoint(endpoint)
            await uow.commit()
        sources_before = _count(database_settings, "source")
        endpoints_before = _count(database_settings, "source_endpoint")

        outcome = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        ).evaluate_and_apply(candidate_id)
        assert outcome.route is QualificationRoute.PENDING_RECON_AMBIGUOUS
        assert outcome.rule_code.value == "CURRENT_MANAGED_ENDPOINT_CONFLICT"
        assert _count(database_settings, "qualification_decision") == 0
        assert _count(database_settings, "source") == sources_before
        assert _count(database_settings, "source_endpoint") == endpoints_before
        assert (
            _scalar(
                database_settings,
                "SELECT status FROM source_candidate WHERE id = %s",
                (str(candidate_id),),
            )
            == "RECONNAISSANCE_PENDING"
        )

    @pytest.mark.asyncio
    async def test_i19f_08_non_exact_endpoint_does_not_block(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        observation = await _ingest(
            spi, _locator_input(record="a", url="http://near-endpoint-intg.onion/")
        )
        await _mark_verified(spi, observation.observation_id)
        admitted = await _admit(spi, observation.observation_id)
        assert admitted.candidate_id is not None
        async with spi.unit_of_work() as uow:
            source = Source(
                source_id=SourceId.generate(),
                status=SourceStatus.ACTIVE,
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
                name="seeded",
            )
            await uow.sources.create(source)
            await uow.sources.add_endpoint(
                SourceEndpoint(
                    endpoint_id=SourceEndpointId.generate(),
                    source_id=source.source_id,
                    uri="http://near-endpoint-intg.onion/other-path",
                    endpoint_type=EndpointType.ONION,
                    status=EndpointStatus.ACTIVE,
                    first_observed_at=BASE_TIME,
                    last_observed_at=BASE_TIME,
                )
            )
            await uow.commit()
        async with spi.unit_of_work() as uow:
            slot_found, endpoint_present = await uow.admission.current_endpoint_match(
                admitted.candidate_id
            )
        assert slot_found is True
        assert endpoint_present is False
        outcome = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        ).evaluate_and_apply(admitted.candidate_id)
        assert outcome.route is QualificationRoute.REGISTRY_QUALIFIED

    @pytest.mark.asyncio
    async def test_i19f_09_missing_slot_blocks(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        candidate_id = await _seed_candidate_db(spi, "http://missing-slot-intg.onion/")
        observation = await _ingest(
            spi, _locator_input(record="a", url="http://missing-slot-intg.onion/")
        )
        await _mark_verified(spi, observation.observation_id)
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO discovery_candidate_admission "
                    "(observation_id, outcome, reason, candidate_id, processed_at) "
                    "VALUES (%s, 'ADMITTED_EXISTING', 'EXISTING_LEGACY_CANDIDATE', "
                    "%s, %s)",
                    (str(observation.observation_id), str(candidate_id), BASE_TIME),
                )
            conn.commit()
        finally:
            conn.close()
        async with spi.unit_of_work() as uow:
            slot_found, endpoint_present = await uow.admission.current_endpoint_match(
                candidate_id
            )
        assert (slot_found, endpoint_present) == (False, False)
        outcome = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        ).evaluate_and_apply(candidate_id)
        assert outcome.route is QualificationRoute.PENDING_RECON_AMBIGUOUS
        assert outcome.rule_code.value == "MISSING_LOCATOR_SLOT"
        assert _count(database_settings, "qualification_decision") == 0

    @pytest.mark.asyncio
    async def test_i19f_10_bounded_access_probe(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        for count, expected in ((0, False), (1, True), (64, True), (512, True)):
            candidate_id = await _seed_candidate_db(
                spi, f"http://bounded-access-{count}.example.test/"
            )
            _seed_pending_access(database_settings, candidate_id, count)
            async with spi.unit_of_work() as uow:
                result = await uow.access.has_credentials_pending_for_candidate(
                    candidate_id
                )
            assert result is expected
        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'public' "
                    "AND indexname = 'access_evidence_pending_candidate_idx'"
                )
                assert cur.fetchone() is not None
        finally:
            conn.close()
        assert _count(database_settings, "access_evidence") == (1 + 64 + 512)


def _recon_agent(llm: FakeLlmClient) -> Any:
    from darkula.app.recon_agent import ReconAgent
    from darkula.infrastructure.observability import NoOpAgentObservability

    return ReconAgent(
        llm=llm,
        observability=NoOpAgentObservability(),
        settings=_recon_settings(),
    )


class TestFixupReplayConcurrencyAndDurability:
    """I19F-11 .. I19F-16."""

    @pytest.mark.asyncio
    async def test_i19f_11_legacy_recon_preserved(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        candidate_id = await _seed_candidate_db(spi, "http://legacy-recon-intg.onion/")
        llm = FakeLlmClient(expected_response_model=ReconAgentDecision)
        llm.enqueue(_agent_decision(ReconDisposition.QUALIFY))
        coordinator = ReconCoordinator(
            spi=spi,
            agent=_recon_agent(llm),
            crawler=FakeCrawler(),
            settings=_recon_settings(),
            clock=lambda: BASE_TIME,
        )
        result = await coordinator.reconnoiter(candidate_id)
        assert result.final_status is CandidateStatus.QUALIFIED
        assert _count(database_settings, "recon_assessment") == 1
        assert _count(database_settings, "qualification_decision") == 0
        assert _count(database_settings, "trusted_registry_observation") == 0

    @pytest.mark.asyncio
    async def test_i19f_12_promoted_replay(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        observation = await _ingest(
            spi, _locator_input(record="a", url="http://promoted-intg.onion/")
        )
        await _mark_verified(spi, observation.observation_id)
        admitted = await _admit(spi, observation.observation_id)
        candidate_id = admitted.candidate_id
        assert candidate_id is not None
        service = CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        )
        first = await service.evaluate_and_apply(candidate_id)
        assert first.decision_id is not None
        async with spi.unit_of_work() as uow:
            await uow.source_candidates.transition(
                candidate_id,
                expected=CandidateStatus.QUALIFIED,
                new_status=CandidateStatus.PROMOTED,
                event=SourceCandidateEventHistory(
                    event_id=CandidateEventId.generate(),
                    candidate_id=candidate_id,
                    event_type=CandidateEventType.PROMOTED,
                    occurred_at=BASE_TIME,
                ),
            )
            await uow.commit()
        replay = await service.evaluate_and_apply(candidate_id)
        assert replay.decision_id == first.decision_id
        assert replay.status is CandidateStatus.PROMOTED
        assert replay.replayed is True
        assert _count(database_settings, "qualification_decision") == 1
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM source_candidate_event_history "
                "WHERE event_type = 'QUALIFIED' AND candidate_id = %s",
                (str(candidate_id),),
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_i19f_13_concurrent_qualified(
        self,
        spi: PostgresDarkulaSpi,
        second_spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
    ) -> None:
        observation = await _ingest(
            spi, _locator_input(record="a", url="http://race-intg-fixup.onion/")
        )
        await _mark_verified(spi, observation.observation_id)
        admitted = await _admit(spi, observation.observation_id)
        candidate_id = admitted.candidate_id
        assert candidate_id is not None
        trusted = QualificationSettings(ransomlook_location_trusted=True)
        results = await asyncio.gather(
            CandidateQualificationService(
                persistence=spi, settings=trusted, clock=lambda: BASE_TIME
            ).evaluate_and_apply(candidate_id),
            CandidateQualificationService(
                persistence=second_spi, settings=trusted, clock=lambda: BASE_TIME
            ).evaluate_and_apply(candidate_id),
        )
        assert _count(database_settings, "qualification_decision") == 1
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM source_candidate_event_history "
                "WHERE event_type = 'QUALIFIED'",
            )
            == 1
        )
        assert {result.route for result in results} <= {
            QualificationRoute.REGISTRY_QUALIFIED,
            QualificationRoute.CHECKPOINT_OR_STATE_RACE,
            QualificationRoute.ALREADY_TERMINAL_OR_BUSY,
        }

    @pytest.mark.asyncio
    async def test_i19f_14_pending_auth_dispatch_guard(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        observation = await _ingest(
            spi,
            _locator_input(
                record="a",
                url="http://auth-dispatch-intg.onion/",
                requires_auth_claim=True,
            ),
        )
        await _mark_verified(spi, observation.observation_id)
        admitted = await _admit(spi, observation.observation_id)
        assert admitted.candidate_id is not None

        class _Exploding:
            async def reconnoiter(self, *args: Any, **kwargs: Any) -> Any:
                raise AssertionError("no reconnaissance may start")

        outcome = await CandidateQualificationService(
            persistence=spi,
            settings=QualificationSettings(ransomlook_location_trusted=True),
            clock=lambda: BASE_TIME,
        ).dispatch_recon(
            admitted.candidate_id,
            coordinator=_Exploding(),  # type: ignore[arg-type]
            authorized=True,
        )
        assert outcome.dispatched is False
        assert outcome.blocked_route is (QualificationRoute.PENDING_RECON_AUTH_REQUIRED)
        assert _count(database_settings, "recon_assessment") == 0
        assert _count(database_settings, "qualification_decision") == 0
        assert (
            _scalar(
                database_settings,
                "SELECT status FROM source_candidate WHERE id = %s",
                (str(admitted.candidate_id),),
            )
            != "REJECTED"
        )

    @pytest.mark.asyncio
    async def test_i19f_15_restart_durability(
        self,
        spi: PostgresDarkulaSpi,
        second_spi: PostgresDarkulaSpi,
    ) -> None:
        observation = await _ingest(
            spi, _locator_input(record="a", url="http://durable-intg.onion/")
        )
        await _mark_verified(spi, observation.observation_id)
        admitted = await _admit(spi, observation.observation_id)
        assert admitted.candidate_id is not None
        async with second_spi.unit_of_work() as uow:
            verified = (
                await uow.admission.verified_registry_observation_ids_for_candidate(
                    admitted.candidate_id, limit=65
                )
            )
            current = await uow.admission.current_endpoint_match(admitted.candidate_id)
        assert verified == (observation.observation_id,)
        assert current == (True, False)

    @pytest.mark.asyncio
    async def test_i19f_16_cancellation_and_marker_immutability(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        claim = _locator_input(record="a", url="http://immutable-intg.onion/")
        try:
            async with spi.unit_of_work() as uow:
                attempt = await _observation_attempt(claim)
                persisted = await uow.discovery.insert_or_get_exact(attempt)
                await uow.admission.record_verified_registry_observation(
                    persisted.observation_id, verified_at=BASE_TIME
                )
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            pass
        assert _count(database_settings, "discovery_observation") == 0
        assert _count(database_settings, "trusted_registry_observation") == 0

        observation = await _ingest(spi, claim)
        await _mark_verified(spi, observation.observation_id)
        await _mark_verified(spi, observation.observation_id)
        assert (
            _scalar(
                database_settings,
                "SELECT count(*) FROM trusted_registry_observation",
            )
            == 1
        )
        assert (
            _scalar(
                database_settings,
                "SELECT verified_at FROM trusted_registry_observation",
            )
            == BASE_TIME
        )


async def _observation_attempt(claim: Any) -> Any:
    from darkula.domain.discovery import observation_from_input
    from darkula.domain.identifiers import DiscoveryObservationId

    return observation_from_input(
        claim,
        observation_id=DiscoveryObservationId.generate(),
        ingested_at=BASE_TIME,
    )
