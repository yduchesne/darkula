# SPDX-License-Identifier: AGPL-3.0-only
"""Geographic-resolution domain concepts (PR 12).

Geographic resolution is a **separate, explicit interpretation** of an
extracted ``LOCATION`` occurrence. It is provider-neutral and never mutates
the occurrence it interprets:

- a ``LOCATION`` :class:`~darkula.domain.extraction.ExtractedEntity` is a
  content-derived mention (what the content says);
- a :class:`GeographicResolution` is a later interpretation of that mention
  (which canonical place it maps to), with its own confidence and resolver
  provenance.

Identity stays distinct: ``GeographicResolutionId`` identifies one resolution
observation, ``ExtractedEntityId`` identifies the resolved occurrence, resolver
name/version identify the resolver implementation, and canonical attributes
(name/geography) are resolved data, never identity. Resolutions are
immutable/versioned; a new resolver version coexists rather than rewriting
history.

Geographic geometry uses a bounded WGS84 latitude/longitude representation
rather than requiring a PostGIS extension that is not enabled by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from darkula.domain.identifiers import ExtractedEntityId, GeographicResolutionId

#: Bounds applied to every geographic/resolver value before persistence.
MAX_LOCATION_MENTION_LENGTH = 512
MAX_CANONICAL_NAME_LENGTH = 512
MAX_GEO_COMPONENT_LENGTH = 256
MAX_RESOLVER_REFERENCE_LENGTH = 512
MAX_RESOLVER_NAME_LENGTH = 64
MAX_RESOLVER_VERSION_LENGTH = 32
#: Default bounded context window (code points) around a mention.
DEFAULT_CONTEXT_CHARS = 200
MAX_CONTEXT_CHARS = 2000

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")


class GeographicResolutionStatus(StrEnum):
    """The finite geographic-resolution outcome vocabulary."""

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


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


def _validate_optional_text(
    value: str | None, *, field_name: str, max_length: int
) -> str | None:
    """Return ``None`` or a bounded, control-free, non-blank value."""
    if value is None:
        return None
    return _validate_bounded_text(value, field_name=field_name, max_length=max_length)


def _validate_confidence(value: float | None) -> float | None:
    """Return ``None`` or a finite confidence in inclusive ``[0, 1]``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("confidence must be finite")
    if number < 0.0 or number > 1.0:
        raise ValueError("confidence must be within [0.0, 1.0]")
    return number


def _validate_country_code(value: str | None) -> str | None:
    """Return ``None`` or an upper-case ISO-3166-1 alpha-2 country code."""
    if value is None:
        return None
    stripped = value.strip().upper()
    if _COUNTRY_CODE.fullmatch(stripped) is None:
        raise ValueError("country_code must be a two-letter ISO 3166-1 alpha-2 code")
    return stripped


def _validate_coordinates(
    latitude: float | None, longitude: float | None
) -> tuple[float | None, float | None]:
    """Validate the WGS84 latitude/longitude pair (both or neither)."""
    if (latitude is None) != (longitude is None):
        raise ValueError("latitude and longitude must both be present or both absent")
    if latitude is None or longitude is None:
        return None, None
    if isinstance(latitude, bool) or not isinstance(latitude, (int, float)):
        raise ValueError("latitude must be a number")
    if isinstance(longitude, bool) or not isinstance(longitude, (int, float)):
        raise ValueError("longitude must be a number")
    lat = float(latitude)
    lon = float(longitude)
    if lat != lat or lat in (float("inf"), float("-inf")):
        raise ValueError("latitude must be finite")
    if lon != lon or lon in (float("inf"), float("-inf")):
        raise ValueError("longitude must be finite")
    if lat < -90.0 or lat > 90.0:
        raise ValueError("latitude must be within [-90.0, 90.0]")
    if lon < -180.0 or lon > 180.0:
        raise ValueError("longitude must be within [-180.0, 180.0]")
    return lat, lon


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    """Return ``value`` normalized to timezone-aware UTC."""
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class GeographicResolverIdentity:
    """Bounded logical identity/version of a geographic resolver."""

    name: str
    version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            _validate_bounded_text(
                self.name,
                field_name="resolver name",
                max_length=MAX_RESOLVER_NAME_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "version",
            _validate_bounded_text(
                self.version,
                field_name="resolver version",
                max_length=MAX_RESOLVER_VERSION_LENGTH,
            ),
        )

    def __str__(self) -> str:
        return f"{self.name}/{self.version}"


