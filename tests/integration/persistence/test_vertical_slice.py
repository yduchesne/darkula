# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical PR 4 vertical slice (plan section 14).

Creates a candidate through the real
``PostgresDarkulaSpi -> PostgresUnitOfWork -> repository -> stored function
-> PostgreSQL`` path, transitions it, records recon, then promotes an
independent Source with endpoints and assessments across several real
transactions — and independently confirms durable state with test-only SQL.
No DataStream/Redpanda, no LLM, no crawler, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from darkula.config.settings import DatabaseSettings
from darkula.domain.identifiers import (
    CandidateEventId,
    ReconAssessmentId,
    SourceAssessmentId,
    SourceCandidateId,
    SourceEndpointId,
    SourceId,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    Confidence,
    EndpointStatus,
    EndpointType,
    ReconAssessment,
    ReconDisposition,
    Source,
    SourceAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
    SourceEndpoint,
    SourceStatus,
)
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from tests.integration.conftest import _raw_connect, row_values

pytestmark = pytest.mark.integration


_MOMENT = datetime(2025, 2, 3, 4, 5, 6, tzinfo=UTC)


@pytest.mark.asyncio
async def test_canonical_vertical_slice(
    spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
) -> None:
    # 1. create SourceCandidate + commit
    candidate = SourceCandidate(
        candidate_id=SourceCandidateId.generate(),
        discovered_at=_MOMENT,
        discovery_method="curated-list",
        entrypoint="http://forum.example.invalid/",
        status=CandidateStatus.DISCOVERED,
        discovery_context={"region": "sample"},
    )
    discovery_event = SourceCandidateEventHistory(
        event_id=CandidateEventId.generate(),
        candidate_id=candidate.candidate_id,
        event_type=CandidateEventType.DISCOVERED,
        occurred_at=_MOMENT,
        context={"channel": "seed-list"},
        reason="listed in curated seed",
        provenance="integration",
    )
    async with spi.unit_of_work() as uow:
        await uow.source_candidates.create(candidate, event=discovery_event)
        await uow.commit()

    # 2. new UoW: atomic transition + recon assessment + commit
    transition_event = SourceCandidateEventHistory(
        event_id=CandidateEventId.generate(),
        candidate_id=candidate.candidate_id,
        event_type=CandidateEventType.QUALIFIED,
        occurred_at=_MOMENT + timedelta(minutes=5),
        context={"stage": "first-pass"},
        reason="recon completed",
        provenance="integration",
    )
    recon = ReconAssessment(
        assessment_id=ReconAssessmentId.generate(),
        candidate_id=candidate.candidate_id,
        assessed_at=_MOMENT + timedelta(minutes=6),
        disposition=ReconDisposition.QUALIFY,
        confidence=Confidence(0.9),
        evidence_references=("recon-session-1",),
        characteristics={"accessibility": "open", "language": "en"},
    )
    async with spi.unit_of_work() as uow:
        await uow.source_candidates.transition(
            candidate.candidate_id,
            expected=CandidateStatus.DISCOVERED,
            new_status=CandidateStatus.QUALIFIED,
            event=transition_event,
        )
        await uow.source_candidates.append_recon_assessment(recon)
        await uow.commit()

    # 3. new UoW: read candidate, history, recon history
    async with spi.unit_of_work() as uow:
        read_candidate = await uow.source_candidates.get(candidate.candidate_id)
        history = await uow.source_candidates.list_history(candidate.candidate_id)
        recon_history = await uow.source_candidates.list_recon_assessments(
            candidate.candidate_id
        )
    assert read_candidate is not None
    assert read_candidate.status is CandidateStatus.QUALIFIED
    assert [e.event_type for e in history] == [
        CandidateEventType.DISCOVERED,
        CandidateEventType.QUALIFIED,
    ]
    assert len(recon_history) == 1
    assert recon_history[0].disposition is ReconDisposition.QUALIFY

    # 4. create Source (not the candidate; distinct identity), add endpoint,
    #    append source assessment + commit
    source = Source(
        source_id=SourceId.generate(),
        status=SourceStatus.ACTIVE,
        created_at=_MOMENT,
        updated_at=_MOMENT,
        name="example-monitored-source",
    )
    endpoint = SourceEndpoint(
        endpoint_id=SourceEndpointId.generate(),
        source_id=source.source_id,
        uri=candidate.entrypoint + "index",
        endpoint_type=EndpointType.CLEARNET,
        status=EndpointStatus.ACTIVE,
        first_observed_at=_MOMENT,
        last_observed_at=_MOMENT,
        metadata={"kind": "index"},
    )
    assessment = SourceAssessment(
        assessment_id=SourceAssessmentId.generate(),
        source_id=source.source_id,
        assessed_at=_MOMENT,
        window_start=_MOMENT - timedelta(days=7),
        window_end=_MOMENT,
        confidence=Confidence(0.8),
        relevance=Confidence(0.7),
        activity=Confidence(0.5),
        novelty=Confidence(0.4),
        evidence_references=("analysis-1",),
        characteristics={"focus": "marketplace-focus"},
    )
    async with spi.unit_of_work() as uow:
        await uow.sources.create(source)
        await uow.sources.add_endpoint(endpoint)
        await uow.sources.append_source_assessment(assessment)
        await uow.commit()

    # 5. new UoW: read source, endpoints, assessment history
    async with spi.unit_of_work() as uow:
        read_source = await uow.sources.get(source.source_id)
        endpoints = await uow.sources.list_endpoints(source.source_id)
        assessments = await uow.sources.list_source_assessments(source.source_id)
    assert read_source is not None
    assert read_source.source_id == source.source_id
    assert [e.endpoint_id for e in endpoints] == [endpoint.endpoint_id]
    assert assessments[0].assessment_id == assessment.assessment_id

    # 6. test-only SQL independently confirms durable state + constraints
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM source_candidate WHERE id = %s",
                (str(candidate.candidate_id),),
            )
            assert row_values(cur, cur.fetchone())[0] == "QUALIFIED"
            cur.execute(
                "SELECT count(*) FROM source_candidate_event_history "
                "WHERE candidate_id = %s",
                (str(candidate.candidate_id),),
            )
            assert row_values(cur, cur.fetchone())[0] == 2
            cur.execute(
                "SELECT count(*) FROM source_endpoint WHERE source_id = %s",
                (str(source.source_id),),
            )
            assert row_values(cur, cur.fetchone())[0] == 1
    finally:
        conn.close()
