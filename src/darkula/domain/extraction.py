# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic extraction domain concepts (PR 11).

PR 11 converts one persisted normalized representation into bounded,
provenance-bearing structured observations. It is deliberately **not**
analysis: it performs no semantic/LLM reasoning, relationship inference,
geographic resolution, reputation judgment, or global-truth promotion.

Identity rules frozen here:

- ``ExtractionResultId`` is the persisted result identity; the semantic /
  idempotency key is ``(content_id, profile_name, profile_version)``;
- ``ExtractorIdentity`` (bounded developer-controlled ``name`` + ``version``)
  is logical extractor identity, not a value;
- ``ExtractedEntityId`` identifies one extracted **occurrence**;
- ``SourceSpan`` is exact provenance: Python code-point offsets into the
  canonical text where ``text[start:end] == raw_value``;
- ``type + normalized value`` is a semantic value, never occurrence identity.

An :class:`ExtractedEntity` asserts only that a named/versioned deterministic
extractor recognized a value at an exact span in that content. It does not
assert maliciousness, ownership, victimhood, identity equivalence, or any
relationship.

Persisted results/entities are immutable/append-only: an incompatible
extractor change requires a new profile/extractor version and a new result,
never rewriting historical rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from darkula.domain.identifiers import (
    ExtractedEntityId,
    ExtractionResultId,
    NormalizedContentId,
)

#: Fixed PR 11 deterministic profile identity.
DETERMINISTIC_OBSERVABLES_PROFILE_NAME = "deterministic-observables"
DETERMINISTIC_OBSERVABLES_PROFILE_VERSION = "v1"

#: Bounds applied to every extracted/metadata value before persistence.
MAX_RAW_VALUE_LENGTH = 2048
MAX_NORMALIZED_VALUE_LENGTH = 2048
MAX_EXTRACTOR_NAME_LENGTH = 64
MAX_EXTRACTOR_VERSION_LENGTH = 32
MAX_PROFILE_NAME_LENGTH = 64
MAX_PROFILE_VERSION_LENGTH = 32
MAX_SUBTYPE_LENGTH = 32
MAX_MANIFEST_ENTRIES = 64
#: Hard upper bound accepted by the domain entity-count validation.
MAX_ENTITY_COUNT = 1_000_000

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class EntityType(StrEnum):
    """Finite PR 11 deterministic observable/entity vocabulary.

    PR 12 owns semantic/geographic types (PERSON, ORGANIZATION, LOCATION,
    MALWARE, ...). They are deliberately absent here so this enum stays a
    truthful, bounded PR 11 contract.
    """

    IP_ADDRESS = "IP_ADDRESS"
    DOMAIN = "DOMAIN"
    URL = "URL"
    EMAIL = "EMAIL"
    HASH = "HASH"


