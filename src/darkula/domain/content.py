# SPDX-License-Identifier: AGPL-3.0-only
"""Executable content-domain concepts (PR 8).

PR 8 freezes the deterministic trusted-side content-ingestion boundary that
converts bounded crawler observations into durable normalized content plus
ObjectStore-backed artifacts, with independent provenance:

- :class:`ContentObservation` — the bounded, Darkula-owned normalization
  input DTO (never a provider/runtime type);
- :class:`ContentArtifact` — a provider-neutral structured record of a stored
  artifact/reference (PostgreSQL owns identity/provenance; ObjectStore owns
  bytes);
- :class:`NormalizedContent` — the provenance-bearing canonical
  representation that later extraction (PR 11+) consumes.

Identity rules frozen here (section 1.4 of the PR 8 plan):

- ``NormalizedContent.id`` is Darkula semantic/persistence identity for one
  normalized observation;
- ``ContentArtifact.id`` is Darkula identity for one artifact reference/record;
- ``ObjectKey`` is a logical ObjectStore address, never identity or hash;
- ``ContentHash`` is representation/change identity for exact bytes;
- ``source URI + observation context`` is provenance, never storage identity.

Completeness semantics (section 6, step 1): the model explicitly separates a
complete representation from a bounded sample/excerpt. A `NORMALIZED_TEXT`
synthesized artifact may be COMPLETE **for the synthesized normalized
representation** without implying Darkula possesses the complete original
HTML/PDF/source body. A bounded crawler excerpt is SAMPLE and never a
complete original.

Normalized content remains untrusted data after the boundary; normalization
is deterministic and never treats hostile instructions as trusted commands.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from darkula.app.object_store import ContentHash, ContentType
from darkula.domain.identifiers import (
    ContentArtifactId,
    NormalizedContentId,
    ObjectKey,
)

#: Canonical bounding constants applied across the content normalization
#: boundary. Every hostile field is bounded before it can consume unbounded
#: memory, storage, or metadata.
MAX_TITLE_LENGTH = 512
MAX_URI_LENGTH = 2048
MAX_NORMALIZED_TEXT_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_METADATA_KEYS = 64
MAX_METADATA_KEY_LENGTH = 64
MAX_METADATA_VALUE_LENGTH = 512
MAX_OBSERVATION_INDEX = 10_000_000

#: Secret-like markers rejected in bounded structural metadata (parity with
#: ``darkula.config.loader.SECRET_MARKERS`` and ``darkula.domain.source``).
METADATA_SECRET_MARKERS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "auth",
    "cookie",
    "access_key",
    "session",
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ArtifactCompleteness(StrEnum):
    """Whether a representation is the complete artifact or a bounded sample.

    Completeness always applies to the specific representation being
    described, never to an unknown original source body.
    """

    COMPLETE = "COMPLETE"
    SAMPLE = "SAMPLE"


class ArtifactKind(StrEnum):
    """Role/kind of a stored artifact (only kinds PR 8 exercises exist).

    ``NORMALIZED_TEXT`` is the canonical representation Darkula synthesizes
    from observed text; ``DOWNLOAD_SAMPLE`` is a bounded sample of a
    crawler download (never the complete download). ``SOURCE_BODY`` is
    reserved for a future complete original body acquisition path and is not
    exercised by PR 8.
    """

    NORMALIZED_TEXT = "NORMALIZED_TEXT"
    SOURCE_BODY = "SOURCE_BODY"
    DOWNLOAD_SAMPLE = "DOWNLOAD_SAMPLE"


#: Normalization version string identifying the canonicalization semantics.
#: It MUST change if an incompatible canonicalization algorithm ever changes.
NORMALIZATION_VERSION = "text-v1"

#: Object key prefix family for content-addressed artifact storage.
CONTENT_ADDRESSED_KEY_ALGORITHM = "sha-256"


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    """Return ``value`` normalized to timezone-aware UTC."""
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


def _validate_bounded_text(
    value: str,
    *,
    field_name: str,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    """Return a trimmed, bounded, control-free text value."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    stripped = value.strip()
    if not allow_empty and not stripped:
        raise ValueError(f"{field_name} must not be blank")
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    if _CONTROL_CHARS.search(stripped):
        raise ValueError(f"{field_name} must not contain control characters")
    return stripped


