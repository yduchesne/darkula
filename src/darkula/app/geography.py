# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral geographic resolution (PR 12).

Geographic resolution interprets one persisted ``LOCATION`` occurrence. It is
a separate lifecycle from semantic extraction:

- extraction answers "the content mentions this place";
- resolution answers "which canonical place the mention maps to", with its own
  confidence and resolver provenance.

The service never mutates the occurrence it interprets, never uses an LLM,
never browses, and never holds a PostgreSQL transaction across ObjectStore or
resolver I/O. ``GeographicResolver`` is a Darkula-owned SPI: application code
never depends on a geocoder SDK, HTTP client, or provider response type.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from darkula.app.canonical_input import (
    CanonicalInputIntegrityError,
    CanonicalInputTooLargeError,
    CanonicalInputUnavailableError,
    load_canonical_text,
)
from darkula.app.object_store import ObjectStore
from darkula.app.persistence import ConflictError, DarkulaSpi
from darkula.domain.content import ContentArtifact
from darkula.domain.extraction import EntityType, ExtractedEntity
from darkula.domain.geography import (
    DEFAULT_CONTEXT_CHARS,
    GeographicResolution,
    GeographicResolutionRequest,
    GeographicResolutionStatus,
    GeographicResolverIdentity,
    GeographicResolverResult,
)
from darkula.domain.identifiers import (
    ExtractedEntityId,
    GeographicResolutionId,
    NormalizedContentId,
)
from darkula.telemetry.decorators import counted, timed, traced
from darkula.telemetry.metrics import get_counter


class GeographicResolutionError(RuntimeError):
    """Base class for bounded geographic-resolution failures."""


class GeographicResolutionNotFoundError(GeographicResolutionError):
    """The referenced extracted entity does not exist."""


class GeographicResolutionNotApplicableError(GeographicResolutionError):
    """The referenced occurrence is not a LOCATION mention."""


class GeographicResolutionInputUnavailableError(GeographicResolutionError):
    """The canonical representation is missing/unavailable in ObjectStore."""


class GeographicResolutionIntegrityError(GeographicResolutionError):
    """Provenance no longer matches the canonical bytes."""


class GeographicResolutionTooLargeError(GeographicResolutionError):
    """The representation exceeds the resolution input bound."""


class GeographicResolutionResolverError(GeographicResolutionError):
    """The external resolver failed (bounded, provider-neutral)."""


class GeographicResolutionContractError(GeographicResolutionError):
    """The resolver violated its provider-neutral contract."""


class GeographicResolutionPersistenceError(GeographicResolutionError):
    """A resolution could not be reloaded/persisted consistently."""


