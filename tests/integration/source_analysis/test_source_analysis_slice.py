# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 14 real-PostgreSQL source-analysis slice (sections 17-18).

Full path: real PostgreSQL ``Source`` + ``CollectionPolicy``/``CollectionRun``
-> real ``NormalizedContent``/``ExtractedEntity``/``GeographicResolution``/
``ExtractedRelationship`` -> real ``SourceAnalysisContextBuilder`` -> real
``SourceAnalyst`` -> ``FakeLlmClient`` -> trusted grounding -> real
``SourceAnalysisService`` -> real persisted ``SourceAssessment``.

Only the external model response is faked; no Darkula architecture under test
is faked.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from darkula.app.collection import deterministic_request_id
from darkula.app.llm import LlmError, LlmErrorCode
from darkula.app.source_analysis import (
    SourceAnalysisContextBuilder,
    SourceAnalysisEvidenceError,
    SourceAnalysisEvidenceKind,
    SourceAnalysisModelError,
    SourceAnalysisNoEvidenceError,
    SourceAnalysisRequest,
    SourceAnalysisService,
)
from darkula.app.source_analyst import (
    SourceAnalysisResponse,
    SourceAnalyst,
    SourceCharacteristics,
)
from darkula.config.settings import SourceAnalysisSettings
from darkula.domain.collection import (
    CollectionPolicy,
    CollectionRun,
    CollectionRunStatus,
)
from darkula.domain.content import ArtifactCompleteness, NormalizedContent
from darkula.domain.extraction import (
    EntityType,
    ExtractedEntity,
    ExtractionResult,
    ExtractorIdentity,
    SourceSpan,
)
from darkula.domain.geography import (
    GeographicResolution,
    GeographicResolutionStatus,
    GeographicResolverIdentity,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    ExtractedEntityId,
    ExtractedRelationshipId,
    ExtractionResultId,
    GeographicResolutionId,
    NormalizedContentId,
    RelationshipExtractionResultId,
    SourceEndpointId,
    SourceId,
)
from darkula.domain.relationships import (
    ExtractedRelationship,
    RelationshipExtractionResult,
    RelationshipPredicate,
)
from darkula.domain.source import (
    EndpointStatus,
    EndpointType,
    Source,
    SourceEndpoint,
    SourceStatus,
)
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_llm import FakeLlmClient

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 2, 5, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 2, 5, 13, 0, tzinfo=UTC)
_ENDPOINT_ID = SourceEndpointId.generate()


def _response(refs: list[str] | None = None) -> SourceAnalysisResponse:
    return SourceAnalysisResponse(
        confidence=0.81,
        relevance=0.61,
        activity=0.41,
        novelty=None,
        characteristics=SourceCharacteristics(
            source_type="marketplace",
            content_focus=["stolen data"],
            summary="A synthetic marketplace observed over the window.",
        ),
        evidence_refs=refs or ["A1"],
    )


def _service(
    spi: PostgresDarkulaSpi,
    *,
    llm: FakeLlmClient,
    settings: SourceAnalysisSettings | None = None,
) -> SourceAnalysisService:
    resolved = settings or SourceAnalysisSettings()
    return SourceAnalysisService(
        spi=spi,
        context_builder=SourceAnalysisContextBuilder(spi=spi, settings=resolved),
        analyst=SourceAnalyst(llm=llm),
        settings=resolved,
        clock=lambda: _T1,
    )


