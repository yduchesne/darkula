# SPDX-License-Identifier: AGPL-3.0-only
"""PR 18 real-PostgreSQL registry polling vertical slices (I01-I20).

Only the external registry HTTP transport is faked. The real RansomLook
parser/adapter, Application poller, ``SourceDiscoveryService``,
``PostgresDarkulaSpi``, versioned stored functions and PostgreSQL all run
unchanged. No candidate/source/crawler/LLM path is exercised.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from darkula.app.persistence import ConflictError
from darkula.app.registry_polling import (
    RegistryPoller,
    RegistryPollOutcome,
    RegistryTransportError,
)
from darkula.app.source_discovery import DiscoveryProvider, SourceDiscoveryService
from darkula.config.settings import DatabaseSettings
from darkula.domain.discovery import DiscoveryInput
from darkula.infrastructure.discovery.ransomlook import RansomLookDiscoveryAdapter
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_world.source_registry import (
    FakeSourceRegistry,
    ScriptedRegistryResponse,
)
from tests.integration.conftest import _raw_connect

pytestmark = pytest.mark.integration

BASE_TIME = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

FORBIDDEN_TABLES = (
    "source_candidate",
    "source_candidate_event_history",
    "source",
    "source_endpoint",
    "recon_assessment",
    "qualification_decision",
    "access_evidence",
    "collection_run",
    "message_outbox",
)


async def _noop_sleep(seconds: float) -> None:
    del seconds


class _Clock:
    def __init__(self, now: datetime = BASE_TIME) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _count(database_settings: DatabaseSettings, table: str) -> int:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _observation_rows(database_settings: DatabaseSettings) -> list[tuple[Any, ...]]:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider_record_id, provider_record_version, "
                "observed_at, ingested_at, normalized_locator, "
                "asserted_reference, semantic_key "
                "FROM discovery_observation ORDER BY provider_record_id"
            )
            return [tuple(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _adapter(registry: FakeSourceRegistry) -> RansomLookDiscoveryAdapter:
    return RansomLookDiscoveryAdapter(
        transport=registry, request_interval_seconds=0, sleep=_noop_sleep
    )


def _poller(spi: PostgresDarkulaSpi, clock: _Clock) -> RegistryPoller:
    return RegistryPoller(
        persistence=spi,
        discovery_service=SourceDiscoveryService(persistence=spi, clock=clock),
        clock=clock,
    )


def _index_body(*names: str) -> bytes:
    import json

    return json.dumps(list(names)).encode("utf-8")


class TestCheckpointPersistence:
    """I02-I06: durable checkpoint CAS semantics."""

    @pytest.mark.asyncio
    async def test_absent_checkpoint_has_generation_zero(self, spi: Any) -> None:
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.generation == 0
        assert checkpoint.cursor is None
        assert checkpoint.cooldown_until is None

    @pytest.mark.asyncio
    async def test_first_advance_creates_generation_one(self, spi: Any) -> None:
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.advance_registry_checkpoint(
                "ransomlook",
                expected_generation=0,
                cursor="alpha",
                completed_at=BASE_TIME,
            )
            await uow.commit()
        assert checkpoint.generation == 1
        assert checkpoint.cursor == "alpha"
        assert checkpoint.last_completed_at == BASE_TIME

    @pytest.mark.asyncio
    async def test_stale_cas_raises_without_change(self, spi: Any) -> None:
        async with spi.unit_of_work() as uow:
            await uow.discovery.advance_registry_checkpoint(
                "ransomlook",
                expected_generation=0,
                cursor="alpha",
                completed_at=BASE_TIME,
            )
            await uow.commit()
        with pytest.raises(ConflictError):
            async with spi.unit_of_work() as uow:
                await uow.discovery.advance_registry_checkpoint(
                    "ransomlook",
                    expected_generation=0,
                    cursor="beta",
                    completed_at=BASE_TIME,
                )
                await uow.commit()
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.cursor == "alpha"
        assert checkpoint.generation == 1

    @pytest.mark.asyncio
    async def test_defer_survives_a_new_spi(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        async with spi.unit_of_work() as uow:
            await uow.discovery.advance_registry_checkpoint(
                "ransomlook",
                expected_generation=0,
                cursor="alpha",
                completed_at=BASE_TIME,
            )
            await uow.commit()
        later = BASE_TIME + timedelta(seconds=900)
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.defer_registry_checkpoint(
                "ransomlook", expected_generation=1, not_before=later
            )
            await uow.commit()
        assert checkpoint.cursor == "alpha"
        assert checkpoint.generation == 2
        assert checkpoint.cooldown_until == later

        second = PostgresDarkulaSpi.from_settings(database_settings)
        await second.start()
        try:
            async with second.unit_of_work() as uow:
                reread = await uow.discovery.get_registry_checkpoint("ransomlook")
        finally:
            await second.close()
        assert reread.cursor == "alpha"
        assert reread.generation == 2
        assert reread.cooldown_until == later

    @pytest.mark.asyncio
    async def test_defer_rollback_is_discarded(self, spi: Any) -> None:
        async with spi.unit_of_work() as uow:
            await uow.discovery.defer_registry_checkpoint(
                "ransomlook", expected_generation=0, not_before=BASE_TIME
            )
            await uow.rollback()
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.generation == 0


class TestVerticalSlice:
    """I07-I14/I17: real adapter, poller, service and PostgreSQL."""

    @pytest.mark.asyncio
    async def test_fake_registry_imports_observations_only(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        for table in FORBIDDEN_TABLES:
            assert _count(database_settings, table) == 0
        registry = FakeSourceRegistry(index=("blackgate",))
        clock = _Clock()
        result = await _poller(spi, clock).poll_once(_adapter(registry))
        assert result.outcome is RegistryPollOutcome.COMPLETED
        assert result.observations_added == 3
        assert result.records_skipped == 0
        rows = _observation_rows(database_settings)
        assert len(rows) == 3
        assert all(row[0].startswith(("group:", "location:")) for row in rows)
        for table in FORBIDDEN_TABLES:
            assert _count(database_settings, table) == 0

    @pytest.mark.asyncio
    async def test_repeated_pass_replays_without_new_rows(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        clock = _Clock()
        registry = FakeSourceRegistry(index=("blackgate",))
        poller = _poller(spi, clock)
        adapter = _adapter(registry)
        await poller.poll_once(adapter)
        first = _observation_rows(database_settings)
        await poller.poll_once(adapter)  # end-of-pass reset
        clock.now += timedelta(days=1)
        result = await poller.poll_once(adapter)
        assert result.observations_replayed == 3
        assert result.observations_added == 0
        assert _observation_rows(database_settings) == first

    @pytest.mark.asyncio
    async def test_changed_safe_field_creates_a_new_version(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        clock = _Clock()
        registry = FakeSourceRegistry(index=("blackgate",))
        poller = _poller(spi, clock)
        adapter = _adapter(registry)
        await poller.poll_once(adapter)
        before = _observation_rows(database_settings)

        changed = FakeSourceRegistry(
            index=("blackgate",),
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {
                                "slug": "http://blackgate-board.onion/forum",
                                "updated": "2026-03-04T05:06:07Z",
                            },
                            {"slug": "blackgate-mirror.onion"},
                        ],
                    },
                    [],
                ]
            },
        )
        await poller.poll_once(_adapter(changed))  # reset cursor
        await poller.poll_once(_adapter(changed))
        after = _observation_rows(database_settings)
        assert len(after) == len(before) + 1
        assert {row[0] for row in before} <= {row[0] for row in after}

    @pytest.mark.asyncio
    async def test_invalid_record_is_skipped_and_valid_sibling_persists(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        registry = FakeSourceRegistry(
            index=("blackgate",),
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {"slug": "ftp://example.org/not-allowed"},
                            {"slug": "https://example.org/valid"},
                        ],
                    },
                    [],
                ]
            },
        )
        clock = _Clock()
        result = await _poller(spi, clock).poll_once(_adapter(registry))
        assert result.records_skipped == 1
        assert result.observations_added == 2
        rows = _observation_rows(database_settings)
        assert any(row[4] == "https://example.org/valid" for row in rows)
        assert not any(row[5].startswith("ftp://") for row in rows)
        assert result.outcome is RegistryPollOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_mid_group_failure_keeps_checkpoint_and_replays(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        clock = _Clock()
        registry = FakeSourceRegistry(index=("blackgate",))
        real_adapter = _adapter(registry)

        class _FailingAdapter:
            provider_id = "ransomlook"

            def __init__(self) -> None:
                self._entry: str | None = None

            async def list_entry_ids(self) -> tuple[str, ...]:
                return ("blackgate",)

            def for_entry(
                self, entry_id: str, *, observed_at: datetime
            ) -> DiscoveryProvider:
                inner = real_adapter.for_entry(entry_id, observed_at=observed_at)
                return _FailAfterTwo(inner)

        result = await _poller(spi, clock).poll_once(_FailingAdapter())
        assert result.outcome is RegistryPollOutcome.TEMPORARY_ERROR
        assert _count(database_settings, "discovery_observation") == 2
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.cursor is None
        assert checkpoint.cooldown_until is not None

        clock.now += timedelta(seconds=900)
        result = await _poller(spi, clock).poll_once(_adapter(registry))
        assert result.observations_replayed == 2
        assert result.observations_added == 1
        assert _count(database_settings, "discovery_observation") == 3

    @pytest.mark.asyncio
    async def test_rate_limit_persists_cooldown_and_blocks_http(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        clock = _Clock()
        registry = FakeSourceRegistry(
            scripted=[
                ScriptedRegistryResponse(200, body=_index_body("blackgate")),
                ScriptedRegistryResponse(429, retry_after="30"),
            ]
        )
        poller = _poller(spi, clock)
        adapter = _adapter(registry)
        result = await poller.poll_once(adapter)
        assert result.outcome is RegistryPollOutcome.TEMPORARY_ERROR
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.cursor is None
        assert checkpoint.cooldown_until == BASE_TIME + timedelta(seconds=30)
        calls = len(registry.calls)

        clock.now += timedelta(seconds=10)
        result = await poller.poll_once(adapter)
        assert result.outcome is RegistryPollOutcome.COOLDOWN
        assert len(registry.calls) == calls
        assert _count(database_settings, "discovery_observation") == 0

    @pytest.mark.asyncio
    async def test_retryable_status_retries_once_then_completes(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        clock = _Clock()
        registry = FakeSourceRegistry(
            scripted=[
                ScriptedRegistryResponse(503),
                ScriptedRegistryResponse(200, body=_index_body("blackgate")),
            ]
        )
        result = await _poller(spi, clock).poll_once(_adapter(registry))
        assert result.outcome is RegistryPollOutcome.COMPLETED
        assert len(registry.calls) == 3  # failed attempt, retried index, detail
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.cursor == "blackgate"
        assert checkpoint.generation == 1

    @pytest.mark.asyncio
    async def test_malformed_group_schema_leaves_checkpoint(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        registry = FakeSourceRegistry(
            index=("blackgate",), details={"blackgate": {"bad": True}}
        )
        result = await _poller(spi, _Clock()).poll_once(_adapter(registry))
        assert result.outcome is RegistryPollOutcome.PERMANENT_ERROR
        assert _count(database_settings, "discovery_observation") == 0
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.generation == 0

    @pytest.mark.asyncio
    async def test_private_group_is_excluded_and_advances(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        registry = FakeSourceRegistry(
            index=("blackgate",),
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "private": True,
                        "locations": [{"slug": "https://secret.example.org/"}],
                    },
                    [],
                ]
            },
        )
        result = await _poller(spi, _Clock()).poll_once(_adapter(registry))
        assert result.records_skipped == 1
        assert _count(database_settings, "discovery_observation") == 0
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.cursor == "blackgate"

    @pytest.mark.asyncio
    async def test_same_locator_distinct_records_and_no_candidates(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        registry = FakeSourceRegistry(
            index=("blackgate",),
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {"slug": "https://Example.org:443/a"},
                            {"slug": "https://example.org/a"},
                        ],
                    },
                    [],
                ]
            },
        )
        result = await _poller(spi, _Clock()).poll_once(_adapter(registry))
        assert result.observations_added == 3
        rows = _observation_rows(database_settings)
        locations = [row for row in rows if row[0].startswith("location:")]
        assert len(locations) == 2
        assert locations[0][4] == locations[1][4] == "https://example.org/a"
        assert locations[0][0] != locations[1][0]
        assert _count(database_settings, "source_candidate") == 0


class _FailAfterTwo(DiscoveryProvider):
    """Yield two real claims, then fail like a transport outage."""

    def __init__(self, inner: DiscoveryProvider) -> None:
        self._inner = inner
        self.skipped_records = 0

    async def observations(self) -> AsyncIterator[DiscoveryInput]:
        count = 0
        async for claim in self._inner.observations():
            if count >= 2:
                raise RegistryTransportError("injected mid-group outage")
            count += 1
            yield claim


class TestConcurrencyAndRestart:
    """I15/I19: CAS races and restart resumption."""

    @pytest.mark.asyncio
    async def test_concurrent_pollers_exactly_one_advances(
        self, spi: Any, database_settings: DatabaseSettings
    ) -> None:
        clock = _Clock()
        first = _poller(spi, clock)
        second = _poller(spi, clock)
        results = await asyncio.gather(
            first.poll_once(_adapter(FakeSourceRegistry())),
            second.poll_once(_adapter(FakeSourceRegistry())),
        )
        outcomes = sorted(result.outcome.value for result in results)
        assert outcomes == ["CHECKPOINT_RACE", "COMPLETED"]
        assert _count(database_settings, "discovery_observation") == 10
        assert _count(database_settings, "source_candidate") == 0
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.cursor == "shadowtalk"
        assert checkpoint.generation == 4

    @pytest.mark.asyncio
    async def test_restart_resumes_from_persisted_cursor(
        self, database_settings: DatabaseSettings
    ) -> None:
        clock = _Clock()
        first_spi = PostgresDarkulaSpi.from_settings(database_settings)
        await first_spi.start()
        try:
            adapter = _adapter(FakeSourceRegistry(index=("accessbay", "blackgate")))
            result = await _poller(first_spi, clock).poll_once(adapter, max_entries=1)
            assert result.entries_completed == 1
        finally:
            await first_spi.close()

        second_spi = PostgresDarkulaSpi.from_settings(database_settings)
        await second_spi.start()
        try:
            async with second_spi.unit_of_work() as uow:
                checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
            assert checkpoint.cursor == "accessbay"

            adapter = _adapter(FakeSourceRegistry(index=("accessbay", "blackgate")))
            result = await _poller(second_spi, clock).poll_once(adapter, max_entries=1)
            assert result.entries_completed == 1
        finally:
            await second_spi.close()

        async with spi_unit(database_settings) as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.cursor == "blackgate"
        assert _count(database_settings, "source_candidate") == 0


class _SpiUnit:
    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        self._spi: PostgresDarkulaSpi | None = None
        self._uow: Any = None

    async def __aenter__(self) -> Any:
        self._spi = PostgresDarkulaSpi.from_settings(self._settings)
        await self._spi.start()
        self._uow = self._spi.unit_of_work()
        return await self._uow.__aenter__()

    async def __aexit__(self, *args: Any) -> None:
        assert self._spi is not None
        await self._uow.__aexit__(*args)
        await self._spi.close()


def spi_unit(settings: DatabaseSettings) -> _SpiUnit:
    return _SpiUnit(settings)
