# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the provider-neutral one-shot registry poller (PR 18)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.support.discovery_fakes import MemDiscoverySpi, seed_observation

from darkula.app.persistence import ConflictError
from darkula.app.registry_polling import (
    RegistryDiscoveryAdapter,
    RegistryPoller,
    RegistryPollOutcome,
    RegistryRateLimited,
    RegistryRecordSkipReason,
    RegistrySchemaError,
    RegistryTransportError,
)
from darkula.app.source_discovery import DiscoveryProvider, SourceDiscoveryService
from darkula.domain.discovery import (
    DiscoveryDetectionMethod,
    DiscoveryInput,
    DiscoveryObservation,
    DiscoveryReferenceKind,
)
from darkula.infrastructure.discovery.ransomlook import RansomLookDiscoveryAdapter
from darkula.testing.fake_world.source_registry import (
    FakeSourceRegistry,
    ScriptedRegistryResponse,
)

BASE_TIME = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


async def _noop_sleep(seconds: float) -> None:
    del seconds


def _claim(
    record_id: str,
    *,
    asserted: str | None = None,
    version: str = "v1",
    observed_at: datetime = BASE_TIME,
) -> DiscoveryInput:
    return DiscoveryInput(
        provider_id="ransomlook",
        provider_record_id=f"record-{record_id}",
        provider_record_version=version,
        detection_method=DiscoveryDetectionMethod.REGISTRY,
        reference_kind=DiscoveryReferenceKind.NAME_ONLY,
        asserted_reference=asserted if asserted is not None else f"site-{record_id}",
        observed_at=observed_at,
        evidence_reference=f"registry://ransomlook/{record_id}",
    )