async def _seed(
    spi: PostgresDarkulaSpi,
) -> tuple[Source, CollectionRun, NormalizedContent, ExtractedEntity]:
    source = Source(
        source_id=SourceId.generate(),
        status=SourceStatus.ACTIVE,
        created_at=_T0,
        updated_at=_T0,
        name="blackgate-intg",
    )
    endpoint = SourceEndpoint(
        endpoint_id=_ENDPOINT_ID,
        source_id=source.source_id,
        uri="http://darkula-fake-world-intg:8080/thread/1",
        endpoint_type=EndpointType.CLEARNET,
        status=EndpointStatus.ACTIVE,
        first_observed_at=_T0,
        last_observed_at=_T0,
    )
    policy = CollectionPolicy(
        policy_id=CollectionPolicyId.generate(),
        source_id=source.source_id,
        active=True,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=300,
        next_due_at=_T0,
        allowed_endpoint_ids=(endpoint.endpoint_id,),
        max_pages=5,
        max_requests=20,
        max_depth=2,
        timeout_seconds=60.0,
    )
    run_id = CollectionRunId.generate()
    run = CollectionRun(
        run_id=run_id,
        policy_id=policy.policy_id,
        policy_revision=1,
        policy_snapshot=policy.execution_snapshot(),
        source_id=source.source_id,
        scheduled_for=_T0,
        created_at=_T0,
        status=CollectionRunStatus.QUEUED,
    )
    async with spi.unit_of_work() as uow:
        await uow.sources.create(source)
        await uow.sources.add_endpoint(endpoint)
        await uow.collection.create_policy(policy)
        await uow.collection.create_run(run)
        await uow.collection.claim_run(
            run_id=run_id,
            execution_id=str(uuid.uuid4()),
            started_at=_T0,
            lease_expires_at=_T1,
        )
        await uow.collection.complete_run(
            run_id=run_id,
            new_status=CollectionRunStatus.SUCCEEDED,
            completed_at=_T1,
            crawl_requests_attempted=1,
            pages_observed=1,
            content_observations=1,
            content_created=1,
            content_deduplicated=0,
            failure_code=None,
            failure_summary=None,
        )
        await uow.commit()

    request_id = deterministic_request_id(
        run_id=run_id, endpoint_id=_ENDPOINT_ID, attempt=1
    )
    content = NormalizedContent(
        content_id=NormalizedContentId.generate(),
        source_uri="http://darkula-fake-world-intg:8080/thread/1",
        observed_at=_T0,
        crawl_request_id=request_id,
        observation_index=1,
        normalization_version="text-v1",
        completeness=ArtifactCompleteness.SAMPLE,
        title="BlackGate thread",
        text="bounded preview",
    )
    extraction = ExtractionResult(
        result_id=ExtractionResultId.generate(),
        content_id=content.content_id,
        profile_name="semantic-entities",
        profile_version="v1",
        extractor_manifest=(ExtractorIdentity(name="semantic-llm", version="v1"),),
        extracted_at=_T0,
        entity_count=2,
    )
    organization = ExtractedEntity(
        entity_id=ExtractedEntityId.generate(),
        extraction_result_id=extraction.result_id,
        content_id=content.content_id,
        entity_type=EntityType.ORGANIZATION,
        raw_value="Mason Creek General Hospital",
        normalized_value="Mason Creek General Hospital",
        source_span=SourceSpan(start=0, end=28),
        extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
        extraction_confidence=0.9,
    )
    location = ExtractedEntity(
        entity_id=ExtractedEntityId.generate(),
        extraction_result_id=extraction.result_id,
        content_id=content.content_id,
        entity_type=EntityType.LOCATION,
        raw_value="Washington",
        normalized_value="Washington",
        source_span=SourceSpan(start=30, end=40),
        extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
        extraction_confidence=0.8,
    )
    resolution = GeographicResolution(
        resolution_id=GeographicResolutionId.generate(),
        extracted_entity_id=location.entity_id,
        status=GeographicResolutionStatus.RESOLVED,
        resolver=GeographicResolverIdentity(name="fake", version="v1"),
        resolved_at=_T0,
        canonical_name="Washington, US",
        country_code="US",
        confidence=0.7,
    )
    relationship_result = RelationshipExtractionResult(
        result_id=RelationshipExtractionResultId.generate(),
        content_id=content.content_id,
        profile_name="relationship-assertions",
        profile_version="v1",
        extractor_manifest=(ExtractorIdentity(name="relationship-llm", version="v1"),),
        extracted_at=_T0,
        relationship_count=1,
    )
    relationship = ExtractedRelationship(
        relationship_id=ExtractedRelationshipId.generate(),
        extraction_result_id=relationship_result.result_id,
        content_id=content.content_id,
        source_entity_id=organization.entity_id,
        predicate=RelationshipPredicate.LOCATED_IN,
        target_entity_id=location.entity_id,
        support_span=SourceSpan(start=0, end=40),
        support_text="Mason Creek General Hospital  Washington",
        extractor=ExtractorIdentity(name="relationship-llm", version="v1"),
        extraction_confidence=0.75,
    )
    async with spi.unit_of_work() as uow:
        await uow.content.create_observation(content)
        await uow.extraction.create_result(extraction)
        await uow.extraction.create_entity(organization)
        await uow.extraction.create_entity(location)
        await uow.geography.create(resolution)
        await uow.relationships.create_result(relationship_result)
        await uow.relationships.create_relationship(relationship)
        await uow.commit()
    return source, run, content, organization


