# SPDX-License-Identifier: AGPL-3.0-only
"""Source repository integration matrix (S1-S8) over real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from darkula.app.persistence import ConflictError, IntegrityError
from darkula.config.settings import DatabaseSettings
from darkula.domain.identifiers import (
    SourceAssessmentId,
    SourceEndpointId,
    SourceId,
)
from darkula.domain.source import (
    Confidence,
    EndpointStatus,
    EndpointType,
    Source,
    SourceAssessment,
    SourceEndpoint,
    SourceStatus,
)
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from tests.integration.conftest import _raw_connect, row_values

pytestmark = pytest.mark.integration


_MOMENT = datetime(2025, 2, 3, 4, 5, 6, tzinfo=UTC)


def _source(*, n: int = 0) -> Source:
    return Source(
        source_id=SourceId.generate(),
        status=SourceStatus.ACTIVE,
        created_at=_MOMENT,
        updated_at=_MOMENT,
        name=f"source-{n}",
    )


def _endpoint(*, n: int = 0, source_id: SourceId | None = None) -> SourceEndpoint:
    return SourceEndpoint(
        endpoint_id=SourceEndpointId.generate(),
        source_id=source_id or SourceId.generate(),
        uri=f"http://mirror.example.invalid/{n}",
        endpoint_type=EndpointType.MIRROR,
        status=EndpointStatus.ACTIVE,
        first_observed_at=_MOMENT,
        last_observed_at=_MOMENT,
        metadata={"n": n},
    )


def _assessment(*, n: int = 0, source_id: SourceId | None = None) -> SourceAssessment:
    return SourceAssessment(
        assessment_id=SourceAssessmentId.generate(),
        source_id=source_id or SourceId.generate(),
        assessed_at=_MOMENT,
        window_start=_MOMENT - timedelta(days=30),
        window_end=_MOMENT,
        confidence=Confidence(0.8),
        relevance=Confidence(0.6),
        activity=None,
        novelty=Confidence(0.5),
        evidence_references=(f"analysis-{n}",),
        characteristics={"focus": "marketplace-focus"},
    )


class TestSourceCreateRead:
    """S1/S3/S4: create, retrieve, duplicate conflict."""

    @pytest.mark.asyncio
    async def test_s1_create_commit_then_retrieve(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source = _source()
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            read = await uow.sources.get(source.source_id)
            assert read is not None
            assert read.source_id == source.source_id
            assert read.status is SourceStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_s1_create_uncommitted_absent(self, spi: PostgresDarkulaSpi) -> None:
        source = _source()
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
        async with spi.unit_of_work() as uow:
            assert await uow.sources.get(source.source_id) is None

    @pytest.mark.asyncio
    async def test_duplicate_source_id_conflict(self, spi: PostgresDarkulaSpi) -> None:
        source = _source()
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            with pytest.raises(ConflictError):
                await uow.sources.create(source)
            await uow.commit()


class TestEndpoints:
    """S2/S3/S4/S5: endpoints belong to a source; rotation preserves identity."""

    @pytest.mark.asyncio
    async def test_s2_add_endpoint_belongs_to_source(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source = _source()
        endpoint = _endpoint(source_id=source.source_id)
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.sources.add_endpoint(endpoint)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            endpoints = await uow.sources.list_endpoints(source.source_id)
        assert len(endpoints) == 1
        assert endpoints[0].source_id == source.source_id
        assert endpoints[0].uri == endpoint.uri

    @pytest.mark.asyncio
    async def test_s3_endpoint_for_missing_source_is_integrity(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.sources.add_endpoint(_endpoint())
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_s4_multiple_endpoints_deterministic_order(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source = _source()
        endpoint_ids = [SourceEndpointId.generate() for _ in range(3)]
        endpoints = [
            SourceEndpoint(
                endpoint_id=endpoint_ids[n],
                source_id=source.source_id,
                uri=f"http://m.example.invalid/{n}",
                endpoint_type=EndpointType.ONION,
                status=EndpointStatus.ACTIVE,
                first_observed_at=_MOMENT,
                last_observed_at=_MOMENT,
            )
            for n in range(3)
        ]
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            for ep in endpoints:
                await uow.sources.add_endpoint(ep)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            listed = await uow.sources.list_endpoints(source.source_id)
        # Deterministic by endpoint_id (stable tie-breaker).
        assert [ep.endpoint_id for ep in listed] == sorted(endpoint_ids, key=str)

    @pytest.mark.asyncio
    async def test_s5_rotate_endpoint_source_identity_unchanged(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source = _source()
        first = _endpoint(n=0, source_id=source.source_id)
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.sources.add_endpoint(first)
            await uow.commit()

        replacement = _endpoint(n=1, source_id=source.source_id)
        async with spi.unit_of_work() as uow:
            await uow.sources.add_endpoint(replacement)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            read = await uow.sources.get(source.source_id)
            endpoints = await uow.sources.list_endpoints(source.source_id)
        assert read is not None
        assert read.source_id == source.source_id
        assert {ep.endpoint_id for ep in endpoints} == {
            first.endpoint_id,
            replacement.endpoint_id,
        }


class TestSourceAssessments:
    """S6/S7/S8: assessments are append-only and deterministic."""

    @pytest.mark.asyncio
    async def test_s6_append_retrieve_assessment(self, spi: PostgresDarkulaSpi) -> None:
        source = _source()
        assessment = _assessment(source_id=source.source_id)
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.sources.append_source_assessment(assessment)
            await uow.commit()

        async with spi.unit_of_work() as uow:
            listed = await uow.sources.list_source_assessments(source.source_id)
        assert len(listed) == 1
        assert listed[0].assessment_id == assessment.assessment_id

    @pytest.mark.asyncio
    async def test_s7_multiple_assessments_deterministic_order(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source = _source()
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.commit()

        assessments = [_assessment(n=n, source_id=source.source_id) for n in (2, 0, 1)]
        for assessment in assessments:
            async with spi.unit_of_work() as uow:
                await uow.sources.append_source_assessment(assessment)
                await uow.commit()

        async with spi.unit_of_work() as uow:
            listed = await uow.sources.list_source_assessments(source.source_id)
        # Deterministic by (assessed_at, assessment_id).
        sorted_assessments = sorted(
            assessments, key=lambda a: (a.assessed_at, str(a.assessment_id))
        )
        assert [a.assessment_id for a in listed] == [
            a.assessment_id for a in sorted_assessments
        ]

    @pytest.mark.asyncio
    async def test_s8_rollback_assessment_absent(
        self, spi: PostgresDarkulaSpi, database_settings: DatabaseSettings
    ) -> None:
        source = _source()
        async with spi.unit_of_work() as uow:
            await uow.sources.create(source)
            await uow.commit()

        assessment = _assessment(source_id=source.source_id)
        async with spi.unit_of_work() as uow:
            await uow.sources.append_source_assessment(assessment)
            await uow.rollback()

        conn = _raw_connect(database_settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM source_assessment WHERE source_id = %s",
                    (str(source.source_id),),
                )
                assert row_values(cur, cur.fetchone())[0] == 0
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_assessment_for_missing_source_is_integrity(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        async with spi.unit_of_work() as uow:
            with pytest.raises(IntegrityError):
                await uow.sources.append_source_assessment(_assessment())
            await uow.rollback()