@dataclass(frozen=True, slots=True)
class GeographicResolutionRequest:
    """Bounded, provider-neutral input to a :class:`GeographicResolver`."""

    mention: str
    left_context: str = ""
    right_context: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mention",
            _validate_bounded_text(
                self.mention,
                field_name="mention",
                max_length=MAX_LOCATION_MENTION_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "left_context",
            _validate_bounded_text(
                self.left_context,
                field_name="left_context",
                max_length=MAX_CONTEXT_CHARS,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "right_context",
            _validate_bounded_text(
                self.right_context,
                field_name="right_context",
                max_length=MAX_CONTEXT_CHARS,
                allow_empty=True,
            ),
        )


def _validate_place_fields(
    status: GeographicResolutionStatus,
    *,
    canonical_name: str | None,
    country_code: str | None,
    administrative_area: str | None,
    locality: str | None,
    latitude: float | None,
    longitude: float | None,
    confidence: float | None,
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    float | None,
    float | None,
    float | None,
]:
    """Validate and normalize the canonical/resolved attribute set."""
    name = _validate_optional_text(
        canonical_name,
        field_name="canonical_name",
        max_length=MAX_CANONICAL_NAME_LENGTH,
    )
    iso = _validate_country_code(country_code)
    admin = _validate_optional_text(
        administrative_area,
        field_name="administrative_area",
        max_length=MAX_GEO_COMPONENT_LENGTH,
    )
    place = _validate_optional_text(
        locality, field_name="locality", max_length=MAX_GEO_COMPONENT_LENGTH
    )
    lat, lon = _validate_coordinates(latitude, longitude)
    conf = _validate_confidence(confidence)

    if status is GeographicResolutionStatus.RESOLVED:
        if name is None:
            raise ValueError("a RESOLVED outcome requires a canonical_name")
        if conf is None:
            raise ValueError("a RESOLVED outcome requires a resolution confidence")
    else:
        # AMBIGUOUS must not pretend one candidate is canonical; UNRESOLVED
        # must not carry canonical fields or geometry.
        if any(item is not None for item in (name, iso, admin, place, lat, lon)):
            raise ValueError(
                "a non-RESOLVED outcome must not carry canonical fields or geometry"
            )
    return name, iso, admin, place, lat, lon, conf


@dataclass(frozen=True, slots=True)
class GeographicResolverResult:
    """Provider-neutral result of one resolver call.

    Carries no provider/HTTP/SDK object; only bounded canonical attributes.
    """

    status: GeographicResolutionStatus
    canonical_name: str | None = None
    country_code: str | None = None
    administrative_area: str | None = None
    locality: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    confidence: float | None = None
    reference: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, GeographicResolutionStatus):
            raise ValueError("status must be a known GeographicResolutionStatus")
        name, iso, admin, place, lat, lon, conf = _validate_place_fields(
            self.status,
            canonical_name=self.canonical_name,
            country_code=self.country_code,
            administrative_area=self.administrative_area,
            locality=self.locality,
            latitude=self.latitude,
            longitude=self.longitude,
            confidence=self.confidence,
        )
        object.__setattr__(self, "canonical_name", name)
        object.__setattr__(self, "country_code", iso)
        object.__setattr__(self, "administrative_area", admin)
        object.__setattr__(self, "locality", place)
        object.__setattr__(self, "latitude", lat)
        object.__setattr__(self, "longitude", lon)
        object.__setattr__(self, "confidence", conf)
        object.__setattr__(
            self,
            "reference",
            _validate_optional_text(
                self.reference,
                field_name="resolver reference",
                max_length=MAX_RESOLVER_REFERENCE_LENGTH,
            ),
        )


@dataclass(frozen=True, slots=True)
class GeographicResolution:
    """One immutable/versioned geographic-resolution observation."""

    resolution_id: GeographicResolutionId
    extracted_entity_id: ExtractedEntityId
    status: GeographicResolutionStatus
    resolver: GeographicResolverIdentity
    resolved_at: datetime
    canonical_name: str | None = None
    country_code: str | None = None
    administrative_area: str | None = None
    locality: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    confidence: float | None = None
    resolver_reference: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, GeographicResolutionStatus):
            raise ValueError("status must be a known GeographicResolutionStatus")
        object.__setattr__(
            self,
            "resolved_at",
            _require_utc(self.resolved_at, field_name="resolved_at"),
        )
        name, iso, admin, place, lat, lon, conf = _validate_place_fields(
            self.status,
            canonical_name=self.canonical_name,
            country_code=self.country_code,
            administrative_area=self.administrative_area,
            locality=self.locality,
            latitude=self.latitude,
            longitude=self.longitude,
            confidence=self.confidence,
        )
        object.__setattr__(self, "canonical_name", name)
        object.__setattr__(self, "country_code", iso)
        object.__setattr__(self, "administrative_area", admin)
        object.__setattr__(self, "locality", place)
        object.__setattr__(self, "latitude", lat)
        object.__setattr__(self, "longitude", lon)
        object.__setattr__(self, "confidence", conf)
        object.__setattr__(
            self,
            "resolver_reference",
            _validate_optional_text(
                self.resolver_reference,
                field_name="resolver reference",
                max_length=MAX_RESOLVER_REFERENCE_LENGTH,
            ),
        )


__all__ = [
    "DEFAULT_CONTEXT_CHARS",
    "MAX_CANONICAL_NAME_LENGTH",
    "MAX_CONTEXT_CHARS",
    "MAX_GEO_COMPONENT_LENGTH",
    "MAX_LOCATION_MENTION_LENGTH",
    "MAX_RESOLVER_NAME_LENGTH",
    "MAX_RESOLVER_REFERENCE_LENGTH",
    "MAX_RESOLVER_VERSION_LENGTH",
    "GeographicResolution",
    "GeographicResolutionRequest",
    "GeographicResolutionStatus",
    "GeographicResolverIdentity",
    "GeographicResolverResult",
]
