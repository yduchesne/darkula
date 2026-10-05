# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic extraction application service (PR 11).

Canonical flow (no PostgreSQL transaction ever spans ObjectStore I/O):

```text
short read UoW: load NormalizedContent + referenced ContentArtifact,
                existing result for (content, profile)
CLOSE DB transaction
ObjectStore: bounded streaming get + incremental SHA-256 / size / UTF-8 verify
deterministic extraction (no UoW open)
short write UoW: create result + ALL entities, commit atomically
```

Failure modes are bounded and provider-neutral; cancellation
(``asyncio.CancelledError``) propagates unchanged and leaves persistence
clean. The DB text preview is never used as an extraction source: a canonical
persisted ``NORMALIZED_TEXT`` ObjectStore representation is required, and its
absence/inconsistency fails closed rather than silently falling back.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from darkula.app.canonical_input import (
    CanonicalInputIntegrityError,
    CanonicalInputTooLargeError,
    CanonicalInputUnavailableError,
    is_extractable,
    load_canonical_text,
)
from darkula.app.extractors import (
    DETERMINISTIC_OBSERVABLES_PROFILE_NAME,
    DETERMINISTIC_OBSERVABLES_PROFILE_VERSION,
    DeterministicEntityExtractor,
    EntityMatch,
    deterministic_observables_extractors,
)
from darkula.app.object_store import ObjectStore
from darkula.app.persistence import ConflictError, DarkulaSpi
from darkula.domain.content import (
    MAX_NORMALIZED_TEXT_BYTES,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.extraction import (
    MAX_NORMALIZED_VALUE_LENGTH,
    MAX_RAW_VALUE_LENGTH,
    ExtractedEntity,
    ExtractionResult,
    ExtractorIdentity,
)
from darkula.domain.identifiers import (
    ExtractedEntityId,
    ExtractionResultId,
    NormalizedContentId,
)
from darkula.telemetry.decorators import counted, timed, traced
from darkula.telemetry.metrics import get_counter


class ExtractionError(RuntimeError):
    """Base class for bounded, provider-neutral extraction failures.

    Public messages never echo ObjectKey, URI, content, raw values, provider
    exception text, or credentials.
    """


class ExtractionContentNotFoundError(ExtractionError):
    """No normalized content observation exists for the requested identity."""


class ExtractionNotSupportedError(ExtractionError):
    """The content has no extractable canonical normalized-text artifact."""


class ExtractionInputUnavailableError(ExtractionError):
    """The canonical representation is missing/unavailable in ObjectStore."""


class ExtractionIntegrityError(ExtractionError):
    """The canonical bytes do not match persisted metadata or are not text."""


class ExtractionTooLargeError(ExtractionError):
    """The representation or extracted-entity count exceeds a hard bound."""


class ExtractionContractError(ExtractionError):
    """An extractor produced an internally inconsistent match."""


@dataclass(frozen=True, slots=True)
class DeterministicExtractionProfile:
    """A fixed, versioned deterministic extractor profile.

    ``run`` is pure: it validates exact spans, preserves distinct
    occurrences, deterministically deduplicates exact repeats, sorts by the
    frozen semantic order, and fails (never truncates) when the entity cap is
    exceeded.
    """

    profile_name: str
    profile_version: str
    extractors: tuple[DeterministicEntityExtractor, ...]
    max_entities: int

    @property
    def manifest(self) -> tuple[ExtractorIdentity, ...]:
        """Return the exact deterministic extractor manifest."""
        return tuple(
            sorted(
                (item.identity for item in self.extractors),
                key=lambda identity: (identity.name, identity.version),
            )
        )

    def run(self, text: str) -> tuple[EntityMatch, ...]:
        """Return the deterministic, validated, bounded match set for ``text``."""
        collected: list[EntityMatch] = []
        for extractor in self.extractors:
            for match in extractor.extract(text):
                span = match.source_span
                if (
                    span.end > len(text)
                    or text[span.start : span.end] != match.raw_value
                ):
                    raise ExtractionContractError(
                        "extractor produced a match inconsistent with the source span"
                    )
                if len(match.raw_value) > MAX_RAW_VALUE_LENGTH:
                    raise ExtractionContractError(
                        "extractor produced an overlong raw value"
                    )
                if len(match.normalized_value) > MAX_NORMALIZED_VALUE_LENGTH:
                    raise ExtractionContractError(
                        "extractor produced an overlong normalized value"
                    )
                collected.append(match)

        # Exact duplicate emitted twice by the same extractor is deduplicated;
        # the same value at a different span is a distinct occurrence.
        deduplicated: dict[tuple[object, ...], EntityMatch] = {}
        for match in collected:
            key = (
                match.entity_type,
                match.raw_value,
                match.normalized_value,
                match.source_span.start,
                match.source_span.end,
                match.extractor.name,
                match.extractor.version,
                match.subtype,
            )
            deduplicated.setdefault(key, match)

        ordered = tuple(
            sorted(
                deduplicated.values(),
                key=lambda item: (
                    item.source_span.start,
                    item.source_span.end,
                    item.entity_type.value,
                    item.normalized_value,
                    item.extractor.name,
                ),
            )
        )
        if len(ordered) > self.max_entities:
            raise ExtractionTooLargeError(
                "deterministic extraction exceeded the configured entity cap"
            )
        return ordered


def deterministic_observables_profile(
    *, max_entities: int
) -> DeterministicExtractionProfile:
    """Return the fixed ``deterministic-observables/v1`` profile."""
    if max_entities < 1:
        raise ValueError("max_entities must be >= 1")
    return DeterministicExtractionProfile(
        profile_name=DETERMINISTIC_OBSERVABLES_PROFILE_NAME,
        profile_version=DETERMINISTIC_OBSERVABLES_PROFILE_VERSION,
        extractors=deterministic_observables_extractors(),
        max_entities=max_entities,
    )


@dataclass(frozen=True, slots=True)
class ExtractionServiceResult:
    """Outcome of one :meth:`DeterministicExtractionService.extract` call.

    ``created_result`` is True when this call durably created the result and
    False when it reused an existing/concurrently-created durable result.
    """

    result: ExtractionResult
    entities: tuple[ExtractedEntity, ...]
    created_result: bool


class DeterministicExtractionService:
    """Canonical load + deterministic extraction + atomic persistence."""

    def __init__(
        self,
        *,
        spi: DarkulaSpi,
        object_store: ObjectStore,
        profile: DeterministicExtractionProfile,
    ) -> None:
        self._spi = spi
        self._object_store = object_store
        self._profile = profile

    @counted(metric="darkula.extraction.executions")
    @timed(metric="darkula.extraction.duration")
    @traced(span_name="extraction.extract")
    async def extract(self, content_id: NormalizedContentId) -> ExtractionServiceResult:
        """Extract deterministic observables from one canonical representation."""
        content, artifact, existing = await self._load_metadata(content_id)
        if existing is not None:
            return existing

        if artifact is None:
            raise ExtractionNotSupportedError(
                "content has no referenced canonical artifact"
            )
        if not _is_extractable(artifact):
            raise ExtractionNotSupportedError(
                "content has no extractable normalized-text representation"
            )

        text = await self._load_canonical_text(artifact)
        matches = self._profile.run(text)
        get_counter("darkula.extraction.entities").add(len(matches))
        return await self._persist(content, matches)

    # -- phase 1: short metadata read ------------------------------------
    async def _load_metadata(
        self, content_id: NormalizedContentId
    ) -> tuple[
        NormalizedContent,
        ContentArtifact | None,
        ExtractionServiceResult | None,
    ]:
        async with self._spi.unit_of_work() as uow:
            content = await uow.content.get_observation(content_id)
            if content is None:
                raise ExtractionContentNotFoundError("normalized content was not found")
            existing_result = await uow.extraction.get_result_by_profile(
                content_id,
                self._profile.profile_name,
                self._profile.profile_version,
            )
            if existing_result is not None:
                entities = await uow.extraction.list_entities_for_result(
                    existing_result.result_id
                )
                return (
                    content,
                    None,
                    ExtractionServiceResult(
                        result=existing_result,
                        entities=entities,
                        created_result=False,
                    ),
                )
            artifact: ContentArtifact | None = None
            if content.artifact_id is not None:
                artifact = await uow.content.get_artifact(content.artifact_id)
            return content, artifact, None

    # -- phase 2: ObjectStore bounded read + integrity verification ------
    async def _load_canonical_text(self, artifact: ContentArtifact) -> str:
        try:
            return await load_canonical_text(
                self._object_store, artifact, max_bytes=MAX_NORMALIZED_TEXT_BYTES
            )
        except CanonicalInputUnavailableError as exc:
            raise ExtractionInputUnavailableError(
                "canonical representation is unavailable"
            ) from exc
        except CanonicalInputTooLargeError as exc:
            raise ExtractionTooLargeError(
                "canonical representation exceeds the input byte bound"
            ) from exc
        except CanonicalInputIntegrityError as exc:
            raise ExtractionIntegrityError(
                "canonical representation failed integrity verification"
            ) from exc

    # -- phase 4: atomic persistence -------------------------------------
    async def _persist(
        self,
        content: NormalizedContent,
        matches: Sequence[EntityMatch],
    ) -> ExtractionServiceResult:
        profile = self._profile
        result = ExtractionResult(
            result_id=ExtractionResultId.generate(),
            content_id=content.content_id,
            profile_name=profile.profile_name,
            profile_version=profile.profile_version,
            extractor_manifest=profile.manifest,
            extracted_at=datetime.now(UTC),
            entity_count=len(matches),
        )
        entities = tuple(
            ExtractedEntity(
                entity_id=ExtractedEntityId.generate(),
                extraction_result_id=result.result_id,
                content_id=content.content_id,
                entity_type=match.entity_type,
                raw_value=match.raw_value,
                normalized_value=match.normalized_value,
                source_span=match.source_span,
                extractor=match.extractor,
                subtype=match.subtype,
            )
            for match in matches
        )

        async with self._spi.unit_of_work() as uow:
            winner = await uow.extraction.get_result_by_profile(
                content.content_id,
                profile.profile_name,
                profile.profile_version,
            )
            if winner is not None:
                winner_entities = await uow.extraction.list_entities_for_result(
                    winner.result_id
                )
                return ExtractionServiceResult(
                    result=winner,
                    entities=winner_entities,
                    created_result=False,
                )
            lost_race = False
            try:
                await uow.extraction.create_result(result)
            except ConflictError:
                # A concurrent worker committed the same semantic result.
                await uow.rollback()
                lost_race = True
            else:
                for entity in entities:
                    await uow.extraction.create_entity(entity)
                await uow.commit()
        if lost_race:
            return await self._load_existing(content.content_id)
        return ExtractionServiceResult(
            result=result, entities=entities, created_result=True
        )

    async def _load_existing(
        self, content_id: NormalizedContentId
    ) -> ExtractionServiceResult:
        """Reload a concurrently-created durable result after losing a race."""
        async with self._spi.unit_of_work() as uow:
            existing = await uow.extraction.get_result_by_profile(
                content_id,
                self._profile.profile_name,
                self._profile.profile_version,
            )
            if existing is None:
                raise ExtractionIntegrityError(
                    "concurrent extraction result could not be reloaded"
                )
            entities = await uow.extraction.list_entities_for_result(existing.result_id)
            return ExtractionServiceResult(
                result=existing,
                entities=entities,
                created_result=False,
            )


def _is_extractable(artifact: ContentArtifact) -> bool:
    """Return whether an artifact is the canonical normalized-text representation."""
    return is_extractable(artifact)


__all__ = [
    "DETERMINISTIC_OBSERVABLES_PROFILE_NAME",
    "DETERMINISTIC_OBSERVABLES_PROFILE_VERSION",
    "DeterministicExtractionProfile",
    "DeterministicExtractionService",
    "ExtractionContentNotFoundError",
    "ExtractionContractError",
    "ExtractionError",
    "ExtractionInputUnavailableError",
    "ExtractionIntegrityError",
    "ExtractionNotSupportedError",
    "ExtractionServiceResult",
    "ExtractionTooLargeError",
    "deterministic_observables_profile",
]