def _validate_uri(value: str) -> str:
    """Return a bounded source URI (hostile data; only bounds/controls)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source_uri must not be blank")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("source_uri must not contain control characters")
    if len(value.strip()) > MAX_URI_LENGTH:
        raise ValueError(f"source_uri must not exceed {MAX_URI_LENGTH} characters")
    return value.strip()


def _validate_metadata(
    value: Mapping[str, str] | None,
    *,
    field_name: str,
) -> Mapping[str, str] | None:
    """Validate bounded, secret-free structural metadata."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    if len(value) > MAX_METADATA_KEYS:
        raise ValueError(f"{field_name} must not exceed {MAX_METADATA_KEYS} keys")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{field_name} keys and values must be strings")
    checked: dict[str, str] = {}
    for key, item in value.items():
        km = _validate_bounded_text(
            key, field_name=f"{field_name} key", max_length=MAX_METADATA_KEY_LENGTH
        ).lower()
        if any(marker in km for marker in METADATA_SECRET_MARKERS):
            raise ValueError(f"{field_name} must not contain secret-like keys")
        vm = _validate_bounded_text(
            item, field_name=f"{field_name} value", max_length=MAX_METADATA_VALUE_LENGTH
        ).lower()
        if any(marker in vm for marker in METADATA_SECRET_MARKERS):
            raise ValueError(f"{field_name} must not contain credential-like values")
        checked[key] = item
    return MappingProxyType(checked)


def _validate_hash(value: ContentHash | None) -> ContentHash | None:
    """Return a validated representation hash (ContentHash validates itself)."""
    if value is None:
        return None
    if value.algorithm != CONTENT_ADDRESSED_KEY_ALGORITHM:
        raise ValueError(
            "content hash algorithm must be the Darkula representation "
            f"algorithm '{CONTENT_ADDRESSED_KEY_ALGORITHM}'"
        )
    return value


@dataclass(frozen=True, slots=True)
class ContentObservation:
    """Bounded, Darkula-owned normalization input DTO (PR 8 step 5-7).

    Contains only what normalization needs; never provider/runtime types,
    Playwright objects, Podman types, or SDK documents. Completeness is
    explicit at the input and is never inferred from size or media type.
    ``body_bytes`` is bounded and represents the complete/sample bytes of the
    stated ``artifact_kind`` when present.
    """

    crawl_request_id: str
    source_uri: str
    observed_at: datetime
    observation_index: int
    title: str | None = None
    text: str | None = None
    media_type: ContentType | None = None
    artifact_kind: ArtifactKind | None = None
    completeness: ArtifactCompleteness = ArtifactCompleteness.SAMPLE
    body_bytes: bytes | None = None
    structural_metadata: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "crawl_request_id",
            _validate_bounded_text(
                self.crawl_request_id,
                field_name="crawl_request_id",
                max_length=200,
            ),
        )
        object.__setattr__(self, "source_uri", _validate_uri(self.source_uri))
        object.__setattr__(
            self,
            "observed_at",
            _require_utc(self.observed_at, field_name="observed_at"),
        )
        if not isinstance(self.observation_index, int) or isinstance(
            self.observation_index, bool
        ):
            raise ValueError("observation_index must be an integer")
        if self.observation_index < 1 or self.observation_index > MAX_OBSERVATION_INDEX:
            raise ValueError("observation_index is out of range")
        if self.title is not None:
            object.__setattr__(
                self,
                "title",
                _validate_bounded_text(
                    self.title, field_name="title", max_length=MAX_TITLE_LENGTH
                ),
            )
        if self.body_bytes is not None:
            if not isinstance(self.body_bytes, bytes):
                raise ValueError("body_bytes must be bytes")
            if len(self.body_bytes) > MAX_ARTIFACT_BYTES:
                raise ValueError(
                    f"body_bytes must not exceed {MAX_ARTIFACT_BYTES} bytes"
                )
            if self.artifact_kind is None:
                raise ValueError("body_bytes requires an explicit artifact_kind")
        object.__setattr__(
            self,
            "structural_metadata",
            _validate_metadata(
                self.structural_metadata, field_name="structural_metadata"
            ),
        )


