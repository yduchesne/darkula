# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula deterministic content normalization boundary (PR 8).

One application-facing capability (section 3): converts a bounded
:class:`~darkula.domain.content.ContentObservation` into a
:class:`NormalizationResult` carrying a provenance-bearing
:class:`~darkula.domain.content.NormalizedContent` plus the
:class:`~darkula.domain.content.ContentArtifact` reference for the
representation that ObjectStore now owns.

Rules frozen here:

- normalization is **deterministic**: no LLM, no wall clock, no random input
  affects the normalized text/hash; the same supported input yields the same
  canonical bytes and hash (N1-N8);
- normalization is **not a trust promotion**: normalized text, titles, URIs,
  metadata, and artifact bytes remain hostile/untrusted data (N9-N10);
- completeness is explicit and never inferred from size/media type; a bounded
  page excerpt is SAMPLE and never a complete original (N11-N12);
- forbidden credentials/cookies/session material is rejected from structural
  metadata and never persisted (N13);
- ``asyncio.CancelledError`` propagates (N14);
- unsupported/invalid/oversized input fails with bounded typed errors.

Normalization is deliberately NOT extraction/analysis: it performs no
entity/relationship/geographic/classification reasoning (that is PR 11-14).
It performs no collection orchestration (PR 9).
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace

from darkula.app.artifacts import ArtifactStorageService
from darkula.app.object_store import ContentHash, ContentType
from darkula.domain.content import (
    MAX_ARTIFACT_BYTES,
    MAX_NORMALIZED_TEXT_BYTES,
    NORMALIZATION_VERSION,
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    ContentObservation,
    NormalizedContent,
)
from darkula.domain.identifiers import NormalizedContentId
from darkula.telemetry.decorators import counted, timed, traced

#: Canonical ``ContentType`` for a synthesized normalized-text artifact.
NORMALIZED_TEXT_CONTENT_TYPE = ContentType("text/plain")

#: Control characters removed during text canonicalization. Only ``\\n`` and
#: ``\\t`` are preserved (line structure and tabular layout); every other C0
#: control and DEL is deterministically removed. This is a deterministic
#: policy applied to hostile data, not an interpretation of it.
_DISALLOWED_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_SHA256_ALGORITHM = "sha-256"


class NormalizationError(RuntimeError):
    """Bounded, provider-neutral normalization failure.

    Public messages never echo hostile content or credentials.
    """


class UnsupportedContentError(NormalizationError):
    """The representation/media type is not supported by this normalizer."""


class InvalidContentObservationError(NormalizationError):
    """The observation is malformed and cannot be normalized."""


class ContentTooLargeError(NormalizationError):
    """The observation or normalized representation exceeds configured bounds."""


def _sha256_hex(data: bytes) -> ContentHash:
    """Return the Darkula sha-256 representation hash of ``data``."""
    return ContentHash(
        algorithm=_SHA256_ALGORITHM, digest_hex=hashlib.sha256(data).hexdigest()
    )


def canonicalize_text(text: str) -> str:
    """Return the deterministic canonicalized text representation.

    - validates the input is str;
    - normalizes line endings to ``\\n`` (CRLF and lone CR become LF);
    - removes disallowed C0/DEL control characters (preserving ``\\n``/``\\t``);
    - preserves meaningful non-ASCII/multilingual characters;
    - performs no model-driven rewriting, summarization, translation, or
      hidden-content interpretation.

    The same input always yields the same canonical bytes (N1-N3, N7).
    """
    if not isinstance(text, str):
        raise InvalidContentObservationError("normalized text must be a string")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return _DISALLOWED_CONTROLS.sub("", normalized)


def _validate_observation(observation: ContentObservation) -> None:
    """Validate normalization input bounds and required provenance fields."""
    # source_uri/crawl_request_id/observed_at are validated by the DTO; here
    # we enforce the byte-bound on normalized text up front (fail closed
    # before any allocation/storage).
    if (
        observation.text is not None
        and len(observation.text.encode("utf-8")) > MAX_NORMALIZED_TEXT_BYTES
    ):
        raise ContentTooLargeError(
            f"observation text exceeds the {MAX_NORMALIZED_TEXT_BYTES}-byte bound"
        )
    if (
        observation.body_bytes is not None
        and len(observation.body_bytes) > MAX_ARTIFACT_BYTES
    ):
        raise ContentTooLargeError(
            f"observation body exceeds the {MAX_ARTIFACT_BYTES}-byte bound"
        )


@dataclass(frozen=True, slots=True)
class NeutralNormalizationResult:
    """The offline, deterministic outcome of canonicalizing an observation.

    Produced without touching ObjectStore or persistence; the caller decides
    whether/how to store and persist the artifact.
    """

    normalized_content: NormalizedContent
    artifact_kind: ArtifactKind | None
    artifact_completeness: ArtifactCompleteness | None
    artifact_content_type: ContentType | None
    artifact_bytes: bytes | None


