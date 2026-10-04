# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula content-ingestion orchestration (PR 8).

Ties the deterministic normalizer (ObjectStore I/O) to a short PostgreSQL
unit of work, preserving the frozen ordering (section 25):

```text
validate + normalize + hash
        -> ObjectStore stat/put          (never inside a DB transaction)
        -> short PostgreSQL UoW
        -> persist artifact metadata + normalized provenance atomically
        -> commit
```

Artifact records are deduplicated by representation identity
(``(hash_algorithm, hash_digest, kind, completeness)``): when the physical
object already exists the existing artifact record is reused, and a new
provenance-bearing observation is still persisted (deduplication never merges
provenance). Durable observation idempotency uses the provenance identity
``(crawl_request_id, observation_index, source_uri)``: a retried observation
is reported as already-persisted rather than creating a duplicate record.

If database persistence fails after a newly deduplicated object was stored in
ObjectStore, the object may be temporarily unreferenced — an accepted,
documented operational condition (orphan cleanup is future work; PR 8 needs
no distributed transactions).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from darkula.app.normalization import ContentNormalizer, NormalizationResult
from darkula.app.persistence import ConflictError, DarkulaSpi, UnitOfWork
from darkula.domain.content import (
    ContentArtifact,
    ContentObservation,
    NormalizedContent,
)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of one :meth:`ContentIngestService.ingest` call.

    ``normalized_content`` is the durable canonical representation;
    ``artifact`` is the (possibly reused) content artifact record;
    ``deduplicated`` says whether the physical object already existed;
    ``created_observation`` is False when the provenance identity was already
    persisted (an idempotent retry) and True when a new observation was
    recorded.
    """

    normalized_content: NormalizedContent
    artifact: ContentArtifact | None
    deduplicated: bool
    created_observation: bool


class ContentIngestService:
    """Application service: normalizer + ObjectStore + short persistence UoW.

    Holds no database connection outside a short working transaction and
    never performs ObjectStore/network I/O inside a PostgreSQL transaction.
    """

    def __init__(
        self,
        *,
        normalizer: ContentNormalizer,
        spi: DarkulaSpi,
    ) -> None:
        self._normalizer = normalizer
        self._spi = spi

    async def ingest(self, observation: ContentObservation) -> IngestResult:
        """Normalize (ObjectStore I/O), then persist atomically in a short UoW."""
        result = await self._normalizer.normalize(observation)
        return await self._persist(result)

    async def _persist(self, result: NormalizationResult) -> IngestResult:
        artifact = result.artifact
        normalized = result.normalized_content

        if artifact is None:
            # No stored representation; persist the observation alone.
            created = await self._persist_observation(normalized)
            return IngestResult(
                normalized_content=normalized,
                artifact=None,
                deduplicated=False,
                created_observation=created,
            )

        async with self._spi.unit_of_work() as uow:
            existing = await uow.content.find_artifact_by_representation(
                content_hash=artifact.content_hash,  # type: ignore[arg-type]
                kind=artifact.artifact_kind,
                completeness=artifact.completeness,
            )
            if existing is not None:
                effective_artifact = existing
                normalized = replace(normalized, artifact_id=existing.artifact_id)
            else:
                effective_artifact = artifact
                await uow.content.create_artifact(artifact)
            created = await self._create_observation(uow, normalized)

        return IngestResult(
            normalized_content=normalized,
            artifact=effective_artifact,
            deduplicated=result.deduplicated,
            created_observation=created,
        )

    async def _persist_observation(self, normalized: NormalizedContent) -> bool:
        async with self._spi.unit_of_work() as uow:
            return await self._create_observation(uow, normalized)

    async def _create_observation(
        self, uow: UnitOfWork, content: NormalizedContent
    ) -> bool:
        """Create one observation; a duplicate provenance identity is an
        idempotent no-op (returns False), never a duplicate record."""
        try:
            await uow.content.create_observation(content)
        except ConflictError:
            return False
        await uow.commit()
        return True


__all__ = ["ContentIngestService", "IngestResult"]
