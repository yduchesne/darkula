# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral one-shot registry polling (PR 18).

``RegistryDiscoveryAdapter`` is deliberately tiny: one allowlisted registry
exposes a bounded list of entry IDs and, per selected entry, one existing
PR 17 :class:`~darkula.app.source_discovery.DiscoveryProvider` whose
``observations()`` yields that entry's bounded :class:`DiscoveryInput`
claims.

``RegistryPoller.poll_once`` implements the frozen ordering invariant:

    entry selected -> all accepted observations committed (exact PR 17
    replay) -> compare-and-swap checkpoint advance -> next entry

It never creates a candidate, source, endpoint, policy, run, event, outbox
record, credential, or collection authorization. Registry data remain
untrusted claims.

Network I/O happens strictly **outside** every persistence unit of work:

- read checkpoint (short UoW) -> close;
- list index (HTTP, no UoW);
- per observation: semantic-key read (short UoW) -> close -> ingest
  (PR 17's own short UoW);
- per completed entry: checkpoint CAS (short UoW).

``asyncio.CancelledError`` propagates unchanged; cancellation cleanup never
advances or defers the checkpoint.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from darkula.app.persistence import ConflictError, DarkulaSpi
from darkula.app.source_discovery import DiscoveryProvider, SourceDiscoveryService
from darkula.domain.discovery import (
    DiscoveryInput,
    DiscoveryObservation,
)
from darkula.domain.registry_polling import (
    RegistryPollCheckpoint,
    validate_registry_cursor,
)

#: Hard independent ceiling regardless of operator configuration.
MAX_ENTRIES_PER_RUN = 10
#: Default cooldown after a retryable provider failure.
DEFAULT_DEFER_SECONDS = 900
#: One retry after a retryable 5xx/transport failure.
RETRY_DELAY_SECONDS = 0.5


class RegistryPollOutcome(StrEnum):
    """Finite, content-free poll outcome vocabulary."""

    COMPLETED = "COMPLETED"
    COOLDOWN = "COOLDOWN"
    CHECKPOINT_RACE = "CHECKPOINT_RACE"
    TEMPORARY_ERROR = "TEMPORARY_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


class RegistryRecordSkipReason(StrEnum):
    """Bounded per-record skip reasons (never a raw provider value)."""

    MISSING_REFERENCE = "MISSING_REFERENCE"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    PRIVATE = "PRIVATE"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    PR17_VALIDATION = "PR17_VALIDATION"


# --- Bounded error classification (never response bodies) ---------------


class RegistryError(RuntimeError):
    """Base bounded registry-adapter failure (no provider payload)."""


class RegistrySchemaError(RegistryError):
    """Provider response does not match the documented, bounded schema."""


class RegistryTransportError(RegistryError):
    """Transport failure after the single bounded retry (temporary)."""


class RegistryEntryNotFound(RegistryTransportError):
    """A detail endpoint still listed in the index returned 404 (temporary)."""


class RegistryAccessDenied(RegistryError):
    """The provider denied anonymous read access (permanent, no workaround)."""


class RegistryRateLimited(RegistryError):
    """The provider returned 429; carries a bounded cooldown."""

    def __init__(self, retry_after_seconds: int) -> None:
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be >= 0")
        super().__init__("registry provider rate limited the request")
        self.retry_after_seconds = retry_after_seconds


class RegistryRecordConflict(RegistryError):
    """One detail response carries contradictory records for one identity."""


class RegistryCheckpointConflict(RegistryError):
    """A compare-and-swap checkpoint update lost to another writer."""


@runtime_checkable
class RecordSkipReporter(Protocol):
    """Optional provider capability: bounded per-record skip count."""

    @property
    def skipped_records(self) -> int: ...


class RegistryDiscoveryAdapter(Protocol):
    """Provider-neutral registry adapter contract (PR 18)."""

    @property
    def provider_id(self) -> str: ...

    async def list_entry_ids(self) -> tuple[str, ...]: ...

    def for_entry(
        self, entry_id: str, *, observed_at: datetime
    ) -> DiscoveryProvider: ...


@dataclass(frozen=True, slots=True)
class RegistryPollResult:
    """Bounded, content-free summary of one one-shot poll."""

    provider_id: str
    entries_selected: int
    entries_completed: int
    observations_added: int
    observations_replayed: int
    records_skipped: int
    checkpoint_generation: int
    outcome: RegistryPollOutcome


class RegistryPoller:
    """Deterministic one-shot registry poller (no scheduler, no daemon)."""

    def __init__(
        self,
        *,
        persistence: DarkulaSpi,
        discovery_service: SourceDiscoveryService,
        clock: Callable[[], datetime],
        logger: logging.Logger | None = None,
    ) -> None:
        self._persistence = persistence
        self._service = discovery_service
        self._clock = clock
        self._logger = logger

    async def _read_checkpoint(self, provider_id: str) -> RegistryPollCheckpoint:
        async with self._persistence.unit_of_work() as uow:
            return await uow.discovery.get_registry_checkpoint(provider_id)

    async def _read_observation(
        self, claim: DiscoveryInput
    ) -> DiscoveryObservation | None:
        async with self._persistence.unit_of_work() as uow:
            return await uow.discovery.get_by_key(
                provider_id=claim.provider_id,
                provider_record_id=claim.provider_record_id,
                provider_record_version=claim.provider_record_version,
                detection_method=claim.detection_method,
                reference_kind=claim.reference_kind,
            )

    async def _ingest_exact(
        self, claim: DiscoveryInput
    ) -> tuple[DiscoveryObservation, bool]:
        """Ingest one claim, reusing the stored ``observed_at`` on replay.

        PR 17's immutable replay tuple includes ``observed_at`` while the
        registry index/detail need not supply a stable observation time.
        Reading the semantic key first and replacing the claim's timestamp
        with the stored one keeps the exact replay semantics unchanged.
        Returns ``(observation, replayed)``.
        """
        existing = await self._read_observation(claim)
        if existing is not None:
            adjusted = replace(claim, observed_at=existing.observed_at)
            return await self._service.ingest(adjusted), True
        try:
            return await self._service.ingest(claim), False
        except ConflictError:
            # A concurrent first writer won the same semantic tuple. Read
            # exactly once and replay; never loop.
            existing = await self._read_observation(claim)
            if existing is None:
                raise
            adjusted = replace(claim, observed_at=existing.observed_at)
            return await self._service.ingest(adjusted), True

    async def _advance(
        self,
        provider_id: str,
        *,
        expected_generation: int,
        cursor: str | None,
    ) -> RegistryPollCheckpoint:
        async with self._persistence.unit_of_work() as uow:
            checkpoint = await uow.discovery.advance_registry_checkpoint(
                provider_id,
                expected_generation=expected_generation,
                cursor=cursor,
                completed_at=self._clock(),
            )
            await uow.commit()
            return checkpoint

    async def _defer(
        self,
        provider_id: str,
        *,
        expected_generation: int,
        not_before: datetime,
    ) -> RegistryPollCheckpoint | None:
        try:
            async with self._persistence.unit_of_work() as uow:
                checkpoint = await uow.discovery.defer_registry_checkpoint(
                    provider_id,
                    expected_generation=expected_generation,
                    not_before=not_before,
                )
                await uow.commit()
                return checkpoint
        except ConflictError:
            return None

    @staticmethod
    def _sort_key(name: str) -> bytes:
        return name.encode("utf-8")

    def _select(
        self,
        names: tuple[str, ...],
        cursor: str | None,
        *,
        max_entries: int,
    ) -> tuple[tuple[str, ...], bool]:
        ordered = sorted(names, key=self._sort_key)
        if cursor is not None:
            cursor_key = self._sort_key(validate_registry_cursor(cursor))
            ordered = [name for name in ordered if self._sort_key(name) > cursor_key]
        limit = min(max(0, max_entries), MAX_ENTRIES_PER_RUN)
        selected = tuple(ordered[:limit])
        exhausted = cursor is not None and not selected
        return selected, exhausted

    async def poll_once(
        self,
        adapter: RegistryDiscoveryAdapter,
        *,
        max_entries: int = MAX_ENTRIES_PER_RUN,
    ) -> RegistryPollResult:
        """Execute exactly one bounded poll pass.

        Returns a content-free result. Checkpoint CAS loss returns
        ``CHECKPOINT_RACE`` without overwriting the other writer's state.
        A same-key payload collision raises the existing ``ConflictError``
        with the current entry's checkpoint unchanged.
        """
        result = await self._poll_once(adapter, max_entries=max_entries)
        self._emit(result)
        return result

    def _emit(self, result: RegistryPollResult) -> None:
        """Emit one content-free operational summary (no provider values)."""
        logger = self._logger or logging.getLogger(__name__)
        logger.info(
            "registry poll provider=%s outcome=%s entries_selected=%d "
            "entries_completed=%d observations_added=%d "
            "observations_replayed=%d records_skipped=%d "
            "checkpoint_generation=%d",
            result.provider_id,
            result.outcome.value,
            result.entries_selected,
            result.entries_completed,
            result.observations_added,
            result.observations_replayed,
            result.records_skipped,
            result.checkpoint_generation,
        )

    async def _poll_once(
        self,
        adapter: RegistryDiscoveryAdapter,
        *,
        max_entries: int = MAX_ENTRIES_PER_RUN,
    ) -> RegistryPollResult:
        provider_id = adapter.provider_id
        checkpoint = await self._read_checkpoint(provider_id)
        now = self._clock()
        if checkpoint.cooldown_until is not None and checkpoint.cooldown_until > now:
            return RegistryPollResult(
                provider_id=provider_id,
                entries_selected=0,
                entries_completed=0,
                observations_added=0,
                observations_replayed=0,
                records_skipped=0,
                checkpoint_generation=checkpoint.generation,
                outcome=RegistryPollOutcome.COOLDOWN,
            )

        try:
            names = await adapter.list_entry_ids()
        except RegistryRateLimited as exc:
            deferred = await self._defer(
                provider_id,
                expected_generation=checkpoint.generation,
                not_before=self._clock() + timedelta(seconds=exc.retry_after_seconds),
            )
            return self._result(
                provider_id,
                self._generation_after_defer(checkpoint.generation, deferred),
                RegistryPollOutcome.TEMPORARY_ERROR,
            )
        except RegistryEntryNotFound:
            deferred = await self._defer(
                provider_id,
                expected_generation=checkpoint.generation,
                not_before=self._clock() + timedelta(seconds=DEFAULT_DEFER_SECONDS),
            )
            return self._result(
                provider_id,
                self._generation_after_defer(checkpoint.generation, deferred),
                RegistryPollOutcome.TEMPORARY_ERROR,
            )
        except RegistryTransportError:
            deferred = await self._defer(
                provider_id,
                expected_generation=checkpoint.generation,
                not_before=self._clock() + timedelta(seconds=DEFAULT_DEFER_SECONDS),
            )
            return self._result(
                provider_id,
                self._generation_after_defer(checkpoint.generation, deferred),
                RegistryPollOutcome.TEMPORARY_ERROR,
            )
        except RegistryAccessDenied:
            return self._result(
                provider_id, checkpoint.generation, RegistryPollOutcome.PERMANENT_ERROR
            )
        except RegistrySchemaError:
            return self._result(
                provider_id, checkpoint.generation, RegistryPollOutcome.PERMANENT_ERROR
            )

        selected, exhausted = self._select(
            names, checkpoint.cursor, max_entries=max_entries
        )
        if exhausted:
            try:
                advanced = await self._advance(
                    provider_id,
                    expected_generation=checkpoint.generation,
                    cursor=None,
                )
            except ConflictError:
                return self._result(
                    provider_id,
                    checkpoint.generation,
                    RegistryPollOutcome.CHECKPOINT_RACE,
                )
            return RegistryPollResult(
                provider_id=provider_id,
                entries_selected=0,
                entries_completed=0,
                observations_added=0,
                observations_replayed=0,
                records_skipped=0,
                checkpoint_generation=advanced.generation,
                outcome=RegistryPollOutcome.COMPLETED,
            )

        entries_completed = 0
        added = 0
        replayed = 0
        skipped = 0
        generation = checkpoint.generation

        for entry_id in selected:
            provider = adapter.for_entry(entry_id, observed_at=self._clock())
            try:
                async for claim in provider.observations():
                    observation, was_replayed = await self._ingest_exact(claim)
                    del observation
                    if was_replayed:
                        replayed += 1
                    else:
                        added += 1
            except RegistryRateLimited as exc:
                deferred = await self._defer(
                    provider_id,
                    expected_generation=generation,
                    not_before=self._clock()
                    + timedelta(seconds=exc.retry_after_seconds),
                )
                return RegistryPollResult(
                    provider_id=provider_id,
                    entries_selected=len(selected),
                    entries_completed=entries_completed,
                    observations_added=added,
                    observations_replayed=replayed,
                    records_skipped=skipped,
                    checkpoint_generation=self._generation_after_defer(
                        generation, deferred
                    ),
                    outcome=RegistryPollOutcome.TEMPORARY_ERROR,
                )
            except RegistryEntryNotFound:
                deferred = await self._defer(
                    provider_id,
                    expected_generation=generation,
                    not_before=self._clock() + timedelta(seconds=DEFAULT_DEFER_SECONDS),
                )
                return RegistryPollResult(
                    provider_id=provider_id,
                    entries_selected=len(selected),
                    entries_completed=entries_completed,
                    observations_added=added,
                    observations_replayed=replayed,
                    records_skipped=skipped,
                    checkpoint_generation=self._generation_after_defer(
                        generation, deferred
                    ),
                    outcome=RegistryPollOutcome.TEMPORARY_ERROR,
                )
            except RegistryTransportError:
                deferred = await self._defer(
                    provider_id,
                    expected_generation=generation,
                    not_before=self._clock() + timedelta(seconds=DEFAULT_DEFER_SECONDS),
                )
                return RegistryPollResult(
                    provider_id=provider_id,
                    entries_selected=len(selected),
                    entries_completed=entries_completed,
                    observations_added=added,
                    observations_replayed=replayed,
                    records_skipped=skipped,
                    checkpoint_generation=self._generation_after_defer(
                        generation, deferred
                    ),
                    outcome=RegistryPollOutcome.TEMPORARY_ERROR,
                )
            except RegistryAccessDenied:
                return RegistryPollResult(
                    provider_id=provider_id,
                    entries_selected=len(selected),
                    entries_completed=entries_completed,
                    observations_added=added,
                    observations_replayed=replayed,
                    records_skipped=skipped,
                    checkpoint_generation=generation,
                    outcome=RegistryPollOutcome.PERMANENT_ERROR,
                )
            except RegistrySchemaError:
                return RegistryPollResult(
                    provider_id=provider_id,
                    entries_selected=len(selected),
                    entries_completed=entries_completed,
                    observations_added=added,
                    observations_replayed=replayed,
                    records_skipped=skipped,
                    checkpoint_generation=generation,
                    outcome=RegistryPollOutcome.PERMANENT_ERROR,
                )
            if isinstance(provider, RecordSkipReporter):
                skipped += provider.skipped_records
            try:
                advanced = await self._advance(
                    provider_id,
                    expected_generation=generation,
                    cursor=entry_id,
                )
            except ConflictError:
                return RegistryPollResult(
                    provider_id=provider_id,
                    entries_selected=len(selected),
                    entries_completed=entries_completed,
                    observations_added=added,
                    observations_replayed=replayed,
                    records_skipped=skipped,
                    checkpoint_generation=generation,
                    outcome=RegistryPollOutcome.CHECKPOINT_RACE,
                )
            generation = advanced.generation
            entries_completed += 1

        return RegistryPollResult(
            provider_id=provider_id,
            entries_selected=len(selected),
            entries_completed=entries_completed,
            observations_added=added,
            observations_replayed=replayed,
            records_skipped=skipped,
            checkpoint_generation=generation,
            outcome=RegistryPollOutcome.COMPLETED,
        )

    @staticmethod
    def _generation_after_defer(
        base_generation: int, checkpoint: RegistryPollCheckpoint | None
    ) -> int:
        return base_generation if checkpoint is None else checkpoint.generation

    @staticmethod
    def _result(
        provider_id: str, generation: int, outcome: RegistryPollOutcome
    ) -> RegistryPollResult:
        return RegistryPollResult(
            provider_id=provider_id,
            entries_selected=0,
            entries_completed=0,
            observations_added=0,
            observations_replayed=0,
            records_skipped=0,
            checkpoint_generation=generation,
            outcome=outcome,
        )


__all__ = [
    "DEFAULT_DEFER_SECONDS",
    "MAX_ENTRIES_PER_RUN",
    "RETRY_DELAY_SECONDS",
    "RecordSkipReporter",
    "RegistryAccessDenied",
    "RegistryCheckpointConflict",
    "RegistryDiscoveryAdapter",
    "RegistryEntryNotFound",
    "RegistryError",
    "RegistryPollOutcome",
    "RegistryPollResult",
    "RegistryPoller",
    "RegistryRateLimited",
    "RegistryRecordConflict",
    "RegistryRecordSkipReason",
    "RegistrySchemaError",
    "RegistryTransportError",
]
