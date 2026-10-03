# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the PostgresUnitOfWork state machine (U1-U7).

The real unit of work is driven with a deterministic fake connection/pool so
the offline suite covers commit/rollback/uncommitted-exit/error/
cancellation behavior without PostgreSQL.
"""

from __future__ import annotations

import asyncio
from typing import Any, Self

import pytest

from darkula.app.persistence import PersistenceError
from darkula.infrastructure.persistence.postgresql.unit_of_work import (
    PostgresUnitOfWork,
)


class _FakeCursor:
    """Minimal async cursor accepting any SQL and returning nothing."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.last_sql = sql
        self.last_params = params

    async def fetchone(self) -> tuple[Any, ...] | None:
        return ("ok",)


class _FakeConnection:
    """Records commit/rollback/cursor operations on one fake connection.

    Cancellation is simulated by raising CancelledError on demand.
    """

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.fail_commit = False
        self.cancel_on_commit = False
        self.cancel_on_rollback = False

    async def commit(self) -> None:
        if self.cancel_on_commit:
            raise asyncio.CancelledError()
        if self.fail_commit:
            raise RuntimeError("commit exploded")
        self.commits += 1

    async def rollback(self) -> None:
        if self.cancel_on_rollback:
            raise asyncio.CancelledError()
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakePool:
    """Pool yielding one fake connection per context-manager entry."""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection
        self.releases = 0

    def connection(self) -> _FakeConnection:
        return self._connection

    @property
    def wrapped(self) -> _FakeConnection:
        return self._connection


def _uow(pool: _FakePool) -> PostgresUnitOfWork:
    return PostgresUnitOfWork(pool=pool)  # type: ignore[arg-type]


class TestCommit:
    """U1: explicit commit persists exactly once."""

    @pytest.mark.asyncio
    async def test_explicit_commit_once(self) -> None:
        pool = _FakePool(_FakeConnection())
        uow = _uow(pool)
        async with uow:
            pass
            await uow.commit()
        assert pool.wrapped.commits == 1
        assert pool.wrapped.rollbacks == 0

    @pytest.mark.asyncio
    async def test_double_commit_fails_bounded(self) -> None:
        uow = _uow(_FakePool(_FakeConnection()))
        async with uow:
            await uow.commit()
            with pytest.raises(PersistenceError, match="already committed"):
                await uow.commit()
        assert isinstance(uow, PostgresUnitOfWork)

    @pytest.mark.asyncio
    async def test_commit_before_open_fails_bounded(self) -> None:
        uow = _uow(_FakePool(_FakeConnection()))
        with pytest.raises(PersistenceError, match="closed"):
            await uow.commit()


class TestRollback:
    """U2/U3/U4: rollback on exception, uncommitted exit, explicit call."""

    @pytest.mark.asyncio
    async def test_exception_before_commit_rolls_back(self) -> None:
        pool = _FakePool(_FakeConnection())
        uow = _uow(pool)
        with pytest.raises(RuntimeError, match="boom"):
            async with uow:
                raise RuntimeError("boom")
        assert pool.wrapped.rollbacks == 1
        assert pool.wrapped.commits == 0

    @pytest.mark.asyncio
    async def test_normal_uncommitted_exit_rolls_back(self) -> None:
        pool = _FakePool(_FakeConnection())
        uow = _uow(pool)
        async with uow:
            pass
        assert pool.wrapped.rollbacks == 1
        assert pool.wrapped.commits == 0

    @pytest.mark.asyncio
    async def test_explicit_rollback_once(self) -> None:
        pool = _FakePool(_FakeConnection())
        uow = _uow(pool)
        async with uow:
            await uow.rollback()
        # rollback happened once explicitly; exit must not roll back again
        assert pool.wrapped.rollbacks == 1

    @pytest.mark.asyncio
    async def test_rollback_after_commit_is_resolved_noop(self) -> None:
        pool = _FakePool(_FakeConnection())
        uow = _uow(pool)
        async with uow:
            await uow.commit()
            await uow.rollback()
        assert pool.wrapped.rollbacks == 0


class TestCancellation:
    """U5: asyncio.CancelledError propagates unchanged and rolls back."""

    @pytest.mark.asyncio
    async def test_cancellation_in_body_propagates(self) -> None:
        pool = _FakePool(_FakeConnection())
        uow = _uow(pool)
        with pytest.raises(asyncio.CancelledError):
            async with uow:
                raise asyncio.CancelledError()
        assert pool.wrapped.rollbacks == 1

    @pytest.mark.asyncio
    async def test_cancellation_during_commit_propagates(self) -> None:
        conn = _FakeConnection()
        conn.cancel_on_commit = True
        pool = _FakePool(conn)
        uow = _uow(pool)
        async with uow:
            with pytest.raises(asyncio.CancelledError):
                await uow.commit()
        # cancellation must never be mapped to a bounded persistence error
        assert not isinstance(asyncio.CancelledError(), PersistenceError)

    @pytest.mark.asyncio
    async def test_commit_failure_is_bounded_and_never_claims_committed(self) -> None:
        conn = _FakeConnection()
        conn.fail_commit = True
        pool = _FakePool(conn)
        uow = _uow(pool)
        async with uow:
            with pytest.raises(PersistenceError, match="failed"):
                await uow.commit()
        assert conn.commits == 0
        assert isinstance(uow, PostgresUnitOfWork)

    @pytest.mark.asyncio
    async def test_commit_failure_with_driver_error_is_bounded(self) -> None:
        from darkula.app.persistence import PersistenceError as PE

        class _BrokenConnection(_FakeConnection):
            fail_commit = True

            async def commit(self) -> None:
                raise RuntimeError("raw driver detail access=denied")

        pool = _FakePool(_BrokenConnection())
        uow = _uow(pool)
        async with uow:
            with pytest.raises(PE) as caught:
                await uow.commit()
        assert "access=denied" not in str(caught.value)
        assert "raw driver" not in str(caught.value)


class TestClosedExits:
    """U6: repository/connection use after close fails boundedly."""

    @pytest.mark.asyncio
    async def test_repository_access_after_close_fails_bounded(self) -> None:
        uow = _uow(_FakePool(_FakeConnection()))
        with pytest.raises(PersistenceError, match="closed"):
            _ = uow.source_candidates


class TestSharedTransaction:
    """U7: both repositories are bound to the same unit of work."""

    @pytest.mark.asyncio
    async def test_two_repositories_same_transaction(self) -> None:
        uow = _uow(_FakePool(_FakeConnection()))
        async with uow:
            candidates = uow.source_candidates
            sources = uow.sources
            assert candidates is uow.source_candidates
            assert sources is uow.sources
            assert type(candidates).__name__ != type(sources).__name__
