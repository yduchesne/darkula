# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL UnitOfWork: one explicit transaction (PR 4).

One :class:`PostgresUnitOfWork` owns exactly one connection borrowed from
the bounded pool and one open database transaction for its lifetime:

- ``__aenter__`` acquires a connection and begins the implicit transaction
  (first statement starts it; we never embed ``BEGIN``/``COMMIT`` SQL in
  Python — transaction control goes through psycopg's owned connection
  API);
- explicit :meth:`commit` persists once;
- explicit :meth:`rollback` discards once (idempotent);
- any exit without commit rolls the open transaction back and returns the
  connection to the pool;
- exceptions and ``asyncio.CancelledError`` propagate unchanged after the
  transaction is resolved;
- a commit failure raises a bounded error and never claims committed;
- repository use after the unit of work is closed fails boundedly.

Repositories are transaction-scoped: they are created by this unit of work
and share its single connection.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from psycopg import AsyncConnection

from darkula.app.persistence import PersistenceError, UnitOfWork
from darkula.infrastructure.persistence.postgresql.errors import (
    map_driver_error,
)

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from darkula.app.repositories import (
        SourceCandidateRepository,
        SourceRepository,
    )
    from darkula.infrastructure.persistence.postgresql.candidate_repository import (
        PostgresSourceCandidateRepository,
    )
    from darkula.infrastructure.persistence.postgresql.source_repository import (
        PostgresSourceRepository,
    )


class _State(StrEnum):
    """One PostgresUnitOfWork lifecycle state."""

    CREATED = "created"
    OPEN = "open"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class PostgresUnitOfWork(UnitOfWork):
    """One explicit PostgreSQL transaction bound to a pooled connection."""

    def __init__(self, *, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self._state = _State.CREATED
        self._conn: AsyncConnection[Any] | None = None
        self._conn_cm: Any = None
        self._candidate_repository: PostgresSourceCandidateRepository | None = None
        self._source_repository: PostgresSourceRepository | None = None

    async def __aenter__(self) -> Self:
        if self._state is not _State.CREATED:
            raise PersistenceError("this unit of work was already used")
        try:
            self._conn_cm = self._pool.connection()
            self._conn = await self._conn_cm.__aenter__()
        except asyncio.CancelledError:
            await self._release_connection()
            raise
        except Exception as exc:
            await self._release_connection()
            raise map_driver_error(exc) from exc
        self._state = _State.OPEN
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._state is _State.OPEN:
                try:
                    await self.rollback()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A rollback failure during an exceptional exit must
                    # not mask the original exception; during a normal
                    # uncommitted exit it is a real bounded failure.
                    if exc_type is None:
                        raise
        finally:
            await self._release_connection()
        return None

    async def commit(self) -> None:
        """Persist the transaction explicitly and durably (once)."""
        if self._state is _State.COMMITTED:
            raise PersistenceError("this unit of work already committed")
        if self._state is not _State.OPEN or self._conn is None:
            raise PersistenceError("this unit of work is closed")
        try:
            await self._conn.commit()
        except asyncio.CancelledError:
            self._state = _State.FAILED
            await self._release_connection()
            raise
        except Exception as exc:
            # Never claim committed: the durable outcome is unknown.
            self._state = _State.FAILED
            await self._release_connection()
            raise map_driver_error(exc) from exc
        self._state = _State.COMMITTED
        await self._release_connection()

    async def rollback(self) -> None:
        """Discard the transaction explicitly; safe to call once (idempotent)."""
        if self._state is _State.ROLLED_BACK:
            return
        if self._state in (_State.COMMITTED, _State.FAILED):
            return
        if self._state is _State.CREATED:
            raise PersistenceError("this unit of work never opened")
        if self._state is not _State.OPEN or self._conn is None:
            return
        try:
            await self._conn.rollback()
        except asyncio.CancelledError:
            self._state = _State.ROLLED_BACK
            await self._release_connection()
            raise
        except Exception as exc:
            self._state = _State.FAILED
            await self._release_connection()
            raise map_driver_error(exc) from exc
        self._state = _State.ROLLED_BACK

    @property
    def is_open(self) -> bool:
        """Return whether repository work is currently permitted."""
        return self._state is _State.OPEN

    def ensure_open(self) -> None:
        """Raise a bounded error when this unit of work is closed."""
        if self._state is not _State.OPEN:
            raise PersistenceError("this unit of work is closed")

    def connection(self) -> AsyncConnection[Any]:
        """Return the live connection; bounded failure once closed."""
        self.ensure_open()
        if self._conn is None:
            raise PersistenceError("this unit of work is closed")
        return self._conn

    @property
    def source_candidates(self) -> SourceCandidateRepository:
        """Return the transaction-bound candidate repository."""
        self.ensure_open()
        if self._candidate_repository is None:
            from darkula.infrastructure.persistence.postgresql.candidate_repository import (  # noqa: E501
                PostgresSourceCandidateRepository,
            )

            self._candidate_repository = PostgresSourceCandidateRepository(self)
        return self._candidate_repository

    @property
    def sources(self) -> SourceRepository:
        """Return the transaction-bound source repository."""
        self.ensure_open()
        if self._source_repository is None:
            from darkula.infrastructure.persistence.postgresql.source_repository import (  # noqa: E501
                PostgresSourceRepository,
            )

            self._source_repository = PostgresSourceRepository(self)
        return self._source_repository

    async def _release_connection(self) -> None:
        """Return the borrowed connection to the pool exactly once."""
        conn_cm = self._conn_cm
        self._conn_cm = None
        self._conn = None
        if conn_cm is not None:
            try:
                await conn_cm.__aexit__(None, None, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Returning a broken connection must not mask the original
                # outcome; the pool discards/resets unusable connections.
                return


__all__ = ["PostgresUnitOfWork"]
