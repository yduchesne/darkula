# SPDX-License-Identifier: AGPL-3.0-only
"""Executable source-domain concepts (PR 4).

Freezes the first executable forms of the six source-domain concepts
described in ``docs/DOMAIN_MODEL.md``:

- :class:`SourceCandidate` — a discovered resource, not yet a managed
  :class:`Source`;
- :class:`SourceCandidateEventHistory` — append-only candidate lifecycle
  history;
- :class:`Source` — a logical managed source, independent of any one URL;
- :class:`SourceEndpoint` — an endpoint belonging to a Source;
- :class:`ReconAssessment` — immutable reconnaissance output;
- :class:`SourceAssessment` — immutable, time-windowed source assessment
  output.

Domain semantics frozen here:

- candidate identity is distinct from its entrypoint text;
- Source identity is distinct from every endpoint URI;
- candidate lifecycle history is append-only;
- ReconAssessment / SourceAssessment are immutable historical observations;
- agent outputs never silently mutate lifecycle state (status transitions
  are explicit repository operations with expected-state checks);
- times are timezone-aware UTC (naive datetimes are rejected);
- intentionally open-ended structured fields are JSON-compatible documents,
  not relationalized ontology;
- metadata never embeds auth credentials/secrets.

Deliberately NOT frozen here: URI normalization/network validation, source
equivalence, cross-source deduplication, endpoint health-check semantics,
and candidate promotion policy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from darkula.domain.identifiers import (
    CandidateEventId,
    ReconAssessmentId,
    SourceAssessmentId,
    SourceCandidateId,
    SourceEndpointId,
    SourceId,
)

#: Canonical text bound for free-form domain text fields (provenance,
#: discovery methods, reasons, URIs, evidence references).
MAX_FREE_TEXT_LENGTH = 2048

#: Upper bound for structured JSON document size (serialized).
MAX_JSON_DOCUMENT_BYTES = 65536

#: Secret-like key/value markers rejected inside structured JSON documents.
#: Keep in parity with ``darkula.config.loader.SECRET_MARKERS`` so domain
#: validation and diagnostics agree on what counts as secret-bearing.
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


class CandidateStatus(StrEnum):
    """Current lifecycle state of a :class:`SourceCandidate`.

    ``PROMOTED`` is the transition into a managed
    :class:`~darkula.domain.source.Source`; the promotion workflow itself is
    not PR 4 scope.
    """

    DISCOVERED = "DISCOVERED"
    RECONNAISSANCE_PENDING = "RECONNAISSANCE_PENDING"
    UNDER_RECONNAISSANCE = "UNDER_RECONNAISSANCE"
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    PROMOTED = "PROMOTED"


class CandidateEventType(StrEnum):
    """Explicit event semantics for candidate lifecycle history.

    Event types describe *what happened*, deliberately separate from the
    candidate's *current status*: an event records one historical occurrence
    and never stands in for status state.
    """

    DISCOVERED = "DISCOVERED"
    RECONNAISSANCE_STARTED = "RECONNAISSANCE_STARTED"
    RECONNAISSANCE_COMPLETED = "RECONNAISSANCE_COMPLETED"
    NEEDS_MORE_RECON = "NEEDS_MORE_RECON"
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    PROMOTED = "PROMOTED"


class SourceStatus(StrEnum):
    """Smallest useful managed-source lifecycle frozen by PR 4.

    A managed :class:`Source` is either being monitored (``ACTIVE``) or not
    (``INACTIVE``). No scheduling/collection state is invented here.
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class EndpointType(StrEnum):
    """Locator category of a :class:`SourceEndpoint`.

    Fixed by :file:`docs/DOMAIN_MODEL.md` archetypes; not a health or
    reachability statement.
    """

    ONION = "ONION"
    CLEARNET = "CLEARNET"
    MIRROR = "MIRROR"
    API = "API"
    FEED = "FEED"


