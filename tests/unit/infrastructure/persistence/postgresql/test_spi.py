# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for PostgresDarkulaSpi lifecycle and construction."""

from __future__ import annotations

import pytest

from darkula.config.settings import DatabaseSettings
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi


class TestSpiConstruction:
    """Construction is lazy and validates pool bounds."""

    def test_from_settings_keeps_settings_conninfo(self) -> None:
        spi = PostgresDarkulaSpi.from_settings(DatabaseSettings())
        assert "host=localhost" in spi._conninfo
        assert spi._started is False

    def test_invalid_pool_bounds_rejected(self) -> None:
        with pytest.raises(ValueError):
            PostgresDarkulaSpi(
                conninfo="host=localhost",
                pool_min_size=0,
                pool_max_size=2,
                acquire_timeout_seconds=5,
            )
        with pytest.raises(ValueError):
            PostgresDarkulaSpi(
                conninfo="host=localhost",
                pool_min_size=3,
                pool_max_size=2,
                acquire_timeout_seconds=5,
            )


class TestSpiLifecycle:
    """start/close and the async lifecycle helper."""

    @pytest.mark.asyncio
    async def test_lifecycle_starts_and_closes(self) -> None:
        spi = PostgresDarkulaSpi.from_settings(DatabaseSettings())
        async with spi.lifecycle():
            assert spi._started
        assert spi._started is False

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        spi = PostgresDarkulaSpi.from_settings(DatabaseSettings())
        await spi.close()
        await spi.close()
        assert spi._started is False

    def test_unit_of_work_returns_postgres_uow(self) -> None:
        spi = PostgresDarkulaSpi.from_settings(DatabaseSettings())
        uow = spi.unit_of_work()
        from darkula.infrastructure.persistence.postgresql.unit_of_work import (
            PostgresUnitOfWork,
        )

        assert isinstance(uow, PostgresUnitOfWork)
