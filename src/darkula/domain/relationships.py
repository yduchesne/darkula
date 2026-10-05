# SPDX-License-Identifier: AGPL-3.0-only
"""Content-derived relationship-assertion domain concepts (PR 13).

PR 13 records what **one normalized content observation asserted** about two
extracted entity occurrences. It is deliberately **not** analysis:

    assertion != global truth

A persisted :class:`ExtractedRelationship` means "this content asserted this
predicate between these two extracted occurrences"; it is not a canonical graph
edge, an ATI ``Relationship``, a resolved real-world fact, a corroborated
claim, or a source-independent truth. Two documents asserting the same
semantics retain two separate assertions.

Identity rules frozen here:

- ``RelationshipExtractionResultId`` identifies one persisted relationship
  result; the semantic/idempotency key is ``(content_id, profile_name,
  profile_version)``;
- ``ExtractedRelationshipId`` identifies one assertion **occurrence**;
- endpoints are :class:`~darkula.domain.identifiers.ExtractedEntityId`
  occurrences (never endpoint values, ``(EntityType, normalized_value)``,
  global IDs, or geographic-resolution IDs);
- :class:`~darkula.domain.extraction.SourceSpan` is exact support provenance:
  ``text[start:end] == support_text`` in the canonical normalized text;
- ``predicate`` is a finite :class:`RelationshipPredicate` value, never an
  arbitrary free-text string.

Results/assertions are immutable/append-only: a new profile/version creates a
new result and never rewrites history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from darkula.domain.extraction import ExtractorIdentity, SourceSpan
from darkula.domain.identifiers import (
    ExtractedEntityId,
    ExtractedRelationshipId,
    NormalizedContentId,
    RelationshipExtractionResultId,
)

#: Fixed PR 13 relationship-assertion profile identity.
RELATIONSHIP_ASSERTIONS_PROFILE_NAME = "relationship-assertions"
RELATIONSHIP_ASSERTIONS_PROFILE_VERSION = "v1"

#: Logical relationship extractor identity (the Darkula contract/version,
#: never a transient provider response id).
RELATIONSHIP_LLM_EXTRACTOR_NAME = "relationship-llm"
RELATIONSHIP_LLM_EXTRACTOR_VERSION = "v1"

#: Bounds applied to relationship metadata before persistence.
MAX_PROFILE_NAME_LENGTH = 64
MAX_PROFILE_VERSION_LENGTH = 32
MAX_MANIFEST_ENTRIES = 64
#: Hard cap on persisted assertions for one result; a cap overflow fails typed
#: and never silently truncates.
MAX_RELATIONSHIP_COUNT = 1_000_000
#: Hard bound on one stored support text (code points).
MAX_RELATIONSHIP_SUPPORT_LENGTH = 8192

#: C0 controls plus DEL, except ``\n``/``\t`` which canonical normalized text
#: preserves for line structure.
_DISALLOWED_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class RelationshipPredicate(StrEnum):
    """Finite, versioned ``relationship-assertions/v1`` predicate vocabulary.

    The vocabulary is intentionally small and operational. It carries no
    ATT&CK/STIX/MISP/OpenCTI semantics; adding a predicate is a developer-
    controlled contract change that requires a new profile version.
    """

    AFFILIATED_WITH = "AFFILIATED_WITH"
    USES = "USES"
    OPERATES = "OPERATES"
    TARGETS = "TARGETS"
    IMPERSONATES = "IMPERSONATES"
    SELLS = "SELLS"
    OFFERS_ACCESS_TO = "OFFERS_ACCESS_TO"
    HAS_ACCESS_TO = "HAS_ACCESS_TO"
    LOCATED_IN = "LOCATED_IN"
    AFFECTS = "AFFECTS"


def _validate_support_text(value: str) -> str:
    """Return a bounded, control-free support text (never stripped).

    Support text must be copied exactly from canonical text, so leading or
    trailing whitespace is preserved; only blank and overlong values are
    rejected.
    """
    if not isinstance(value, str):
        raise ValueError("support_text must be a string")
    if not value.strip():
        raise ValueError("support_text must not be blank")
    if len(value) > MAX_RELATIONSHIP_SUPPORT_LENGTH:
        raise ValueError(
            f"support_text must not exceed {MAX_RELATIONSHIP_SUPPORT_LENGTH} characters"
        )
    if _DISALLOWED_CONTROLS.search(value):
        raise ValueError("support_text must not contain control characters")
    return value


def _validate_bounded_name(value: str, *, field_name: str, max_length: int) -> str:
    """Return a trimmed, bounded, control-free name value."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if _DISALLOWED_CONTROLS.search(value):
        raise ValueError(f"{field_name} must not contain control characters")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    return stripped


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    """Return ``value`` normalized to timezone-aware UTC."""
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