class EndpointStatus(StrEnum):
    """Minimal managed endpoint lifecycle.

    ``ACTIVE``/``INACTIVE`` record managed membership state only; no
    reachability/health semantics are invented here.
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class ReconDisposition(StrEnum):
    """Documented recon disposition output of :class:`ReconAssessment`.

    The Coordinator applies lifecycle decisions; the assessment only
    recommends.
    """

    QUALIFY = "QUALIFY"
    NEEDS_MORE_RECON = "NEEDS_MORE_RECON"
    REJECT = "REJECT"


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    """Return ``value`` normalized to timezone-aware UTC.

    Naive datetimes are rejected. Aware datetimes with a non-UTC offset are
    normalized to UTC so persisted values are canonical.
    """
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


def _validate_text(
    value: str,
    *,
    field_name: str,
    allow_empty: bool = False,
    max_length: int = MAX_FREE_TEXT_LENGTH,
) -> str:
    """Return a trimmed, bounded, control-free text value.

    Blank values are rejected unless ``allow_empty`` is true. Control
    characters (C0 plus DEL) are rejected wherever text may reach logs,
    telemetry, or JSON documents.
    """
    stripped = value.strip()
    if not allow_empty and not stripped:
        raise ValueError(f"{field_name} must not be blank")
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    if _CONTROL_CHARS.search(stripped):
        raise ValueError(f"{field_name} must not contain control characters")
    return stripped


def _validate_json_document(
    value: Mapping[str, Any] | None,
    *,
    field_name: str,
) -> Mapping[str, Any] | None:
    """Validate one structured JSON-compatible document.

    Requirements:

    - must be ``None`` or a mapping;
    - keys are non-empty, bounded, control-free strings;
    - the whole document round-trips through :func:`json.dumps`; and
    - no key or string value embeds auth credentials/secrets.
    """
    if value is None:
        return None
    for key in value:
        if not isinstance(key, str):
            raise ValueError(f"{field_name} keys must be strings")
        _validate_text(key, field_name=f"{field_name} key")
        lowered = key.lower()
        if any(marker in lowered for marker in METADATA_SECRET_MARKERS):
            raise ValueError(f"{field_name} must not contain secret-like keys")
    for item in value.values():
        if isinstance(item, str):
            lowered = item.lower()
            if any(marker in lowered for marker in METADATA_SECRET_MARKERS):
                raise ValueError(
                    f"{field_name} must not contain credential-like values"
                )
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-compatible") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_DOCUMENT_BYTES:
        raise ValueError(
            f"{field_name} must not exceed {MAX_JSON_DOCUMENT_BYTES} bytes"
        )
    return value


def _validate_evidence_references(
    references: tuple[str, ...],
    *,
    field_name: str,
) -> tuple[str, ...]:
    """Return validated bounded evidence/provenance references."""
    return tuple(
        _validate_text(reference, field_name=f"{field_name} reference")
        for reference in references
    )


@dataclass(frozen=True, slots=True)
class Confidence:
    """One bounded confidence representation shared by assessment records.

    A ``float`` on the inclusive ``[0, 1]`` interval. Defined once so all
    assessment records use a single documented confidence semantic; it is
    JSON-compatible (serializes as its numeric value).
    """

    value: float

    def __post_init__(self) -> None:
        if isinstance(self.value, bool):
            raise ValueError("confidence must be a number, not a boolean")
        if not (0.0 <= self.value <= 1.0):
            raise ValueError("confidence must be within [0, 1]")

    def __float__(self) -> float:
        return self.value


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """A discovered resource that is not yet a managed :class:`Source`.

    Candidate identity is the :class:`SourceCandidateId`; the entrypoint is
    discovery locator/provenance and never *is* the candidate identity.
    """

    candidate_id: SourceCandidateId
    discovered_at: datetime
    discovery_method: str
    entrypoint: str
    status: CandidateStatus
    discovery_context: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "discovered_at",
            _require_utc(self.discovered_at, field_name="discovered_at"),
        )
        object.__setattr__(
            self,
            "discovery_method",
            _validate_text(self.discovery_method, field_name="discovery_method"),
        )
        object.__setattr__(
            self,
            "entrypoint",
            _validate_text(self.entrypoint, field_name="entrypoint"),
        )
        object.__setattr__(
            self,
            "discovery_context",
            _validate_json_document(
                self.discovery_context, field_name="discovery_context"
            ),
        )


@dataclass(frozen=True, slots=True)
class SourceCandidateEventHistory:
    """One append-only candidate lifecycle history record.

    Immutable: repositories provide no production update/delete for history
    records.
    """

    event_id: CandidateEventId
    candidate_id: SourceCandidateId
    event_type: CandidateEventType
    occurred_at: datetime
    context: Mapping[str, Any] | None = None
    reason: str | None = None
    provenance: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "occurred_at",
            _require_utc(self.occurred_at, field_name="occurred_at"),
        )
        object.__setattr__(
            self,
            "context",
            _validate_json_document(self.context, field_name="context"),
        )
        if self.reason is not None:
            object.__setattr__(
                self,
                "reason",
                _validate_text(self.reason, field_name="reason", allow_empty=True),
            )
        if self.provenance is not None:
            object.__setattr__(
                self,
                "provenance",
                _validate_text(
                    self.provenance, field_name="provenance", allow_empty=True
                ),
            )


@dataclass(frozen=True, slots=True)
class Source:
    """A logical managed source, independent of any one URL.

    Source identity survives endpoint/mirror rotation; it is never derived
    from an endpoint URI. ``created_at``/``updated_at`` track managed-state
    history; a fresh Source has ``created_at == updated_at``.
    """

    source_id: SourceId
    status: SourceStatus
    created_at: datetime
    updated_at: datetime
    name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "created_at",
            _require_utc(self.created_at, field_name="created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _require_utc(self.updated_at, field_name="updated_at"),
        )
        if self.name is not None:
            object.__setattr__(
                self,
                "name",
                _validate_text(self.name, field_name="name", allow_empty=True),
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class SourceEndpoint:
    """A first-class endpoint belonging to a managed :class:`Source`.

    The URI is a locator within the Source, never the Source's identity.
    """

    endpoint_id: SourceEndpointId
    source_id: SourceId
    uri: str
    endpoint_type: EndpointType
    status: EndpointStatus
    first_observed_at: datetime
    last_observed_at: datetime
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "first_observed_at",
            _require_utc(self.first_observed_at, field_name="first_observed_at"),
        )
        object.__setattr__(
            self,
            "last_observed_at",
            _require_utc(self.last_observed_at, field_name="last_observed_at"),
        )
        object.__setattr__(self, "uri", _validate_text(self.uri, field_name="uri"))
        object.__setattr__(
            self,
            "metadata",
            _validate_json_document(self.metadata, field_name="metadata"),
        )
        if self.last_observed_at < self.first_observed_at:
            raise ValueError("last_observed_at must not precede first_observed_at")


@dataclass(frozen=True, slots=True)
class ReconAssessment:
    """Immutable reconnaissance output for one candidate.

    ``characteristics`` carries the documented recon fields (source type,
    accessibility, content/navigation characteristics, discovered endpoints,
    authentication characteristics, proposed collection strategy, relevance)
    as deliberate structured sections rather than a frozen relational
    ontology. ``confidence`` and ``disposition`` are first-class structured
    outputs; the Coordinator applies lifecycle decisions.
    """

    assessment_id: ReconAssessmentId
    candidate_id: SourceCandidateId
    assessed_at: datetime
    disposition: ReconDisposition
    confidence: Confidence
    evidence_references: tuple[str, ...] = ()
    characteristics: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assessed_at",
            _require_utc(self.assessed_at, field_name="assessed_at"),
        )
        object.__setattr__(
            self,
            "characteristics",
            _validate_json_document(self.characteristics, field_name="characteristics"),
        )
        object.__setattr__(
            self,
            "evidence_references",
            _validate_evidence_references(
                self.evidence_references, field_name="evidence_references"
            ),
        )


@dataclass(frozen=True, slots=True)
class SourceAssessment:
    """Immutable, time-windowed source intelligence output.

    The assessment is a historical observation over ``[window_start,
    window_end]``; it never overwrites a mutable Source field as a
    substitute for history.
    """

    assessment_id: SourceAssessmentId
    source_id: SourceId
    assessed_at: datetime
    window_start: datetime
    window_end: datetime
    confidence: Confidence
    relevance: Confidence | None = None
    activity: Confidence | None = None
    novelty: Confidence | None = None
    evidence_references: tuple[str, ...] = ()
    characteristics: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assessed_at",
            _require_utc(self.assessed_at, field_name="assessed_at"),
        )
        object.__setattr__(
            self,
            "window_start",
            _require_utc(self.window_start, field_name="window_start"),
        )
        object.__setattr__(
            self,
            "window_end",
            _require_utc(self.window_end, field_name="window_end"),
        )
        object.__setattr__(
            self,
            "characteristics",
            _validate_json_document(self.characteristics, field_name="characteristics"),
        )
        object.__setattr__(
            self,
            "evidence_references",
            _validate_evidence_references(
                self.evidence_references, field_name="evidence_references"
            ),
        )
        if self.window_end < self.window_start:
            raise ValueError("window_end must not precede window_start")


__all__ = [
    "MAX_FREE_TEXT_LENGTH",
    "MAX_JSON_DOCUMENT_BYTES",
    "METADATA_SECRET_MARKERS",
    "CandidateEventType",
    "CandidateStatus",
    "Confidence",
    "EndpointStatus",
    "EndpointType",
    "ReconAssessment",
    "ReconDisposition",
    "Source",
    "SourceAssessment",
    "SourceCandidate",
    "SourceCandidateEventHistory",
    "SourceEndpoint",
    "SourceStatus",
]