class TestRealPostgresSourceAnalysis:
    @pytest.mark.asyncio
    async def test_slice_persists_grounded_assessment(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, _run, _content, organization = await _seed(spi)
        llm = FakeLlmClient(default_response=_response())
        service = _service(spi, llm=llm)
        request = SourceAnalysisRequest(source.source_id, _T0, _T1)
        outcome = await service.analyze(request)
        assert outcome.created is True
        assessment = outcome.assessment
        assert assessment.source_id == source.source_id
        assert assessment.window_start == _T0
        assert assessment.window_end == _T1
        assert assessment.profile_name == "source-analysis"
        assert assessment.profile_version == "v1"
        assert assessment.evidence_references
        assert llm.call_count == 1

        # Durable evidence refs resolve to persisted observations.
        from darkula.domain.identifiers import SourceAssessmentId

        async with spi.unit_of_work() as uow:
            stored = await uow.sources.get_source_assessment_by_profile(
                source.source_id, _T0, _T1, "source-analysis", "v1"
            )
            entity = await uow.extraction.get_entity(organization.entity_id)
        assert stored is not None
        assert isinstance(stored.assessment_id, SourceAssessmentId)
        assert entity is not None

        # Replay performs no second model call and returns the same assessment.
        replay = await service.analyze(request)
        assert replay.created is False
        assert replay.assessment.assessment_id == assessment.assessment_id
        assert llm.call_count == 1

    @pytest.mark.asyncio
    async def test_context_only_contains_source_window_evidence(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, _run, content, _entity = await _seed(spi)
        builder = SourceAnalysisContextBuilder(
            spi=spi, settings=SourceAnalysisSettings()
        )
        context = await builder.build(SourceAnalysisRequest(source.source_id, _T0, _T1))
        assert any(
            item.kind is SourceAnalysisEvidenceKind.CONTENT_OBSERVATION
            and item.durable_reference == f"content:{content.content_id}"
            for item in context.evidence
        )
        kinds = {item.kind for item in context.evidence}
        assert SourceAnalysisEvidenceKind.ENTITY_OCCURRENCE in kinds
        assert SourceAnalysisEvidenceKind.GEOGRAPHIC_RESOLUTION in kinds
        assert SourceAnalysisEvidenceKind.RELATIONSHIP_ASSERTION in kinds
        assert SourceAnalysisEvidenceKind.COLLECTION_RUN in kinds

    @pytest.mark.asyncio
    async def test_no_evidence_fails_closed(self, spi: PostgresDarkulaSpi) -> None:
        source = Source(
            source_id=SourceId.generate(),
            status=SourceStatus.ACTIVE,
            created_at=_T0,
            updated_at=_T0,
        )
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.commit()
        llm = FakeLlmClient(default_response=_response())
        service = _service(spi, llm=llm)
        with pytest.raises(SourceAnalysisNoEvidenceError):
            await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_unknown_ref_persists_nothing(self, spi: PostgresDarkulaSpi) -> None:
        source, _run, _content, _entity = await _seed(spi)
        llm = FakeLlmClient(default_response=_response(["A999"]))
        service = _service(spi, llm=llm)
        with pytest.raises(SourceAnalysisEvidenceError):
            await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        async with spi.unit_of_work() as uow:
            stored = await uow.sources.get_source_assessment_by_profile(
                source.source_id, _T0, _T1, "source-analysis", "v1"
            )
        assert stored is None

    @pytest.mark.asyncio
    async def test_provider_failure_persists_nothing(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, _run, _content, _entity = await _seed(spi)
        llm = FakeLlmClient()
        llm.enqueue(LlmError(LlmErrorCode.PROVIDER_FAILURE))
        service = _service(spi, llm=llm)
        with pytest.raises(SourceAnalysisModelError):
            await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        async with spi.unit_of_work() as uow:
            stored = await uow.sources.get_source_assessment_by_profile(
                source.source_id, _T0, _T1, "source-analysis", "v1"
            )
        assert stored is None

    @pytest.mark.asyncio
    async def test_cancellation_persists_nothing(self, spi: PostgresDarkulaSpi) -> None:
        source, _run, _content, _entity = await _seed(spi)
        llm = FakeLlmClient()
        llm.enqueue(asyncio.CancelledError())
        service = _service(spi, llm=llm)
        with pytest.raises(asyncio.CancelledError):
            await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        async with spi.unit_of_work() as uow:
            stored = await uow.sources.get_source_assessment_by_profile(
                source.source_id, _T0, _T1, "source-analysis", "v1"
            )
        assert stored is None

    @pytest.mark.asyncio
    async def test_concurrent_same_key_one_durable_winner(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, _run, _content, _entity = await _seed(spi)
        # Each caller gets its own response; duplicate model work is acceptable,
        # duplicate durable rows are not.
        first = _service(spi, llm=FakeLlmClient(default_response=_response()))
        second = _service(spi, llm=FakeLlmClient(default_response=_response()))
        request = SourceAnalysisRequest(source.source_id, _T0, _T1)
        outcomes = await asyncio.gather(first.analyze(request), second.analyze(request))
        created = [outcome for outcome in outcomes if outcome.created]
        assert len(created) == 1
        async with spi.unit_of_work() as uow:
            assessments = await uow.sources.list_source_assessments(source.source_id)
        assert len(assessments) == 1
        assert all(
            outcome.assessment.assessment_id == assessments[0].assessment_id
            for outcome in outcomes
        )
