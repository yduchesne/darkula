# SPDX-License-Identifier: AGPL-3.0-only
"""PR 14 request, context, grounding, and service behavior (AP/AC/EG/SS)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from tests.support.source_analysis_fakes import (
    MemSourceAnalysisSpi,
    MemSourceAnalysisState,
)

from darkula.app.collection import deterministic_request_id
from darkula.app.llm import LlmError, LlmErrorCode
from darkula.app.persistence import IntegrityError
from darkula.app.source_analysis import (
    SourceAnalysisContext,
    SourceAnalysisContextBuilder,
    SourceAnalysisEvidence,
    SourceAnalysisEvidenceError,
    SourceAnalysisEvidenceKind,
    SourceAnalysisInvalidWindowError,
    SourceAnalysisModelError,
    SourceAnalysisNoEvidenceError,
    SourceAnalysisOutcome,
    SourceAnalysisOutputInvalidError,
    SourceAnalysisPersistenceIntegrityError,
    SourceAnalysisRequest,
    SourceAnalysisService,
    SourceAnalysisSourceNotFoundError,
    derive_source_request_ids,
    ground_evidence_references,
)
from darkula.app.source_analyst import (
    SourceAnalysisResponse,
    SourceAnalyst,
    SourceCharacteristics,
)
from darkula.config.settings import SourceAnalysisSettings
from darkula.domain.collection import CollectionRun, CollectionRunStatus
from darkula.domain.content import ArtifactCompleteness, NormalizedContent
from darkula.domain.extraction import (
    EntityType,
    ExtractedEntity,
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
    RelationshipPredicate,
)
from darkula.domain.source import (
    Source,
    SourceStatus,
)
from darkula.testing.fake_llm import FakeLlmClient

_T0 = datetime(2026, 2, 5, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 2, 5, 13, 0, tzinfo=UTC)
_OUTSIDE = datetime(2026, 2, 6, 12, 0, tzinfo=UTC)
_ENDPOINT = SourceEndpointId.generate()


def _response(refs: list[str] | None = None) -> SourceAnalysisResponse:
    return SourceAnalysisResponse(
        confidence=0.8,
        relevance=0.7,
        activity=0.4,
        novelty=None,
        characteristics=SourceCharacteristics(
            source_type="marketplace",
            summary="A synthetic marketplace observed over the window.",
        ),
        evidence_refs=refs or ["A1"],
    )


def _source() -> Source:
    return Source(
        source_id=SourceId.generate(),
        status=SourceStatus.ACTIVE,
        created_at=_T0,
        updated_at=_T0,
        name="blackgate",
    )


def _snapshot(endpoint_ids: tuple[SourceEndpointId, ...]) -> dict[str, object]:
    return {
        "policy_revision": 1,
        "interval_seconds": 300,
        "allowed_endpoint_ids": [str(item) for item in endpoint_ids],
        "allowed_paths": ["/"],
        "max_pages": 5,
        "max_requests": 20,
        "max_depth": 2,
        "timeout_seconds": 60.0,
        "authentication_reference": None,
    }


def _run(
    *,
    source_id: SourceId,
    run_id: CollectionRunId | None = None,
    endpoint_ids: tuple[SourceEndpointId, ...] = (_ENDPOINT,),
    attempt_count: int = 1,
    created_at: datetime = _T0,
    completed_at: datetime | None = _T1,
) -> CollectionRun:
    return CollectionRun(
        run_id=run_id or CollectionRunId.generate(),
        policy_id=CollectionPolicyId.generate(),
        policy_revision=1,
        policy_snapshot=_snapshot(endpoint_ids),
        source_id=source_id,
        scheduled_for=created_at,
        created_at=created_at,
        started_at=created_at,
        completed_at=completed_at,
        status=(
            CollectionRunStatus.SUCCEEDED
            if completed_at is not None
            else CollectionRunStatus.RUNNING
        ),
        attempt_count=attempt_count,
        content_observations=1,
    )


def _observation(
    *,
    crawl_request_id: str,
    observed_at: datetime = _T0,
    title: str = "Index",
) -> NormalizedContent:
    return NormalizedContent(
        content_id=NormalizedContentId.generate(),
        source_uri="http://darkula-fake-world-intg:8080/thread/1",
        observed_at=observed_at,
        crawl_request_id=crawl_request_id,
        observation_index=1,
        normalization_version="text-v1",
        completeness=ArtifactCompleteness.SAMPLE,
        title=title,
        text="bounded preview",
    )


def _entity(
    content_id: NormalizedContentId, value: str = "Mason Creek"
) -> ExtractedEntity:
    return ExtractedEntity(
        entity_id=ExtractedEntityId.generate(),
        extraction_result_id=ExtractionResultId.generate(),
        content_id=content_id,
        entity_type=EntityType.ORGANIZATION,
        raw_value=value,
        normalized_value=value,
        source_span=SourceSpan(start=0, end=len(value)),
        extractor=ExtractorIdentity(name="semantic-llm", version="v1"),
        extraction_confidence=0.9,
    )


def _resolution(entity: ExtractedEntity) -> GeographicResolution:
    return GeographicResolution(
        resolution_id=GeographicResolutionId.generate(),
        extracted_entity_id=entity.entity_id,
        status=GeographicResolutionStatus.RESOLVED,
        resolver=GeographicResolverIdentity(name="fake", version="v1"),
        resolved_at=_T0,
        canonical_name="Mason Creek, WA",
        country_code="US",
        confidence=0.8,
    )


def _relationship(
    content_id: NormalizedContentId, source: ExtractedEntity, target: ExtractedEntity
) -> ExtractedRelationship:
    return ExtractedRelationship(
        relationship_id=ExtractedRelationshipId.generate(),
        extraction_result_id=RelationshipExtractionResultId.generate(),
        content_id=content_id,
        source_entity_id=source.entity_id,
        predicate=RelationshipPredicate.LOCATED_IN,
        target_entity_id=target.entity_id,
        support_span=SourceSpan(start=0, end=10),
        support_text="0123456789",
        extractor=ExtractorIdentity(name="relationship-llm", version="v1"),
        extraction_confidence=0.8,
    )


def _seed_context(
    state: MemSourceAnalysisState,
    *,
    source: Source,
    request_id: str | None = None,
    observed_at: datetime = _T0,
    run_created_at: datetime = _T0,
    run_completed_at: datetime | None = _T1,
    content_title: str = "Index",
    with_entity: bool = False,
    with_geography: bool = False,
    with_relationship: bool = False,
) -> tuple[NormalizedContent, str]:
    run = _run(
        source_id=source.source_id,
        created_at=run_created_at,
        completed_at=run_completed_at,
    )
    state.runs[run.run_id] = run
    derived = deterministic_request_id(
        run_id=run.run_id, endpoint_id=_ENDPOINT, attempt=1
    )
    content = _observation(
        crawl_request_id=request_id or derived,
        observed_at=observed_at,
        title=content_title,
    )
    state.observations[content.content_id] = content
    if with_entity or with_geography or with_relationship:
        organization = _entity(content.content_id)
        state.entities[str(organization.entity_id)] = organization
        if with_geography:
            state.resolutions[str(_resolution(organization).resolution_id)] = (
                _resolution(organization)
            )
        if with_relationship:
            target = _entity(content.content_id, value="Washington")
            state.entities[str(target.entity_id)] = target
            rel = _relationship(content.content_id, organization, target)
            state.relationships[str(rel.relationship_id)] = rel
    return content, derived


def _builder(
    spi: MemSourceAnalysisSpi, **overrides: int
) -> SourceAnalysisContextBuilder:
    settings = SourceAnalysisSettings(**overrides)  # type: ignore[arg-type]
    return SourceAnalysisContextBuilder(spi=spi, settings=settings)


def _service(
    spi: MemSourceAnalysisSpi,
    *,
    response: SourceAnalysisResponse | None = None,
    llm: FakeLlmClient | None = None,
    **overrides: int,
) -> SourceAnalysisService:
    settings = SourceAnalysisSettings(**overrides)  # type: ignore[arg-type]
    client = llm or FakeLlmClient(default_response=response or _response())
    return SourceAnalysisService(
        spi=spi,
        context_builder=_builder(spi, **overrides),
        analyst=SourceAnalyst(llm=client),
        settings=settings,
        clock=lambda: _T1,
    )


class TestRequest:
    """AP1-AP8: explicit bounded request validation."""

    def test_ap1_valid_utc_window(self) -> None:
        source = SourceId.generate()
        request = SourceAnalysisRequest(source, _T0, _T1)
        assert request.window_start == _T0
        assert request.window_end == _T1
        assert request.source_id == source

    def test_ap2_aware_non_utc_normalized(self) -> None:
        offset = timezone(timedelta(hours=-5))
        start = datetime(2026, 2, 5, 7, 0, tzinfo=offset)
        request = SourceAnalysisRequest(SourceId.generate(), start, start)
        assert request.window_start == _T0
        assert request.window_start.utcoffset() == timedelta(0)

    def test_ap3_naive_rejected(self) -> None:
        with pytest.raises(SourceAnalysisInvalidWindowError):
            SourceAnalysisRequest(SourceId.generate(), datetime(2026, 2, 5), _T1)

    def test_ap4_end_before_start_rejected(self) -> None:
        with pytest.raises(SourceAnalysisInvalidWindowError):
            SourceAnalysisRequest(SourceId.generate(), _T1, _T0)

    @pytest.mark.asyncio
    async def test_ap5_exact_max_window_legal(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        service = _service(spi, max_window_days=1)
        request = SourceAnalysisRequest(source.source_id, _T0, _T0 + timedelta(days=1))
        with pytest.raises(SourceAnalysisNoEvidenceError):
            await service.analyze(request)

    @pytest.mark.asyncio
    async def test_ap6_over_max_window_typed_failure(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        service = _service(spi, max_window_days=1)
        request = SourceAnalysisRequest(source.source_id, _T0, _T0 + timedelta(days=2))
        with pytest.raises(SourceAnalysisInvalidWindowError):
            await service.analyze(request)

    def test_ap7_profile_is_not_a_request_field(self) -> None:
        assert set(SourceAnalysisRequest.__dataclass_fields__) == {
            "source_id",
            "window_start",
            "window_end",
        }

    def test_ap8_source_identity_required(self) -> None:
        with pytest.raises(TypeError):
            SourceAnalysisRequest(  # type: ignore[call-arg]
                window_start=_T0, window_end=_T1
            )


class TestContextBuilder:
    """AC1-AC20: bounded deterministic persisted-evidence selection."""

    @pytest.mark.asyncio
    async def test_ac1_included_and_ac19_ref_mapping(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        assert context.evidence
        assert context.evidence[0].ref == "A1"
        assert any(
            item.kind is SourceAnalysisEvidenceKind.CONTENT_OBSERVATION
            for item in context.evidence
        )
        assert [item.ref for item in context.evidence] == [
            f"A{i}" for i in range(1, len(context.evidence) + 1)
        ]

    @pytest.mark.asyncio
    async def test_ac2_ac3_window_bounds_exclude(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(
            state, source=source, observed_at=datetime(2026, 2, 4, tzinfo=UTC)
        )
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        content_items = [
            item
            for item in context.evidence
            if item.kind is SourceAnalysisEvidenceKind.CONTENT_OBSERVATION
        ]
        assert content_items == []

    @pytest.mark.asyncio
    async def test_ac4_other_source_excluded(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source, observed_at=_OUTSIDE)
        other = _source()
        # Another source's run and observation must never leak in.
        _seed_context(state, source=other, observed_at=_T0)
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        content_items = [
            item
            for item in context.evidence
            if item.kind is SourceAnalysisEvidenceKind.CONTENT_OBSERVATION
        ]
        assert content_items == []

    @pytest.mark.asyncio
    async def test_ac5_entities_included(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source, with_entity=True)
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        kinds = {item.kind for item in context.evidence}
        assert SourceAnalysisEvidenceKind.ENTITY_OCCURRENCE in kinds

    @pytest.mark.asyncio
    async def test_ac6_geography_linked_to_occurrence(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source, with_geography=True)
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        kinds = {item.kind for item in context.evidence}
        assert SourceAnalysisEvidenceKind.GEOGRAPHIC_RESOLUTION in kinds

    @pytest.mark.asyncio
    async def test_ac7_relationships_are_assertions(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source, with_relationship=True)
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        rels = [
            item
            for item in context.evidence
            if item.kind is SourceAnalysisEvidenceKind.RELATIONSHIP_ASSERTION
        ]
        assert rels
        assert rels[0].durable_reference.startswith("relationship:")

    @pytest.mark.asyncio
    async def test_ac8_collection_run_represented(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        runs = [
            item
            for item in context.evidence
            if item.kind is SourceAnalysisEvidenceKind.COLLECTION_RUN
        ]
        assert runs
        assert runs[0].durable_reference.startswith("collection-run:")

    @pytest.mark.asyncio
    async def test_ac9_deterministic_ordering(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source, with_entity=True)
        first = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        second = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        assert first.evidence == second.evidence

    @pytest.mark.asyncio
    async def test_ac12_missing_extraction_not_invented(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        kinds = {item.kind for item in context.evidence}
        assert SourceAnalysisEvidenceKind.ENTITY_OCCURRENCE not in kinds

    @pytest.mark.asyncio
    async def test_ac14_overflow_truncates_with_flag(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source, with_relationship=True)
        context = await _builder(spi, max_evidence_items=1).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        assert len(context.evidence) == 1
        assert context.context_truncated is True

    @pytest.mark.asyncio
    async def test_ac16_summary_bounded(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source, content_title="x" * 400)
        context = await _builder(
            spi, max_evidence_summary_chars=20, max_context_chars=100
        ).build(SourceAnalysisRequest(source.source_id, _T0, _T1))
        assert all(len(item.summary) <= 20 for item in context.evidence)

    @pytest.mark.asyncio
    async def test_ac20_unprovable_ownership_excluded(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        # An observation whose crawl request is not derived from any persisted
        # run cannot be proven to belong to the source: never included.
        content = _observation(crawl_request_id="unknown-request")
        state.observations[content.content_id] = content
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        assert context.evidence == ()

    def test_ac10_derive_skips_invalid_snapshot(self) -> None:
        run = _run(source_id=SourceId.generate())
        broken = CollectionRun(
            run_id=run.run_id,
            policy_id=run.policy_id,
            policy_revision=1,
            policy_snapshot={"unexpected": "shape"},
            source_id=run.source_id,
            scheduled_for=run.scheduled_for,
            created_at=run.created_at,
            status=CollectionRunStatus.QUEUED,
        )
        assert derive_source_request_ids((broken,)) == ()

    def test_ac10_distinct_content_ids_are_distinct(self) -> None:
        run = _run(source_id=SourceId.generate())
        first = derive_source_request_ids((run,))
        second = derive_source_request_ids((run, _run(source_id=run.source_id)))
        assert set(first).issubset(set(second))
        assert len(second) >= 2

    @pytest.mark.asyncio
    async def test_empty_state_no_evidence(self) -> None:
        source = _source()
        spi = MemSourceAnalysisSpi(
            MemSourceAnalysisState(sources={source.source_id: source})
        )
        context = await _builder(spi).build(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        assert context.evidence == ()
        assert context.context_truncated is False


class TestGrounding:
    """EG1-EG8: trusted durable reference grounding."""

    def _catalog(self) -> tuple[SourceAnalysisEvidence, ...]:
        return (
            SourceAnalysisEvidence(
                ref="A1",
                kind=SourceAnalysisEvidenceKind.CONTENT_OBSERVATION,
                observed_at=_T0,
                summary="one",
                durable_reference="content:1",
            ),
            SourceAnalysisEvidence(
                ref="A2",
                kind=SourceAnalysisEvidenceKind.ENTITY_OCCURRENCE,
                observed_at=_T0,
                summary="two",
                durable_reference="entity:2",
            ),
        )

    def test_eg1_supplied_refs_produce_durable_refs(self) -> None:
        result = ground_evidence_references(
            self._catalog(), _response(["A1"]), max_refs=100
        )
        assert result == ("content:1",)

    def test_eg2_unknown_ref_fails_whole_operation(self) -> None:
        with pytest.raises(SourceAnalysisEvidenceError):
            ground_evidence_references(
                self._catalog(), _response(["A999"]), max_refs=100
            )

    def test_eg3_duplicate_refs_deduplicated(self) -> None:
        result = ground_evidence_references(
            self._catalog(), _response(["A2", "A2"]), max_refs=100
        )
        assert result == ("entity:2",)

    def test_eg4_refs_returned_in_context_order(self) -> None:
        result = ground_evidence_references(
            self._catalog(), _response(["A2", "A1"]), max_refs=100
        )
        assert result == ("content:1", "entity:2")

    def test_eg5_too_many_refs_rejected(self) -> None:
        with pytest.raises(SourceAnalysisOutputInvalidError):
            ground_evidence_references(
                self._catalog(), _response(["A1", "A2"]), max_refs=1
            )

    def test_eg6_crafted_durable_ref_is_unknown(self) -> None:
        with pytest.raises(SourceAnalysisEvidenceError):
            ground_evidence_references(
                self._catalog(),
                _response(["content:1"]),
                max_refs=100,
            )

    def test_eg8_relationship_ref_is_occurrence_ref(self) -> None:
        catalog = (
            SourceAnalysisEvidence(
                ref="A1",
                kind=SourceAnalysisEvidenceKind.RELATIONSHIP_ASSERTION,
                observed_at=_T0,
                summary="a --rel--> b",
                durable_reference="relationship:9",
            ),
        )
        result = ground_evidence_references(catalog, _response(["A1"]), max_refs=100)
        assert result == ("relationship:9",)


class TestService:
    """SS1-SS20: service sequencing, trust, races, and failures."""

    @pytest.mark.asyncio
    async def test_ss1_missing_source_no_llm(self) -> None:
        spi = MemSourceAnalysisSpi(MemSourceAnalysisState())
        llm = FakeLlmClient(default_response=_response())
        service = _service(spi, llm=llm)
        with pytest.raises(SourceAnalysisSourceNotFoundError):
            await service.analyze(SourceAnalysisRequest(SourceId.generate(), _T0, _T1))
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_ss3_no_evidence_no_llm_or_persistence(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        llm = FakeLlmClient(default_response=_response())
        service = _service(spi, llm=llm)
        with pytest.raises(SourceAnalysisNoEvidenceError):
            await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        assert llm.call_count == 0
        assert state.assessments == {}

    @pytest.mark.asyncio
    async def test_ss4_ss5_ss6_ss7_ss8_valid_analysis(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        service = _service(spi)
        outcome = await service.analyze(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        assert isinstance(outcome, SourceAnalysisOutcome)
        assert outcome.created is True
        assessment = outcome.assessment
        assert assessment.source_id == source.source_id
        assert assessment.window_start == _T0
        assert assessment.window_end == _T1
        assert assessment.assessed_at == _T1
        assert assessment.profile_name == "source-analysis"
        assert assessment.profile_version == "v1"
        assert assessment.evidence_references[0].startswith(
            ("content:", "collection-run:", "entity:", "geography:", "relationship:")
        )
        assert str(assessment.assessment_id) in {str(key) for key in state.assessments}

    @pytest.mark.asyncio
    async def test_ss2_ss12_replay_no_second_llm(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        llm = FakeLlmClient(default_response=_response())
        service = _service(spi, llm=llm)
        request = SourceAnalysisRequest(source.source_id, _T0, _T1)
        first = await service.analyze(request)
        second = await service.analyze(request)
        assert first.assessment.assessment_id == second.assessment.assessment_id
        assert second.created is False
        assert llm.call_count == 1

    @pytest.mark.asyncio
    async def test_ss9_model_failure_no_assessment(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        llm = FakeLlmClient()
        llm.enqueue(LlmError(LlmErrorCode.PROVIDER_FAILURE))
        service = _service(spi, llm=llm)
        with pytest.raises(SourceAnalysisModelError):
            await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        assert state.assessments == {}

    @pytest.mark.asyncio
    async def test_ss10_grounding_failure_no_assessment(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        service = _service(spi, response=_response(["A999"]))
        with pytest.raises(SourceAnalysisEvidenceError):
            await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        assert state.assessments == {}

    @pytest.mark.asyncio
    async def test_ss11_persistence_failure_no_partial_row(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        state.append_error = IntegrityError("injected")
        service = _service(spi)
        with pytest.raises(SourceAnalysisPersistenceIntegrityError):
            await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        assert state.assessments == {}

    @pytest.mark.asyncio
    async def test_ss13_profile_version_coexists(self) -> None:
        from darkula.domain.identifiers import SourceAssessmentId
        from darkula.domain.source import Confidence, SourceAssessment

        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        v2 = SourceAssessment(
            assessment_id=SourceAssessmentId.generate(),
            source_id=source.source_id,
            assessed_at=_T0,
            window_start=_T0,
            window_end=_T1,
            confidence=Confidence(0.5),
            profile_name="source-analysis",
            profile_version="v2",
        )
        state.assessments[v2.assessment_id] = v2
        service = _service(spi)
        outcome = await service.analyze(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        assert outcome.created is True
        assert len(state.assessments) == 2

    @pytest.mark.asyncio
    async def test_ss14_different_window_coexists(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        later = _T1 + timedelta(hours=1)
        _seed_context(state, source=source, observed_at=_T0)
        _seed_context(
            state,
            source=source,
            observed_at=later,
            run_created_at=_T1,
            run_completed_at=later + timedelta(hours=1),
        )
        service = _service(spi)
        first = await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        second = await service.analyze(
            SourceAnalysisRequest(source.source_id, later, later)
        )
        assert first.created is True
        assert second.created is True
        assert first.assessment.assessment_id != second.assessment.assessment_id
        assert len(state.assessments) == 2

    @pytest.mark.asyncio
    async def test_ss16_cancellation_no_assessment(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        llm = FakeLlmClient()
        llm.enqueue(asyncio.CancelledError())
        service = _service(spi, llm=llm)
        with pytest.raises(asyncio.CancelledError):
            await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        assert state.assessments == {}

    @pytest.mark.asyncio
    async def test_ss17_ss18_loser_reloads_winner(self) -> None:
        from darkula.domain.identifiers import SourceAssessmentId
        from darkula.domain.source import Confidence, SourceAssessment

        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source)
        winner = SourceAssessment(
            assessment_id=SourceAssessmentId.generate(),
            source_id=source.source_id,
            assessed_at=_T0,
            window_start=_T0,
            window_end=_T1,
            confidence=Confidence(0.9),
            profile_name="source-analysis",
            profile_version="v1",
        )
        # Simulate a concurrent winner appearing after the model call.
        state.semantic_conflict = True
        state.assessments[winner.assessment_id] = winner
        service = _service(spi)
        outcome = await service.analyze(
            SourceAnalysisRequest(source.source_id, _T0, _T1)
        )
        assert outcome.created is False
        assert outcome.assessment.assessment_id == winner.assessment_id

    @pytest.mark.asyncio
    async def test_ss19_ss20_history_unchanged(self) -> None:
        source = _source()
        state = MemSourceAnalysisState(sources={source.source_id: source})
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=source, with_geography=True, with_relationship=True)
        entities_before = dict(state.entities)
        resolutions_before = dict(state.resolutions)
        relationships_before = dict(state.relationships)
        service = _service(spi)
        await service.analyze(SourceAnalysisRequest(source.source_id, _T0, _T1))
        assert state.sources[source.source_id] == source
        assert state.entities == entities_before
        assert state.resolutions == resolutions_before
        assert state.relationships == relationships_before

    @pytest.mark.asyncio
    async def test_ss15_different_source_distinct(self) -> None:
        first = _source()
        second = _source()
        state = MemSourceAnalysisState(
            sources={first.source_id: first, second.source_id: second}
        )
        spi = MemSourceAnalysisSpi(state)
        _seed_context(state, source=first)
        _seed_context(state, source=second)
        service = _service(spi)
        a = await service.analyze(SourceAnalysisRequest(first.source_id, _T0, _T1))
        b = await service.analyze(SourceAnalysisRequest(second.source_id, _T0, _T1))
        assert a.assessment.source_id == first.source_id
        assert b.assessment.source_id == second.source_id
        assert a.assessment.assessment_id != b.assessment.assessment_id


class TestContextTruncation:
    def test_context_dataclass_defaults(self) -> None:
        context = SourceAnalysisContext(
            source_id=SourceId.generate(),
            window_start=_T0,
            window_end=_T1,
            evidence=(),
        )
        assert context.context_truncated is False