class GeographicResolver(ABC):
    """Darkula-owned provider-neutral geographic resolver SPI."""

    @property
    @abstractmethod
    def identity(self) -> GeographicResolverIdentity:
        """Return the bounded logical resolver identity/version."""
        raise NotImplementedError

    @abstractmethod
    async def resolve(
        self, request: GeographicResolutionRequest
    ) -> GeographicResolverResult:
        """Resolve one bounded location mention to a provider-neutral result.

        Implementations must not return raw provider/SDK/HTTP objects and must
        propagate ``asyncio.CancelledError`` unchanged.
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class GeographicResolutionServiceResult:
    """Outcome of one :meth:`GeographicResolutionService.resolve` call."""

    resolution: GeographicResolution
    created_resolution: bool


class GeographicResolutionService:
    """Resolve LOCATION occurrences with bounded context and atomic persistence."""

    def __init__(
        self,
        *,
        spi: DarkulaSpi,
        object_store: ObjectStore,
        resolver: GeographicResolver,
        context_chars: int = DEFAULT_CONTEXT_CHARS,
        max_input_bytes: int,
    ) -> None:
        if context_chars < 0:
            raise ValueError("context_chars must be >= 0")
        if max_input_bytes < 1:
            raise ValueError("max_input_bytes must be >= 1")
        self._spi = spi
        self._object_store = object_store
        self._resolver = resolver
        self._context_chars = context_chars
        self._max_input_bytes = max_input_bytes

    @counted(metric="darkula.geographic_resolution.executions")
    @timed(metric="darkula.geographic_resolution.duration")
    @traced(span_name="geographic_resolution.resolve")
    async def resolve(
        self, entity_id: ExtractedEntityId
    ) -> GeographicResolutionServiceResult:
        """Resolve one persisted LOCATION occurrence."""
        entity, existing = await self._load_entity(entity_id)
        if existing is not None:
            get_counter("darkula.geographic_resolution.status").add(
                1, {"status": existing.status.value}
            )
            return GeographicResolutionServiceResult(
                resolution=existing, created_resolution=False
            )

        if entity.entity_type is not EntityType.LOCATION:
            raise GeographicResolutionNotApplicableError(
                "geographic resolution applies only to LOCATION occurrences"
            )

        content_id = entity.content_id
        artifact = await self._load_artifact(content_id)
        text = await self._load_text(artifact)
        request = self._build_request(entity, text)
        result = await self._invoke_resolver(request)
        resolution = GeographicResolution(
            resolution_id=GeographicResolutionId.generate(),
            extracted_entity_id=entity.entity_id,
            status=result.status,
            resolver=self._resolver.identity,
            resolved_at=datetime.now(UTC),
            canonical_name=result.canonical_name,
            country_code=result.country_code,
            administrative_area=result.administrative_area,
            locality=result.locality,
            latitude=result.latitude,
            longitude=result.longitude,
            confidence=result.confidence,
            resolver_reference=result.reference,
        )
        outcome = await self._persist(resolution)
        get_counter("darkula.geographic_resolution.status").add(
            1, {"status": outcome.resolution.status.value}
        )
        return outcome

    async def _load_entity(
        self, entity_id: ExtractedEntityId
    ) -> tuple[ExtractedEntity, GeographicResolution | None]:
        async with self._spi.unit_of_work() as uow:
            entity = await uow.extraction.get_entity(entity_id)
            if entity is None:
                raise GeographicResolutionNotFoundError(
                    "the extracted entity was not found"
                )
            existing = await uow.geography.get_for_entity(
                entity_id,
                self._resolver.identity.name,
                self._resolver.identity.version,
            )
            return entity, existing

    async def _load_artifact(self, content_id: NormalizedContentId) -> ContentArtifact:
        async with self._spi.unit_of_work() as uow:
            content = await uow.content.get_observation(content_id)
            if content is None or content.artifact_id is None:
                raise GeographicResolutionInputUnavailableError(
                    "content has no referenced canonical artifact"
                )
            artifact = await uow.content.get_artifact(content.artifact_id)
        if artifact is None:
            raise GeographicResolutionInputUnavailableError(
                "canonical artifact metadata was not found"
            )
        return artifact

    async def _load_text(self, artifact: ContentArtifact) -> str:
        try:
            return await load_canonical_text(
                self._object_store, artifact, max_bytes=self._max_input_bytes
            )
        except CanonicalInputUnavailableError as exc:
            raise GeographicResolutionInputUnavailableError(
                "canonical representation is unavailable"
            ) from exc
        except CanonicalInputTooLargeError as exc:
            raise GeographicResolutionTooLargeError(
                "canonical representation exceeds the resolution input bound"
            ) from exc
        except CanonicalInputIntegrityError as exc:
            raise GeographicResolutionIntegrityError(
                "canonical representation failed integrity verification"
            ) from exc

    def _build_request(
        self, entity: ExtractedEntity, text: str
    ) -> GeographicResolutionRequest:
        span = entity.source_span
        if span.end > len(text) or text[span.start : span.end] != entity.raw_value:
            raise GeographicResolutionIntegrityError(
                "the LOCATION occurrence no longer matches the canonical text"
            )
        left = text[max(0, span.start - self._context_chars) : span.start]
        right = text[span.end : span.end + self._context_chars]
        return GeographicResolutionRequest(
            mention=entity.raw_value,
            left_context=left,
            right_context=right,
        )

    async def _invoke_resolver(
        self, request: GeographicResolutionRequest
    ) -> GeographicResolverResult:
        try:
            result = await self._resolver.resolve(request)
        except asyncio.CancelledError:
            raise
        except GeographicResolutionError:
            raise
        except Exception as exc:  # resolver boundary: map to bounded error
            raise GeographicResolutionResolverError(
                "the geographic resolver failed"
            ) from exc
        if not isinstance(result, GeographicResolverResult):
            raise GeographicResolutionContractError(
                "the resolver returned a non-provider-neutral result"
            )
        return result

    async def _persist(
        self, resolution: GeographicResolution
    ) -> GeographicResolutionServiceResult:
        async with self._spi.unit_of_work() as uow:
            winner = await uow.geography.get_for_entity(
                resolution.extracted_entity_id,
                resolution.resolver.name,
                resolution.resolver.version,
            )
            if winner is not None:
                return GeographicResolutionServiceResult(
                    resolution=winner, created_resolution=False
                )
            lost_race = False
            try:
                await uow.geography.create(resolution)
            except ConflictError:
                await uow.rollback()
                lost_race = True
            else:
                await uow.commit()
        if lost_race:
            return await self._load_existing(resolution)
        return GeographicResolutionServiceResult(
            resolution=resolution, created_resolution=True
        )

    async def _load_existing(
        self, resolution: GeographicResolution
    ) -> GeographicResolutionServiceResult:
        async with self._spi.unit_of_work() as uow:
            existing = await uow.geography.get_for_entity(
                resolution.extracted_entity_id,
                resolution.resolver.name,
                resolution.resolver.version,
            )
            if existing is None:
                raise GeographicResolutionPersistenceError(
                    "concurrent geographic resolution could not be reloaded"
                )
            return GeographicResolutionServiceResult(
                resolution=existing, created_resolution=False
            )


__all__ = [
    "GeographicResolutionContractError",
    "GeographicResolutionError",
    "GeographicResolutionInputUnavailableError",
    "GeographicResolutionIntegrityError",
    "GeographicResolutionNotApplicableError",
    "GeographicResolutionNotFoundError",
    "GeographicResolutionPersistenceError",
    "GeographicResolutionResolverError",
    "GeographicResolutionService",
    "GeographicResolutionServiceResult",
    "GeographicResolutionStatus",
    "GeographicResolutionTooLargeError",
    "GeographicResolver",
]
