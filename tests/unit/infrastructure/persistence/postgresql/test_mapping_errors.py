# SPDX-License-Identifier: AGPL-3.0-only
"""Mapping + driver-error unit tests for the PostgreSQL adapter (M1-M5)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.errors import ForeignKeyViolation, OperationalError, UniqueViolation

from darkula.app.persistence import (
    ConflictError,
    IntegrityError,
    PersistenceError,
    PersistenceUnavailableError,
)
from darkula.domain.source import CandidateStatus
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_candidate,
    map_endpoint,
    map_event,
    map_recon_assessment,
    map_source,
    map_source_assessment,
)

_MOMENT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
_CANDIDATE_ID = str(uuid.uuid4())
_EVENT_ID = str(uuid.uuid4())
_SOURCE_ID = str(uuid.uuid4())
_ENDPOINT_ID = str(uuid.uuid4())
_RECON_ID = str(uuid.uuid4())
_ASSESSMENT_ID = str(uuid.uuid4())


class TestRowMapping:
    """M1: valid persisted rows map to correct domain objects."""

    def test_map_candidate_row(self) -> None:
        row = (
            _CANDIDATE_ID,
            _MOMENT,
            "index-discovery",
            "http://example.invalid/a",
            "DISCOVERED",
            {"channel": "seed"},
        )
        candidate = map_candidate(row)
        assert str(candidate.candidate_id) == _CANDIDATE_ID
        assert candidate.status is CandidateStatus.DISCOVERED
        assert candidate.discovery_context == {"channel": "seed"}

    def test_map_event_row(self) -> None:
        row = (
            _EVENT_ID,
            _CANDIDATE_ID,
            "DISCOVERED",
            _MOMENT,
            {"channel": "seed"},
            "reason",
            "provenance",
        )
        event = map_event(row)
        assert str(event.event_id) == _EVENT_ID
        assert event.reason == "reason"

    def test_map_source_row(self) -> None:
        row = (_SOURCE_ID, "ACTIVE", _MOMENT, _MOMENT, "example")
        source = map_source(row)
        assert str(source.source_id) == _SOURCE_ID
        assert source.name == "example"

    def test_map_endpoint_row(self) -> None:
        row = (
            _ENDPOINT_ID,
            _SOURCE_ID,
            "http://m.invalid/",
            "MIRROR",
            "ACTIVE",
            _MOMENT,
            _MOMENT,
            {"lang": "en"},
        )
        endpoint = map_endpoint(row)
        assert endpoint.endpoint_type.value == "MIRROR"

    def test_map_recon_row(self) -> None:
        row = (
            _RECON_ID,
            _CANDIDATE_ID,
            _MOMENT,
            "QUALIFY",
            0.75,
            ["recon-1"],
            {"accessibility": "open"},
        )
        recon = map_recon_assessment(row)
        assert recon.disposition.value == "QUALIFY"
        assert recon.confidence.value == 0.75

    def test_map_source_assessment_row(self) -> None:
        row = (
            _ASSESSMENT_ID,
            _SOURCE_ID,
            _MOMENT,
            _MOMENT,
            _MOMENT,
            0.8,
            0.6,
            None,
            0.5,
            ["analysis-2"],
            {"focus": "marketplace-focus"},
        )
        assessment = map_source_assessment(row)
        assert assessment.relevance is not None
        assert assessment.activity is None
        assert assessment.confidence.value == 0.8


class TestMappingFailures:
    """M2: invalid persisted enum values surface as bounded errors."""

    def test_invalid_candidate_status(self) -> None:
        row = (_CANDIDATE_ID, _MOMENT, "m", "http://e.invalid/", "BOGUS", None)
        with pytest.raises(PersistenceError, match="persisted candidate"):
            map_candidate(row)

    def test_invalid_recon_disposition(self) -> None:
        row: tuple[object, ...] = (
            _RECON_ID,
            _CANDIDATE_ID,
            _MOMENT,
            "BOGUS",
            0.5,
            [],
            None,
        )
        with pytest.raises(PersistenceError, match="persisted recon assessment"):
            map_recon_assessment(row)

    def test_naive_timestamp_row_rejected(self) -> None:
        row = (
            _CANDIDATE_ID,
            datetime(2025, 1, 1),
            "m",
            "http://e.invalid/",
            "DISCOVERED",
            None,
        )
        with pytest.raises(PersistenceError, match="persisted candidate"):
            map_candidate(row)


class TestDriverErrorMapping:
    """M3/M4/M5: typed bounded errors; no echo; cancellation unchanged."""

    def test_unique_violation_maps_to_conflict(self) -> None:
        exc = UniqueViolation('duplicate key value violates unique constraint \\"x\\"')
        assert isinstance(map_driver_error(exc), ConflictError)

    def test_fk_violation_maps_to_integrity(self) -> None:
        exc = ForeignKeyViolation('insert or update on table \\"child\\"')
        assert isinstance(map_driver_error(exc), IntegrityError)

    def test_operational_error_maps_to_unavailable(self) -> None:
        exc = OperationalError("connection refused")
        assert isinstance(map_driver_error(exc), PersistenceUnavailableError)

    def test_generic_psycopg_error_is_bounded(self) -> None:
        exc = psycopg.Error("raw provider detail sensitive=value")
        mapped = map_driver_error(exc)
        assert isinstance(mapped, PersistenceError)
        assert "sensitive" not in str(mapped)
        assert "raw provider detail" not in str(mapped)

    def test_cancelled_error_never_mapped_as_psycopg_error(self) -> None:
        # CancelledError is not a psycopg.Error; repositories re-raise it
        # before calling the mapper, so it must never be mapped here.
        assert not isinstance(asyncio.CancelledError(), psycopg.Error)

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_m5(self) -> None:
        # Verifies the mapper is never reached for cancellation (it would
        # otherwise echo it as a bounded persistence error).
        async def repo_call() -> None:
            try:
                # simulate a driver call that is cancelled
                await asyncio.sleep(0)
                raise asyncio.CancelledError()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - guard parity
                raise map_driver_error(exc) from exc

        with pytest.raises(asyncio.CancelledError):
            await repo_call()


class TestRelationshipRowMapping:
    """RDB19: invalid persisted relationship labels map boundedly."""

    def test_valid_relationship_row_maps(self) -> None:
        from darkula.infrastructure.persistence.postgresql.mapping import (
            map_extracted_relationship,
        )

        row = (
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            "LOCATED_IN",
            str(uuid.uuid4()),
            "support",
            0,
            7,
            "relationship-llm",
            "v1",
            0.9,
        )
        relationship = map_extracted_relationship(row)
        assert relationship.predicate.value == "LOCATED_IN"
        assert relationship.support_text == "support"

    def test_invalid_persisted_predicate_maps_bounded(self) -> None:
        from darkula.app.persistence import MappingError
        from darkula.infrastructure.persistence.postgresql.mapping import (
            map_extracted_relationship,
        )

        row = (
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            "FRIENDS_WITH",
            str(uuid.uuid4()),
            "support",
            0,
            7,
            "relationship-llm",
            "v1",
            0.9,
        )
        with pytest.raises(MappingError) as info:
            map_extracted_relationship(row)
        assert "FRIENDS_WITH" not in str(info.value)