def _validate_confidence(value: float) -> float:
    """Return a finite extraction confidence in inclusive ``[0, 1]`` or raise."""
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("extraction_confidence must be a number")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError("extraction_confidence must be finite")
    if number < 0.0 or number > 1.0:
        raise ValueError("extraction_confidence must be within [0.0, 1.0]")
    return number


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
class RelationshipExtractionResult:
    """One immutable/versioned relationship-extraction result.

    ``extractor_manifest`` is the exact historical manifest that produced the
    result (never reconstructed from current code). ``relationship_count`` must
    equal the number of persisted assertions for the result.
    """

    result_id: RelationshipExtractionResultId
    content_id: NormalizedContentId
    profile_name: str
    profile_version: str
    extractor_manifest: tuple[ExtractorIdentity, ...]
    extracted_at: datetime
    relationship_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_name",
            _validate_bounded_name(
                self.profile_name,
                field_name="profile_name",
                max_length=MAX_PROFILE_NAME_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "profile_version",
            _validate_bounded_name(
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
        if not isinstance(self.relationship_count, int) or isinstance(
            self.relationship_count, bool
        ):
            raise ValueError("relationship_count must be an integer")
        if (
            self.relationship_count < 0
            or self.relationship_count > MAX_RELATIONSHIP_COUNT
        ):
            raise ValueError("relationship_count is out of range")


@dataclass(frozen=True, slots=True)
class ExtractedRelationship:
    """One persisted, provenance-bearing relationship assertion occurrence.

    ``extraction_confidence`` is the confidence that **the content asserted
    the relationship**. It is never objective truth, source credibility,
    endpoint extraction confidence, geographic-resolution confidence, or a
    threat score. ``support_text`` is the exact canonical text slice at
    ``support_span`` and is stored for bounded historical inspectability.
    """

    relationship_id: ExtractedRelationshipId
    extraction_result_id: RelationshipExtractionResultId
    content_id: NormalizedContentId
    source_entity_id: ExtractedEntityId
    predicate: RelationshipPredicate
    target_entity_id: ExtractedEntityId
    support_span: SourceSpan
    support_text: str
    extractor: ExtractorIdentity
    extraction_confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.predicate, RelationshipPredicate):
            raise ValueError("predicate must be a known RelationshipPredicate")
        if self.source_entity_id == self.target_entity_id:
            raise ValueError("a relationship must not be a self-edge")
        object.__setattr__(
            self, "support_text", _validate_support_text(self.support_text)
        )
        if self.support_span.length != len(self.support_text):
            raise ValueError("support_span length must equal the support_text length")
        object.__setattr__(
            self,
            "extraction_confidence",
            _validate_confidence(self.extraction_confidence),
        )


__all__ = [
    "MAX_MANIFEST_ENTRIES",
    "MAX_PROFILE_NAME_LENGTH",
    "MAX_PROFILE_VERSION_LENGTH",
    "MAX_RELATIONSHIP_COUNT",
    "MAX_RELATIONSHIP_SUPPORT_LENGTH",
    "RELATIONSHIP_ASSERTIONS_PROFILE_NAME",
    "RELATIONSHIP_ASSERTIONS_PROFILE_VERSION",
    "RELATIONSHIP_LLM_EXTRACTOR_NAME",
    "RELATIONSHIP_LLM_EXTRACTOR_VERSION",
    "ExtractedRelationship",
    "RelationshipExtractionResult",
    "RelationshipPredicate",
]
