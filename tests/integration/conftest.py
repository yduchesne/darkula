# SPDX-License-Identifier: AGPL-3.0-only
"""Integration fixtures: real PostgresDarkulaSpi against the Darkula-owned
Podman PostgreSQL at host port 35432.

The suite assumes the container is running and migrations are applied
(``./build.sh --intg`` provisions, migrates, runs this suite, then cleans
up). The fixture re-verifies schema presence and resets table state between
tests with test-only SQL so order never matters.

Production-path tests always go through
``PostgresDarkulaSpi -> PostgresUnitOfWork -> repository -> stored function
-> PostgreSQL``; direct SQL is restricted to setup/teardown/assertions.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
import pytest_asyncio

from darkula.config.loader import load_settings
from darkula.config.settings import (
    DatabaseSettings,
    DataStreamDriver,
    DataStreamSettings,
)
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi

#: Integration-only credential shared with scripts/darkula_postgres.sh; the
#: operator may override through DARKULA_DATABASE__PASSWORD.
INTEGRATION_PASSWORD = "darkula-local-intg"


def row_values(
    cursor: psycopg.Cursor[Any], row: tuple[Any, ...] | None
) -> tuple[Any, ...]:
    """Return a non-None fetched row (test-only SQL inspection helper)."""
    assert row is not None, "expected a result row"
    return row


TABLE_NAMES = (
    "source_candidate_event_history",
    "recon_assessment",
    "source_endpoint",
    "source_assessment",
    "source_candidate",
    "source",
    "message_outbox",
    "processed_message",
    "content_artifact",
    "normalized_content",
    "collection_run",
    "collection_policy_endpoint",
    "collection_policy",
    "extracted_entity",
    "extraction_result",
    "geographic_resolution",
)

#: Host-published Darkula Redpanda Kafka port (container port 9092).
REDPANDA_BOOTSTRAP = "127.0.0.1:39092"


@pytest.fixture(scope="session")
def redpanda_bootstrap() -> str:
    """Resolved Redpanda bootstrap address for host-side integration."""
    return REDPANDA_BOOTSTRAP


@pytest.fixture(scope="session")
def redpanda_settings(
    redpanda_bootstrap: str,
) -> DataStreamSettings:
    """DataStream settings pointing at the Darkula-owned Redpanda."""
    return DataStreamSettings(
        driver=DataStreamDriver.REDPANDA,
        bootstrap_servers=redpanda_bootstrap,
        client_id="darkula-intg",
        poll_timeout_ms=2000,
        max_poll_records=100,
    )


@pytest.fixture(scope="session")
def database_settings() -> DatabaseSettings:
    """Resolved development-profile settings pointing at host 35432."""
    os.environ.setdefault("DARKULA_DATABASE__PASSWORD", INTEGRATION_PASSWORD)
    settings = load_settings()
    assert settings.database.port == 35432, (
        "integration suite requires the host-published Darkula port 35432"
    )
    return settings.database


@pytest_asyncio.fixture
async def spi(database_settings: DatabaseSettings) -> AsyncIterator[PostgresDarkulaSpi]:
    """One real PostgresDarkulaSpi per test (pool opened lazily)."""
    instance = PostgresDarkulaSpi.from_settings(database_settings)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()


def _raw_connect(database_settings: DatabaseSettings) -> psycopg.Connection[Any]:
    """Test-only direct connection for setup/teardown/verification SQL."""
    return psycopg.connect(database_settings.conninfo())


def wait_for_migrations(database_settings: DatabaseSettings) -> None:
    """Test-only verification that the migration schema is present."""
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version FROM darkula_schema_migrations "
                "ORDER BY version DESC LIMIT 1"
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, (
        "no migration ledger found; run ./scripts/darkula_postgres.sh migrate"
    )


@pytest.fixture(autouse=True)
def _reset_tables(
    request: pytest.FixtureRequest, database_settings: DatabaseSettings
) -> Any:
    """Test-only deterministic state reset before every DB-backed test.

    The crawler sub-suite is deliberately DB-free (it tests that PostgreSQL is
    *denied* from the sandbox), so it neither needs nor triggers this reset.
    """
    from pathlib import Path

    crawler_root = Path(__file__).resolve().parent / "crawler"
    if crawler_root in Path(str(request.path)).resolve().parents:
        return None
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext('darkula_intg_reset'))")
            try:
                cur.execute(
                    f"TRUNCATE {', '.join(TABLE_NAMES)} RESTART IDENTITY CASCADE"
                )
            finally:
                cur.execute("SELECT pg_advisory_unlock(hashtext('darkula_intg_reset'))")
        conn.commit()
        wait_for_migrations(database_settings)
    finally:
        conn.close()