@dataclass(frozen=True, slots=True)
class ContentArtifact:
    """Provider-neutral record of one stored content artifact/reference.

    ``ObjectStore`` owns the bytes; this record owns the Darkula identity and
    provider-neutral metadata. The physical object may be shared by many
    artifact records and many normalized observations (deduplication never
    merges provenance). ``object_key`` is an address, not identity and not a
    content hash.
    """

    artifact_id: ContentArtifactId
    object_key: ObjectKey
    content_type: ContentType | None
    size_bytes: int
    content_hash: ContentHash | None
    artifact_kind: ArtifactKind
    completeness: ArtifactCompleteness
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        object.__setattr__(
            self,
            "content_hash",
            _validate_hash(self.content_hash),
        )
        object.__setattr__(
            self,
            "created_at",
            _require_utc(self.created_at, field_name="created_at"),
        )


@dataclass(frozen=True, slots=True)
class NormalizedContent:
    """Canonical, provenance-bearing representation of one observation.

    ``text`` and any content fields are normalized but remain untrusted.
    ``content_hash`` identifies the exact normalized representation bytes
    (UTF-8 of the canonicalized representation), never the unknown original
    source body. ``artifact_id`` links to the backing
    :class:`ContentArtifact` where one exists.
    """

    content_id: NormalizedContentId
    source_uri: str
    observed_at: datetime
    crawl_request_id: str
    observation_index: int
    normalization_version: str
    completeness: ArtifactCompleteness
    title: str | None = None
    text: str | None = None
    content_type: ContentType | None = None
    content_hash: ContentHash | None = None
    artifact_id: ContentArtifactId | None = None
    structural_metadata: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_uri", _validate_uri(self.source_uri))
        object.__setattr__(
            self,
            "observed_at",
            _require_utc(self.observed_at, field_name="observed_at"),
        )
        object.__setattr__(
            self,
            "crawl_request_id",
            _validate_bounded_text(
                self.crawl_request_id,
                field_name="crawl_request_id",
                max_length=200,
            ),
        )
        if not isinstance(self.observation_index, int) or isinstance(
            self.observation_index, bool
        ):
            raise ValueError("observation_index must be an integer")
        if self.observation_index < 1 or self.observation_index > MAX_OBSERVATION_INDEX:
            raise ValueError("observation_index is out of range")
        object.__setattr__(
            self,
            "normalization_version",
            _validate_bounded_text(
                self.normalization_version,
                field_name="normalization_version",
                max_length=64,
            ),
        )
        if self.title is not None:
            object.__setattr__(
                self,
                "title",
                _validate_bounded_text(
                    self.title, field_name="title", max_length=MAX_TITLE_LENGTH
                ),
            )
        if (
            self.text is not None
            and len(self.text.encode("utf-8")) > MAX_NORMALIZED_TEXT_BYTES
        ):
            raise ValueError(
                f"normalized text must not exceed {MAX_NORMALIZED_TEXT_BYTES} bytes"
            )
        object.__setattr__(
            self,
            "content_hash",
            _validate_hash(self.content_hash),
        )
        object.__setattr__(
            self,
            "structural_metadata",
            _validate_metadata(
                self.structural_metadata, field_name="structural_metadata"
            ),
        )


__all__ = [
    "CONTENT_ADDRESSED_KEY_ALGORITHM",
    "MAX_ARTIFACT_BYTES",
    "MAX_METADATA_KEYS",
    "MAX_METADATA_KEY_LENGTH",
    "MAX_METADATA_VALUE_LENGTH",
    "MAX_NORMALIZED_TEXT_BYTES",
    "MAX_OBSERVATION_INDEX",
    "MAX_TITLE_LENGTH",
    "MAX_URI_LENGTH",
    "NORMALIZATION_VERSION",
    "ArtifactCompleteness",
    "ArtifactKind",
    "ContentArtifact",
    "ContentObservation",
    "NormalizedContent",
]