def canonicalize_observation(
    observation: ContentObservation,
    *,
    normalization_version: str = NORMALIZATION_VERSION,
) -> NeutralNormalizationResult:
    """Deterministically canonicalize one bounded observation (offline, pure).

    Returns the canonical normalized representation plus, when meaningful
    text or bounded bytes exist, the exact artifact representation to store
    (bytes + hash + kind + completeness + content type). No wall clock and no
    random input affect the bytes or the hash.
    """
    _validate_observation(observation)

    normalized_text: str | None = None
    content_hash: ContentHash | None = None
    if observation.text is not None:
        normalized_text = canonicalize_text(observation.text)
        content_hash = _sha256_hex(normalized_text.encode("utf-8"))
    elif observation.body_bytes is not None:
        content_hash = _sha256_hex(observation.body_bytes)

    normalized = NormalizedContent(
        content_id=NormalizedContentId.generate(),
        source_uri=observation.source_uri,
        observed_at=observation.observed_at,
        crawl_request_id=observation.crawl_request_id,
        observation_index=observation.observation_index,
        normalization_version=normalization_version,
        completeness=observation.completeness,
        title=observation.title,
        text=normalized_text,
        content_type=observation.media_type,
        content_hash=content_hash,
        artifact_id=None,
        structural_metadata=observation.structural_metadata,
    )

    # Decide the stored artifact representation.
    artifact_kind: ArtifactKind | None = None
    artifact_completeness: ArtifactCompleteness | None = None
    artifact_content_type: ContentType | None = None
    artifact_bytes: bytes | None = None

    if observation.body_bytes is not None and observation.artifact_kind is not None:
        artifact_kind = observation.artifact_kind
        artifact_completeness = observation.completeness
        artifact_content_type = observation.media_type
        artifact_bytes = observation.body_bytes
    elif normalized_text is not None and normalized_text != "":
        # Synthesized normalized-text representation: complete for the
        # synthesized canonical representation, never the original source
        # body. A bounded page excerpt therefore yields SAMPLE here.
        artifact_kind = ArtifactKind.NORMALIZED_TEXT
        artifact_completeness = observation.completeness
        artifact_content_type = NORMALIZED_TEXT_CONTENT_TYPE
        artifact_bytes = normalized_text.encode("utf-8")

    return NeutralNormalizationResult(
        normalized_content=normalized,
        artifact_kind=artifact_kind,
        artifact_completeness=artifact_completeness,
        artifact_content_type=artifact_content_type,
        artifact_bytes=artifact_bytes,
    )


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Outcome of one normalization + artifact-storage pass.

    ``normalized_content`` is the provenance-bearing canonical
    representation; ``artifact`` (when present) is the stored
    :class:`ContentArtifact` reference; ``deduplicated`` says whether the
    physical object already existed (provenance remains distinct regardless).
    """

    normalized_content: NormalizedContent
    artifact: ContentArtifact | None
    deduplicated: bool


class ContentNormalizer(ABC):
    """One Darkula-owned application-boundary normalization capability.

    Implementations are deterministic, never treat hostile content as
    instructions, and never place ObjectStore access inside any PostgreSQL
    transaction (the caller persists the result in a separate short UoW).
    """

    @abstractmethod
    async def normalize(
        self,
        observation: ContentObservation,
    ) -> NormalizationResult:
        """Validate, canonicalize, store the artifact, and return the result.

        ObjectStore I/O (if any) happens here; persistence is the caller's
        job in a short unit of work afterwards.
        """
        raise NotImplementedError


class DeterministicContentNormalizer(ContentNormalizer):
    """Provider-neutral deterministic normalizer over the artifact service.

    Depends only on :class:`ArtifactStorageService` (and therefore the
    existing ObjectStore SPI); holds no database connection and performs no
    collection orchestration.
    """

    def __init__(
        self,
        *,
        artifact_service: ArtifactStorageService,
        normalization_version: str = NORMALIZATION_VERSION,
    ) -> None:
        self._artifact_service = artifact_service
        self._normalization_version = normalization_version

    @counted(metric="content.normalize.count")
    @timed(metric="content.normalize.duration")
    @traced(span_name="content.normalize")
    async def normalize(
        self,
        observation: ContentObservation,
    ) -> NormalizationResult:
        neutral = canonicalize_observation(
            observation, normalization_version=self._normalization_version
        )
        if neutral.artifact_bytes is None:
            return NormalizationResult(
                normalized_content=neutral.normalized_content,
                artifact=None,
                deduplicated=False,
            )

        if neutral.artifact_kind is None or neutral.artifact_completeness is None:
            raise NormalizationError(
                "cannot store an artifact without an explicit kind/completeness"
            )
        artifact_hash = _sha256_hex(neutral.artifact_bytes)
        stored = await self._artifact_service.store(
            kind=neutral.artifact_kind,
            completeness=neutral.artifact_completeness,
            content_type=neutral.artifact_content_type,
            content_hash=artifact_hash,
            size_bytes=len(neutral.artifact_bytes),
            data=neutral.artifact_bytes,
            created_at=observation.observed_at,
        )
        normalized = replace(
            neutral.normalized_content,
            artifact_id=stored.artifact.artifact_id,
        )
        return NormalizationResult(
            normalized_content=normalized,
            artifact=stored.artifact,
            deduplicated=stored.deduplicated,
        )


__all__ = [
    "ContentNormalizer",
    "ContentTooLargeError",
    "DeterministicContentNormalizer",
    "InvalidContentObservationError",
    "NeutralNormalizationResult",
    "NormalizationError",
    "NormalizationResult",
    "UnsupportedContentError",
    "canonicalize_observation",
    "canonicalize_text",
]
