# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL DarkulaSpi: owns pool/connection infrastructure (PR 4).

:class:`PostgresDarkulaSpi` is the central composition-selected persistence
entry point. It owns one bounded :class:`psycopg_pool.AsyncConnectionPool`
and hands out fresh
:class:`~darkula.infrastructure.persistence.postgresql.unit_of_work.PostgresUnitOfWork`
instances; repositories never construct connections.

Lifecycle is explicit and owned by the caller:

- ``await spi.start()`` opens the pool (bounded workers, no unbounded
  connection fan-out);
- ``await spi.close()`` drains and closes it;
- the async context manager ties both together.

Composition constructs the SPI without opening any connection, so QA and
integration-fixture code both stay lazy and infrastructure-free until an
explicit start.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Self

from psycopg_pool import AsyncConnectionPool

from darkula.app.persistence import DarkulaSpi, UnitOfWork
from darkula.config.settings import DatabaseSettings
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.unit_of_work import (
    PostgresUnitOfWork,
)


class PostgresDarkulaSpi(DarkulaSpi):
    """Darkula-owned PostgreSQL persistence service.

    The SPI never embeds or executes SQL: it only manages the pool and
    returns transaction-scoped units of work whose repositories invoke
    versioned stored functions.
    """

    def __init__(
        self,
        *,
        conninfo: str,
        pool_min_size: int,
        pool_max_size: int,
        acquire_timeout_seconds: float,
    ) -> None:
        if pool_min_size < 1:
            raise ValueError("pool_min_size must be >= 1")
        if pool_max_size < pool_min_size:
            raise ValueError("pool_max_size must be >= pool_min_size")
        if acquire_timeout_seconds <= 0:
            raise ValueError("acquire_timeout_seconds must be positive")
        self._conninfo = conninfo
        self._pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=pool_min_size,
            max_size=pool_max_size,
            timeout=acquire_timeout_seconds,
            open=False,
        )
        self._started = False

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> Self:
        """Construct the SPI from validated database settings."""
        conninfo = settings.conninfo()
        return cls(
            conninfo=conninfo,
            pool_min_size=settings.pool_min_size,
            pool_max_size=settings.pool_max_size,
            acquire_timeout_seconds=settings.connect_timeout_seconds,
        )

    @property
    def is_started(self) -> bool:
        """Return whether the pool has been explicitly opened."""
        return self._started

    async def start(self) -> None:
        """Open the bounded connection pool explicitly.

        Connect failures surface as bounded :class:`PersistenceUnavailableError`.
        """
        if self._started:
            return
        try:
            await self._pool.open()
        except Exception as exc:  # OSError/psycopg connect failures
            raise map_driver_error(exc) from exc
        self._started = True

    async def close(self) -> None:
        """Drain and close the pool; safe to call more than once."""
        if self._started:
            await self._pool.close()
        self._started = False

    @asynccontextmanager
    async def lifecycle(self) -> AsyncIterator[Self]:
        """Explicit async shutdown context: ``async with spi.lifecycle():``."""
        await self.start()
        try:
            yield self
        finally:
            await self.close()

    def unit_of_work(self) -> UnitOfWork:
        """Return one fresh unit of work bound to this SPI's pool."""
        return PostgresUnitOfWork(pool=self._pool)


__all__ = ["PostgresDarkulaSpi"]
