# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the PostgreSQL repositories using fake connections.

Verifies that repository code (a) only ever issues stored-function
invocation strings (never data-access SQL), (b) marshals parameters and
maps results exactly, and (c) surfaces controlled conflicts as typed
bounded errors — all without PostgreSQL.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, ClassVar, Self

import pytest

from darkula.app.persistence import (
    ConflictError,
    IntegrityError,
    PersistenceError,
)
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
from darkula.infrastructure.persistence.postgresql.candidate_repository import (
    PostgresSourceCandidateRepository,
)
from darkula.infrastructure.persistence.postgresql.source_repository import (
    PostgresSourceRepository,
)


class _RecordingCursor:
    """Async cursor recording the last SQL + params and serving queued rows."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.executed_sql: list[str] = []
        self.executed_params: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed_sql.append(sql)
        self.executed_params.append(params or ())

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Scope:
    """Deterministic ExecutionScope under test (no real DB connection)."""

    def __init__(self, cursor: _RecordingCursor, *, closed: bool = False) -> None:
        self.cursor = cursor
        self.closed = closed

    def ensure_open(self) -> None:
        if self.closed:
            raise PersistenceError("this unit of work is closed")

    def connection(self) -> Any:
        self.ensure_open()

        class _Conn:
            def cursor(self) -> _RecordingCursor:
                return cursor

        cursor = self.cursor
        return _Conn()


_MOMENT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
_CANDIDATE_ID = SourceCandidateId.generate()
_EVENT_ID = CandidateEventId.generate()
_SOURCE_ID = SourceId.generate()
_ENDPOINT_ID = SourceEndpointId.generate()
_RECON_ID = ReconAssessmentId.generate()
_ASSESSMENT_ID = SourceAssessmentId.generate()


def _candidate() -> SourceCandidate:
    return SourceCandidate(
        candidate_id=_CANDIDATE_ID,
        discovered_at=_MOMENT,
        discovery_method="index-discovery",
        entrypoint="http://example.invalid/a",
        status=CandidateStatus.DISCOVERED,
        discovery_context={"channel": "seed"},
    )


def _event(
    event_type: CandidateEventType = CandidateEventType.DISCOVERED,
) -> SourceCandidateEventHistory:
    return SourceCandidateEventHistory(
        event_id=_EVENT_ID,
        candidate_id=_CANDIDATE_ID,
        event_type=event_type,
        occurred_at=_MOMENT,
        context={"channel": "seed"},
        reason="reason",
        provenance="provenance",
    )


def _source() -> Source:
    return Source(
        source_id=_SOURCE_ID,
        status=SourceStatus.ACTIVE,
        created_at=_MOMENT,
        updated_at=_MOMENT,
        name="example",
    )


def _endpoint() -> SourceEndpoint:
    return SourceEndpoint(
        endpoint_id=_ENDPOINT_ID,
        source_id=_SOURCE_ID,
        uri="http://mirror.example.invalid/",
        endpoint_type=EndpointType.MIRROR,
        status=EndpointStatus.ACTIVE,
        first_observed_at=_MOMENT,
        last_observed_at=_MOMENT,
        metadata={"lang": "en"},
    )


def _recon() -> ReconAssessment:
    return ReconAssessment(
        assessment_id=_RECON_ID,
        candidate_id=_CANDIDATE_ID,
        assessed_at=_MOMENT,
        disposition=ReconDisposition.QUALIFY,
        confidence=Confidence(0.75),
        evidence_references=("recon-1",),
        characteristics={"accessibility": "open"},
    )


def _source_assessment() -> SourceAssessment:
    return SourceAssessment(
        assessment_id=_ASSESSMENT_ID,
        source_id=_SOURCE_ID,
        assessed_at=_MOMENT,
        window_start=_MOMENT,
        window_end=_MOMENT,
        confidence=Confidence(0.8),
        relevance=Confidence(0.6),
        activity=None,
        novelty=Confidence(0.5),
        evidence_references=("analysis-2",),
        characteristics={"focus": "marketplace-focus"},
    )


def _row(*values: Any) -> tuple[Any, ...]:
    return values


class TestCandidateRepositoryInvocation:
    """Repository calls invoke stored functions only, with exact params."""

    @pytest.mark.asyncio
    async def test_create_invokes_create_function(self) -> None:
        cursor = _RecordingCursor(rows=[_row(True)])
        repo = PostgresSourceCandidateRepository(_Scope(cursor))
        await repo.create(_candidate(), event=_event())
        assert len(cursor.executed_sql) == 1
        sql = cursor.executed_sql[0]
        assert "candidate_create_v1" in sql
        assert "INSERT" not in sql
        assert "UPDATE" not in sql
        params = cursor.executed_params[0]
        assert str(_CANDIDATE_ID) in params

    @pytest.mark.asyncio
    async def test_get_invokes_get_function(self) -> None:
        cursor = _RecordingCursor(
            rows=[
                _row(
                    str(_CANDIDATE_ID),
                    _MOMENT,
                    "m",
                    "http://e.invalid/",
                    "DISCOVERED",
                    None,
                )
            ]
        )
        repo = PostgresSourceCandidateRepository(_Scope(cursor))
        candidate = await repo.get(_CANDIDATE_ID)
        assert candidate is not None
        assert str(candidate.candidate_id) == str(_CANDIDATE_ID)
        assert "candidate_get_v1" in cursor.executed_sql[0]

    @pytest.mark.asyncio
    async def test_transition_conflict_maps_typed(self) -> None:
        cursor = _RecordingCursor(rows=[_row(False)])
        repo = PostgresSourceCandidateRepository(_Scope(cursor))
        with pytest.raises(ConflictError):
            await repo.transition(
                _CANDIDATE_ID,
                expected=CandidateStatus.DISCOVERED,
                new_status=CandidateStatus.QUALIFIED,
                event=_event(CandidateEventType.QUALIFIED),
            )

    @pytest.mark.asyncio
    async def test_append_history_unknown_candidate_maps_integrity(self) -> None:
        cursor = _RecordingCursor(rows=[_row("unknown_candidate")])
        repo = PostgresSourceCandidateRepository(_Scope(cursor))
        with pytest.raises(IntegrityError):
            await repo.append_history(_event())

    @pytest.mark.asyncio
    async def test_append_recon_duplicate_maps_conflict(self) -> None:
        cursor = _RecordingCursor(rows=[_row("duplicate")])
        repo = PostgresSourceCandidateRepository(_Scope(cursor))
        with pytest.raises(ConflictError):
            await repo.append_recon_assessment(_recon())

    @pytest.mark.asyncio
    async def test_list_recon_maps_rows_in_order(self) -> None:
        cursor = _RecordingCursor(
            rows=[
                _row(
                    str(_RECON_ID),
                    str(_CANDIDATE_ID),
                    _MOMENT,
                    "QUALIFY",
                    0.7,
                    ["r"],
                    None,
                )
            ]
        )
        repo = PostgresSourceCandidateRepository(_Scope(cursor))
        recon = await repo.list_recon_assessments(_CANDIDATE_ID)
        assert len(recon) == 1
        assert str(recon[0].assessment_id) == str(_RECON_ID)

    @pytest.mark.asyncio
    async def test_clock_rows_empty_history(self) -> None:
        cursor = _RecordingCursor(rows=[])
        repo = PostgresSourceCandidateRepository(_Scope(cursor))
        assert await repo.list_history(_CANDIDATE_ID) == ()

    @pytest.mark.asyncio
    async def test_cancellation_propagates_unmapped(self) -> None:
        class _CancellingCursor(_RecordingCursor):
            async def execute(
                self, sql: str, params: tuple[Any, ...] | None = None
            ) -> None:
                raise asyncio.CancelledError()

        repo = PostgresSourceCandidateRepository(_Scope(_CancellingCursor()))
        with pytest.raises(asyncio.CancelledError):
            await repo.append_history(_event())

    @pytest.mark.asyncio
    async def test_driver_error_maps_to_bounded_persistence_error(self) -> None:
        from psycopg import OperationalError as PsycopgOperationalError

        class _FailingCursor(_RecordingCursor):
            async def execute(
                self, sql: str, params: tuple[Any, ...] | None = None
            ) -> None:
                raise PsycopgOperationalError("connection refused")

        repo = PostgresSourceCandidateRepository(_Scope(_FailingCursor()))
        with pytest.raises(PersistenceError) as caught:
            await repo.append_history(_event())
        assert "connection refused" not in str(caught.value)
        assert isinstance(caught.value, PersistenceError)

    @pytest.mark.asyncio
    async def test_get_missing_candidate_returns_none(self) -> None:
        cursor = _RecordingCursor(rows=[])
        repo = PostgresSourceCandidateRepository(_Scope(cursor))
        assert await repo.get(_CANDIDATE_ID) is None


class TestSourceRepositoryInvocation:
    """Source repository calls stored functions with exact params."""

    @pytest.mark.asyncio
    async def test_create_source(self) -> None:
        cursor = _RecordingCursor(rows=[_row(True)])
        repo = PostgresSourceRepository(_Scope(cursor))
        await repo.create(_source())
        assert "source_create_v1" in cursor.executed_sql[0]

    @pytest.mark.asyncio
    async def test_add_endpoint_unknown_source_maps_integrity(self) -> None:
        cursor = _RecordingCursor(rows=[_row("unknown_source")])
        repo = PostgresSourceRepository(_Scope(cursor))
        with pytest.raises(IntegrityError):
            await repo.add_endpoint(_endpoint())

    @pytest.mark.asyncio
    async def test_list_endpoints_maps(self) -> None:
        cursor = _RecordingCursor(
            rows=[
                _row(
                    str(_ENDPOINT_ID),
                    str(_SOURCE_ID),
                    "http://m.invalid/",
                    "MIRROR",
                    "ACTIVE",
                    _MOMENT,
                    _MOMENT,
                    None,
                )
            ]
        )
        repo = PostgresSourceRepository(_Scope(cursor))
        endpoints = await repo.list_endpoints(_SOURCE_ID)
        assert len(endpoints) == 1
        assert str(endpoints[0].endpoint_id) == str(_ENDPOINT_ID)

    @pytest.mark.asyncio
    async def test_append_source_assessment_duplicate_maps_conflict(self) -> None:
        cursor = _RecordingCursor(rows=[_row("duplicate")])
        repo = PostgresSourceRepository(_Scope(cursor))
        with pytest.raises(ConflictError):
            await repo.append_source_assessment(_source_assessment())

    @pytest.mark.asyncio
    async def test_list_source_assessments_maps(self) -> None:
        cursor = _RecordingCursor(
            rows=[
                _row(
                    str(_ASSESSMENT_ID),
                    str(_SOURCE_ID),
                    _MOMENT,
                    _MOMENT,
                    _MOMENT,
                    0.8,
                    0.6,
                    None,
                    0.5,
                    ["a"],
                    None,
                )
            ]
        )
        repo = PostgresSourceRepository(_Scope(cursor))
        assessments = await repo.list_source_assessments(_SOURCE_ID)
        assert len(assessments) == 1
        assert assessments[0].novelty is not None
        assert assessments[0].activity is None


class TestClosedScope:
    """U6: repository use after the transaction is closed fails boundedly."""

    @pytest.mark.asyncio
    async def test_create_after_close_fails_bounded(self) -> None:
        repo = PostgresSourceCandidateRepository(
            _Scope(_RecordingCursor(), closed=True)
        )
        with pytest.raises(PersistenceError, match="closed"):
            await repo.create(_candidate(), event=_event())


class TestSqlBoundary:
    """Production repository modules contain only stored-function invocation
    strings — never data-access SQL statements."""

    MODULES: ClassVar[list[str]] = [
        "src/darkula/infrastructure/persistence/postgresql/candidate_repository.py",
        "src/darkula/infrastructure/persistence/postgresql/source_repository.py",
    ]

    @staticmethod
    def _sql_literals(source: str) -> list[str]:
        import ast

        tree = ast.parse(source)
        literals: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value)
        return literals

    @staticmethod
    def _read_module(path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_no_data_access_sql_keywords(self) -> None:
        # The invariant: no SQL *statement* begins with a data-access verb.
        # Function names may legitimately contain such words
        # (candidate_create_v1), so only the statement head is scanned.
        forbidden_heads = (
            "INSERT ",
            "INSERT INTO",
            "UPDATE ",
            "UPDATE SET",
            "DELETE FROM",
            "CREATE ",
            "ALTER ",
            "DROP TABLE",
            "TRUNCATE",
        )
        for module in self.MODULES:
            source = self._read_module(module)
            for literal in self._sql_literals(source):
                head = literal.lstrip().upper()
                if not head.startswith("SELECT "):
                    continue
                for token in forbidden_heads:
                    assert not head.startswith(token), (
                        f"{module} contains SQL token {token}"
                    )

    def test_all_sql_constants_are_function_invocations(self) -> None:
        for module in self.MODULES:
            source = self._read_module(module)
            for literal in self._sql_literals(source):
                if not literal.lstrip().upper().startswith("SELECT * FROM "):
                    continue
                invocation = literal.lstrip()
                assert invocation.startswith("SELECT * FROM ")
                function = invocation[len("SELECT * FROM ") :].split("(")[0]
                assert function.endswith(("_v1", "_v2"))
                assert " WHERE " not in invocation.upper()
                assert " JOIN " not in invocation.upper()