class HashSubtype(StrEnum):
    """Bounded hash-algorithm subtype carried by ``HASH`` occurrences."""

    MD5 = "MD5"
    SHA1 = "SHA1"
    SHA256 = "SHA256"


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
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{field_name} must not contain control characters")
    stripped = value.strip()
    if not allow_empty and not stripped:
        raise ValueError(f"{field_name} must not be blank")
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    return stripped


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    """Return ``value`` normalized to timezone-aware UTC."""
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """Half-open ``[start, end)`` code-point span in canonical text.

    ``0 <= start < end`` and the raw value equals ``text[start:end]`` for the
    exact canonical UTF-8 (Python string) representation. Spans are
    provenance, never identity.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or isinstance(self.start, bool):
            raise ValueError("span start must be an integer")
        if not isinstance(self.end, int) or isinstance(self.end, bool):
            raise ValueError("span end must be an integer")
        if self.start < 0:
            raise ValueError("span start must not be negative")
        if self.end <= self.start:
            raise ValueError("span end must be greater than span start")

    @property
    def length(self) -> int:
        """Return the span length in code points."""
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class ExtractorIdentity:
    """Bounded developer-controlled extractor identity (``name`` + ``version``)."""

    name: str
    version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            _validate_bounded_text(
                self.name,
                field_name="extractor name",
                max_length=MAX_EXTRACTOR_NAME_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "version",
            _validate_bounded_text(
                self.version,
                field_name="extractor version",
                max_length=MAX_EXTRACTOR_VERSION_LENGTH,
            ),
        )

    def __str__(self) -> str:
        return f"{self.name}/{self.version}"


def _validate_manifest(
    manifest: tuple[ExtractorIdentity, ...],
) -> tuple[ExtractorIdentity, ...]:
    """Return a validated immutable extractor manifest."""
    if not isinstance(manifest, tuple):
        raise ValueError("extractor manifest must be a tuple")
    if not manifest:
        raise ValueError("extractor manifest must not be empty")
    if len(manifest) > MAX_MANIFEST_ENTRIES:
        raise ValueError(
            f"extractor manifest must not exceed {MAX_MANIFEST_ENTRIES} entries"
        )
    seen: set[tuple[str, str]] = set()
    for entry in manifest:
        if not isinstance(entry, ExtractorIdentity):
            raise ValueError("extractor manifest entries must be ExtractorIdentity")
        key = (entry.name, entry.version)
        if key in seen:
            raise ValueError("extractor manifest must not contain duplicate entries")
        seen.add(key)
    return manifest


@dataclass(frozen=True, slots=True)
class ExtractedEntity:
    """One persisted extracted-entity occurrence with exact provenance."""

    entity_id: ExtractedEntityId
    extraction_result_id: ExtractionResultId
    content_id: NormalizedContentId
    entity_type: EntityType
    raw_value: str
    normalized_value: str
    source_span: SourceSpan
    extractor: ExtractorIdentity
    subtype: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.entity_type, EntityType):
            raise ValueError("entity_type must be a known EntityType")
        object.__setattr__(
            self,
            "raw_value",
            _validate_bounded_text(
                self.raw_value,
                field_name="raw_value",
                max_length=MAX_RAW_VALUE_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "normalized_value",
            _validate_bounded_text(
                self.normalized_value,
                field_name="normalized_value",
                max_length=MAX_NORMALIZED_VALUE_LENGTH,
            ),
        )
        if self.subtype is not None:
            object.__setattr__(
                self,
                "subtype",
                _validate_bounded_text(
                    self.subtype,
                    field_name="subtype",
                    max_length=MAX_SUBTYPE_LENGTH,
                ),
            )


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """One immutable/versioned deterministic extraction result.

    ``extractor_manifest`` is the exact historical manifest that produced the
    result (never reconstructed from current code). ``entity_count`` must equal
    the number of persisted occurrences for the result.
    """

    result_id: ExtractionResultId
    content_id: NormalizedContentId
    profile_name: str
    profile_version: str
    extractor_manifest: tuple[ExtractorIdentity, ...]
    extracted_at: datetime
    entity_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_name",
            _validate_bounded_text(
                self.profile_name,
                field_name="profile_name",
                max_length=MAX_PROFILE_NAME_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "profile_version",
            _validate_bounded_text(
                self.profile_version,
                field_name="profile_version",
                max_length=MAX_PROFILE_VERSION_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "extractor_manifest",
            _validate_manifest(self.extractor_manifest),
        )
        object.__setattr__(
            self,
            "extracted_at",
            _require_utc(self.extracted_at, field_name="extracted_at"),
        )
        if not isinstance(self.entity_count, int) or isinstance(
            self.entity_count, bool
        ):
            raise ValueError("entity_count must be an integer")
        if self.entity_count < 0 or self.entity_count > MAX_ENTITY_COUNT:
            raise ValueError("entity_count is out of range")


__all__ = [
    "DETERMINISTIC_OBSERVABLES_PROFILE_NAME",
    "DETERMINISTIC_OBSERVABLES_PROFILE_VERSION",
    "MAX_ENTITY_COUNT",
    "MAX_EXTRACTOR_NAME_LENGTH",
    "MAX_EXTRACTOR_VERSION_LENGTH",
    "MAX_MANIFEST_ENTRIES",
    "MAX_NORMALIZED_VALUE_LENGTH",
    "MAX_PROFILE_NAME_LENGTH",
    "MAX_PROFILE_VERSION_LENGTH",
    "MAX_RAW_VALUE_LENGTH",
    "MAX_SUBTYPE_LENGTH",
    "EntityType",
    "ExtractedEntity",
    "ExtractionResult",
    "ExtractorIdentity",
    "HashSubtype",
    "SourceSpan",
]