class _StubProvider(DiscoveryProvider):
    def __init__(
        self,
        claims: list[DiscoveryInput],
        *,
        skipped: int = 0,
        fail_after: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.claims = claims
        self.skipped_records = skipped
        self.fail_after = fail_after
        self.error = error
        self.yielded = 0

    async def observations(self) -> AsyncIterator[DiscoveryInput]:
        for claim in self.claims:
            if self.fail_after is not None and self.yielded >= self.fail_after:
                assert self.error is not None
                raise self.error
            self.yielded += 1
            yield claim


class _StubAdapter:
    provider_id = "ransomlook"

    def __init__(
        self,
        entries: dict[str, _StubProvider],
        *,
        index: list[str] | None = None,
        list_error: BaseException | None = None,
    ) -> None:
        self.entries = entries
        self.index = index if index is not None else list(entries)
        self.list_error = list_error
        self.detail_requests: list[str] = []

    async def list_entry_ids(self) -> tuple[str, ...]:
        if self.list_error is not None:
            raise self.list_error
        return tuple(self.index)

    def for_entry(self, entry_id: str, *, observed_at: datetime) -> DiscoveryProvider:
        self.detail_requests.append(entry_id)
        return self.entries[entry_id]


class _Clock:
    def __init__(self) -> None:
        self.now = BASE_TIME

    def __call__(self) -> datetime:
        return self.now


def _poller(spi: MemDiscoverySpi, clock: _Clock) -> RegistryPoller:
    return RegistryPoller(
        persistence=spi,
        discovery_service=SourceDiscoveryService(persistence=spi, clock=clock),
        clock=clock,
    )


class TestSelectionAndCheckpoint:
    """U19-U23/U27: keyset selection and per-entry checkpoint ordering."""

    @pytest.mark.asyncio
    async def test_polls_at_most_configured_entries(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        adapter = _StubAdapter(
            {
                "alpha": _StubProvider([_claim("a")]),
                "beta": _StubProvider([_claim("b")]),
                "gamma": _StubProvider([_claim("c")]),
            }
        )
        result = await _poller(spi, clock).poll_once(adapter, max_entries=2)
        assert result.outcome is RegistryPollOutcome.COMPLETED
        assert result.entries_selected == 2
        assert result.entries_completed == 2
        assert adapter.detail_requests == ["alpha", "beta"]
        checkpoint = spi.state.registry_checkpoints["ransomlook"]
        assert checkpoint.cursor == "beta"
        assert checkpoint.generation == 2

    @pytest.mark.asyncio
    async def test_end_of_index_resets_cursor_without_wrapping(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        async with spi.unit_of_work() as uow:
            await uow.discovery.advance_registry_checkpoint(
                "ransomlook",
                expected_generation=0,
                cursor="zeta",
                completed_at=BASE_TIME,
            )
            await uow.commit()
        adapter = _StubAdapter({"alpha": _StubProvider([_claim("a")])})
        result = await _poller(spi, clock).poll_once(adapter)
        assert result.entries_selected == 0
        assert result.entries_completed == 0
        assert adapter.detail_requests == []
        checkpoint = spi.state.registry_checkpoints["ransomlook"]
        assert checkpoint.cursor is None
        assert checkpoint.generation == 2

    @pytest.mark.asyncio
    async def test_new_earlier_entry_is_not_lost(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        async with spi.unit_of_work() as uow:
            await uow.discovery.advance_registry_checkpoint(
                "ransomlook",
                expected_generation=0,
                cursor="beta",
                completed_at=BASE_TIME,
            )
            await uow.commit()
        adapter = _StubAdapter(
            {
                "alpha": _StubProvider([]),
                "beta": _StubProvider([]),
                "gamma": _StubProvider([]),
            },
            index=["alpha", "beta", "gamma"],
        )
        result = await _poller(spi, clock).poll_once(adapter)
        assert adapter.detail_requests == ["gamma"]
        assert result.entries_selected == 1
        # A full pass (after reset) captures the earlier alpha entry.
        await _poller(spi, clock).poll_once(adapter)  # resets cursor
        result = await _poller(spi, clock).poll_once(adapter)
        assert adapter.detail_requests[-3:] == ["alpha", "beta", "gamma"]
        assert result.entries_selected == 3

    @pytest.mark.asyncio
    async def test_record_skip_count_is_reported_and_advances(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        adapter = _StubAdapter(
            {"alpha": _StubProvider([_claim("a")], skipped=2)},
        )
        result = await _poller(spi, clock).poll_once(adapter)
        assert result.records_skipped == 2
        assert result.outcome is RegistryPollOutcome.COMPLETED
        assert spi.state.registry_checkpoints["ransomlook"].cursor == "alpha"

    @pytest.mark.asyncio
    async def test_partial_group_failure_does_not_advance_checkpoint(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        failing = _StubAdapter(
            {
                "alpha": _StubProvider(
                    [_claim("a1"), _claim("a2")],
                    fail_after=1,
                    error=RegistryTransportError("temporary"),
                )
            }
        )
        result = await _poller(spi, clock).poll_once(failing)
        assert result.outcome is RegistryPollOutcome.TEMPORARY_ERROR
        assert len(spi.state.observations_by_key) == 1
        checkpoint = spi.state.registry_checkpoints["ransomlook"]
        assert checkpoint.cursor is None
        assert checkpoint.cooldown_until == BASE_TIME + timedelta(seconds=900)

        clock.now += timedelta(seconds=900)
        succeeding = _StubAdapter(
            {"alpha": _StubProvider([_claim("a1"), _claim("a2")])}
        )
        result = await _poller(spi, clock).poll_once(succeeding)
        assert result.observations_added == 1
        assert result.observations_replayed == 1
        assert spi.state.registry_checkpoints["ransomlook"].cursor == "alpha"

    @pytest.mark.asyncio
    async def test_checkpoint_race_is_reported_without_overwrite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        adapter = _StubAdapter({"alpha": _StubProvider([_claim("a")])})
        poller = _poller(spi, clock)

        async def _raise(*args: Any, **kwargs: Any) -> Any:
            raise ConflictError("race")

        monkeypatch.setattr(poller, "_advance", _raise)
        result = await poller.poll_once(adapter)
        assert result.outcome is RegistryPollOutcome.CHECKPOINT_RACE


class TestReplay:
    """U24-U26: exact replays reuse the stored acceptance identity."""

    @pytest.mark.asyncio
    async def test_clock_advance_does_not_conflict(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        adapter = _StubAdapter({"alpha": _StubProvider([_claim("a")])})
        first = await _poller(spi, clock).poll_once(adapter)
        stored = next(iter(spi.state.observations_by_key.values()))
        assert first.observations_added == 1

        clock.now += timedelta(days=2)
        async with spi.unit_of_work() as uow:
            await uow.discovery.advance_registry_checkpoint(
                "ransomlook",
                expected_generation=1,
                cursor=None,
                completed_at=clock.now,
            )
            await uow.commit()
        second = await _poller(spi, clock).poll_once(adapter)
        replayed = next(iter(spi.state.observations_by_key.values()))
        assert second.observations_replayed == 1
        assert second.observations_added == 0
        assert replayed.observation_id == stored.observation_id
        assert replayed.observed_at == stored.observed_at
        assert replayed.ingested_at == stored.ingested_at

    @pytest.mark.asyncio
    async def test_concurrent_first_writer_replays_once(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        existing = _observation_for(_claim("a"))
        runtime = SourceDiscoveryService(persistence=spi, clock=clock)
        racing = _RacingService(spi, runtime, existing)
        poller = RegistryPoller(
            persistence=spi,
            discovery_service=racing,  # type: ignore[arg-type]
            clock=clock,
        )
        adapter = _StubAdapter({"alpha": _StubProvider([_claim("a")])})
        result = await poller.poll_once(adapter)
        assert result.observations_replayed == 1
        assert racing.calls == 2
        assert len(spi.state.observations_by_key) == 1

    @pytest.mark.asyncio
    async def test_concurrent_changed_payload_fails_group(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        existing = _observation_for(_claim("a", asserted="changed", version="v2"))
        runtime = SourceDiscoveryService(persistence=spi, clock=clock)
        racing = _RacingService(spi, runtime, existing)
        poller = RegistryPoller(
            persistence=spi,
            discovery_service=racing,  # type: ignore[arg-type]
            clock=clock,
        )
        adapter = _StubAdapter({"alpha": _StubProvider([_claim("a")])})
        with pytest.raises(ConflictError):
            await poller.poll_once(adapter)
        assert "ransomlook" not in spi.state.registry_checkpoints


def _observation_for(claim: DiscoveryInput) -> DiscoveryObservation:
    from darkula.domain.discovery import observation_from_input
    from darkula.domain.identifiers import DiscoveryObservationId

    return observation_from_input(
        claim,
        observation_id=DiscoveryObservationId.generate(),
        ingested_at=BASE_TIME,
    )


class _RacingService:
    """Service stub that simulates a concurrent first writer exactly once."""

    def __init__(
        self,
        spi: MemDiscoverySpi,
        runtime: SourceDiscoveryService,
        existing: DiscoveryObservation,
    ) -> None:
        self._spi = spi
        self._runtime = runtime
        self._existing = existing
        self.calls = 0

    async def ingest(self, source: DiscoveryInput) -> DiscoveryObservation:
        self.calls += 1
        if self.calls == 1:
            await seed_observation(self._spi, self._existing)
            raise ConflictError("concurrent first writer")
        return await self._runtime.ingest(source)


class TestCooldownAndStatus:
    """U28-U31: durable, bounded cooldown semantics."""

    @pytest.mark.asyncio
    async def test_rate_limit_defers_and_blocks_next_run(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        registry = FakeSourceRegistry(
            scripted=[ScriptedRegistryResponse(429, retry_after="30")]
        )
        adapter = RansomLookDiscoveryAdapter(
            transport=registry, request_interval_seconds=0, sleep=_noop_sleep
        )
        poller = _poller(spi, clock)
        result = await poller.poll_once(adapter)
        assert result.outcome is RegistryPollOutcome.TEMPORARY_ERROR
        checkpoint = spi.state.registry_checkpoints["ransomlook"]
        assert checkpoint.cooldown_until == BASE_TIME + timedelta(seconds=30)
        assert checkpoint.cursor is None

        calls_before = len(registry.calls)
        clock.now += timedelta(seconds=10)
        result = await poller.poll_once(adapter)
        assert result.outcome is RegistryPollOutcome.COOLDOWN
        assert len(registry.calls) == calls_before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", ["99999", "Wed, 21 Oct 2015 07:28:00 GMT", None])
    async def test_malicious_retry_after_uses_fixed_fallback(
        self, header: str | None
    ) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        registry = FakeSourceRegistry(
            scripted=[ScriptedRegistryResponse(429, retry_after=header)]
        )
        adapter = RansomLookDiscoveryAdapter(
            transport=registry, request_interval_seconds=0, sleep=_noop_sleep
        )
        await _poller(spi, clock).poll_once(adapter)
        checkpoint = spi.state.registry_checkpoints["ransomlook"]
        assert checkpoint.cooldown_until == BASE_TIME + timedelta(seconds=900)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            RegistrySchemaError("bad"),
            RegistryTransportError("down"),
        ],
    )
    async def test_transport_and_schema_errors_map_to_outcomes(
        self, error: BaseException
    ) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        adapter = _StubAdapter({}, list_error=error)
        result = await _poller(spi, clock).poll_once(adapter)
        expected = (
            RegistryPollOutcome.PERMANENT_ERROR
            if isinstance(error, RegistrySchemaError)
            else RegistryPollOutcome.TEMPORARY_ERROR
        )
        assert result.outcome is expected

    @pytest.mark.asyncio
    async def test_rate_limited_index_defers(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        adapter = _StubAdapter({}, list_error=RegistryRateLimited(45))
        result = await _poller(spi, clock).poll_once(adapter)
        assert result.outcome is RegistryPollOutcome.TEMPORARY_ERROR
        checkpoint = spi.state.registry_checkpoints["ransomlook"]
        assert checkpoint.cooldown_until == BASE_TIME + timedelta(seconds=45)
        assert checkpoint.generation == 1


class TestCancellation:
    """U34: cancellation propagates with no checkpoint side effects."""

    @pytest.mark.asyncio
    async def test_cancel_propagates_without_checkpoint_change(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        adapter = _StubAdapter({}, list_error=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _poller(spi, clock).poll_once(adapter)
        assert spi.state.registry_checkpoints == {}


class TestSkipVocabulary:
    """Bounded skip reasons are a fixed developer-controlled vocabulary."""

    def test_reason_codes_are_bounded(self) -> None:
        assert {reason.value for reason in RegistryRecordSkipReason} == {
            "MISSING_REFERENCE",
            "INVALID_REFERENCE",
            "PRIVATE",
            "INVALID_TIMESTAMP",
            "INVALID_SCHEMA",
            "PR17_VALIDATION",
        }

    def test_adapter_satisfies_protocol(self) -> None:
        adapter: RegistryDiscoveryAdapter = RansomLookDiscoveryAdapter(
            transport=FakeSourceRegistry(), request_interval_seconds=0
        )
        assert adapter.provider_id == "ransomlook"


class TestCheckpointParity:
    """U39/U40: in-memory CAS parity, staging and rollback semantics."""

    @pytest.mark.asyncio
    async def test_missing_checkpoint_value(self) -> None:
        spi = MemDiscoverySpi()
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.get_registry_checkpoint("ransomlook")
        assert checkpoint.generation == 0
        assert checkpoint.cursor is None

    @pytest.mark.asyncio
    async def test_first_advance_creates_generation_one(self) -> None:
        spi = MemDiscoverySpi()
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
        assert spi.state.registry_checkpoints["ransomlook"].cursor == "alpha"

    @pytest.mark.asyncio
    async def test_stale_advance_conflicts_without_change(self) -> None:
        spi = MemDiscoverySpi()
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
        assert spi.state.registry_checkpoints["ransomlook"].cursor == "alpha"

    @pytest.mark.asyncio
    async def test_defer_preserves_cursor_and_raises_cooldown(self) -> None:
        spi = MemDiscoverySpi()
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
        assert checkpoint.last_completed_at == BASE_TIME
        assert checkpoint.cooldown_until == later

    @pytest.mark.asyncio
    async def test_defer_from_missing_sets_generation_one(self) -> None:
        spi = MemDiscoverySpi()
        async with spi.unit_of_work() as uow:
            checkpoint = await uow.discovery.defer_registry_checkpoint(
                "ransomlook", expected_generation=0, not_before=BASE_TIME
            )
            await uow.commit()
        assert checkpoint.generation == 1
        assert checkpoint.cursor is None
        assert checkpoint.cooldown_until == BASE_TIME

    @pytest.mark.asyncio
    async def test_rollback_discards_staged_checkpoint(self) -> None:
        spi = MemDiscoverySpi()
        async with spi.unit_of_work() as uow:
            await uow.discovery.advance_registry_checkpoint(
                "ransomlook",
                expected_generation=0,
                cursor="alpha",
                completed_at=BASE_TIME,
            )
            await uow.rollback()
        assert spi.state.registry_checkpoints == {}


def _real_adapter(registry: FakeSourceRegistry) -> RansomLookDiscoveryAdapter:
    return RansomLookDiscoveryAdapter(
        transport=registry, request_interval_seconds=0, sleep=_noop_sleep
    )


class _ConflictAfterTwo(DiscoveryProvider):
    def __init__(self, inner: DiscoveryProvider) -> None:
        self._inner = inner
        self.skipped_records = 0

    async def observations(self) -> AsyncIterator[DiscoveryInput]:
        count = 0
        async for claim in self._inner.observations():
            if count >= 2:
                from darkula.app.registry_polling import RegistryRecordConflict

                raise RegistryRecordConflict("injected contradictory record")
            count += 1
            yield claim


class _CancelAfterOne(DiscoveryProvider):
    def __init__(self, inner: DiscoveryProvider) -> None:
        self._inner = inner
        self.skipped_records = 0

    async def observations(self) -> AsyncIterator[DiscoveryInput]:
        count = 0
        async for claim in self._inner.observations():
            if count >= 1:
                raise asyncio.CancelledError()
            count += 1
            yield claim


class _WrapperAdapter:
    provider_id = "ransomlook"

    def __init__(
        self,
        inner: RansomLookDiscoveryAdapter,
        wrapper: Callable[[DiscoveryProvider], DiscoveryProvider],
    ) -> None:
        self._inner = inner
        self._wrapper = wrapper

    async def list_entry_ids(self) -> tuple[str, ...]:
        return ("blackgate",)

    def for_entry(self, entry_id: str, *, observed_at: datetime) -> DiscoveryProvider:
        return self._wrapper(self._inner.for_entry(entry_id, observed_at=observed_at))


class TestProviderDataConflicts:
    """T18/T19: contradictory provider records are permanent, replayable."""

    @staticmethod
    def _conflicting_registry() -> FakeSourceRegistry:
        return FakeSourceRegistry(
            index=("blackgate",),
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {"slug": "https://example.org/a"},
                            {
                                "slug": "https://example.org/a",
                                "updated": "2026-01-01T00:00:00Z",
                            },
                        ],
                    },
                    [],
                ]
            },
        )

    @pytest.mark.asyncio
    async def test_duplicate_conflict_is_permanent_without_advance(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        poller = _poller(spi, clock)
        result = await poller.poll_once(_real_adapter(self._conflicting_registry()))
        assert result.outcome is RegistryPollOutcome.PERMANENT_ERROR
        assert result.checkpoint_generation == 0
        assert spi.state.registry_checkpoints == {}
        assert len(spi.state.observations_by_key) == 2

    @pytest.mark.asyncio
    async def test_partial_conflict_then_corrected_rerun_replays(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        poller = _poller(spi, clock)

        wrapped = _WrapperAdapter(
            _real_adapter(self._conflicting_registry()), _ConflictAfterTwo
        )
        first = await poller.poll_once(wrapped)
        assert first.outcome is RegistryPollOutcome.PERMANENT_ERROR
        assert len(spi.state.observations_by_key) == 2
        assert spi.state.registry_checkpoints == {}

        corrected = FakeSourceRegistry(
            index=("blackgate",),
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [{"slug": "https://example.org/a"}],
                    },
                    [],
                ]
            },
        )
        second = await poller.poll_once(_real_adapter(corrected))
        assert second.outcome is RegistryPollOutcome.COMPLETED
        assert second.observations_replayed == 2
        assert second.observations_added == 0
        assert spi.state.registry_checkpoints["ransomlook"].cursor == "blackgate"

    @pytest.mark.asyncio
    async def test_cancel_after_partial_claim_does_not_defer(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        poller = _poller(spi, clock)
        wrapped = _WrapperAdapter(
            _real_adapter(FakeSourceRegistry(index=("blackgate",))), _CancelAfterOne
        )
        with pytest.raises(asyncio.CancelledError):
            await poller.poll_once(wrapped)
        assert spi.state.registry_checkpoints == {}


class TestRateLimitHeaders:
    """T22: malformed Retry-After on a detail GET stays bounded."""

    @pytest.mark.asyncio
    async def test_detail_rate_limit_with_invalid_header_uses_fallback(self) -> None:
        clock = _Clock()
        spi = MemDiscoverySpi()
        registry = FakeSourceRegistry(
            scripted=[
                ScriptedRegistryResponse(200, body=b'["blackgate"]'),
                ScriptedRegistryResponse(429, retry_after="9" * 10000),
            ]
        )
        result = await _poller(spi, clock).poll_once(
            RansomLookDiscoveryAdapter(
                transport=registry, request_interval_seconds=0, sleep=_noop_sleep
            )
        )
        assert result.outcome is RegistryPollOutcome.TEMPORARY_ERROR
        checkpoint = spi.state.registry_checkpoints["ransomlook"]
        assert checkpoint.cursor is None
        assert checkpoint.cooldown_until == BASE_TIME + timedelta(seconds=900)
