# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral discovery and provenance domain (PR 17).

Discovery claims are **untrusted evidence**, never source truth and never
collection authorization. This module freezes the bounded domain contract a
future registry/content-reference provider adapter (PR 18/PR 22) produces
and Darkula durably records:

- :class:`DiscoveryInput` — one bounded, provider-neutral observation
  candidate (what a provider claims);
- :class:`DiscoveryObservation` — one immutable accepted observation with
  its locator validation, provider provenance, acceptance time and stable
  semantic replay key;
- :class:`AccessEvidence` — one immutable, non-authorizing access record for
  an *already existing* candidate or managed endpoint;
- :class:`QualificationDecision` — one immutable qualification decision that
  commits atomically with exactly one existing candidate lifecycle
  transition/event (only PR 19 decides/invokes it).

Domain invariants frozen here:

- a discovery observation never creates or mutates a ``SourceCandidate``,
  ``Source``, ``SourceEndpoint``, ``CollectionPolicy``, ``CollectionRun``,
  assessment, or collection authorization;
- a name-only reference has no synthetic locator and can never become a
  crawl target;
- URI equality never implies logical source equivalence: observations from
  different providers/records stay distinct evidence;
- the semantic replay key is the stable tuple
  ``(provider_id, provider_record_id, provider_record_version,
  detection_method, reference_kind)`` hashed deterministically; Python
  ``hash()`` and random UUIDs are never used for deduplication;
- access readiness (``AccessStatus``) is orthogonal to ``CandidateStatus``
  and ``SourceStatus``. Untrusted discovery can only claim
  ``CREDENTIALS_PENDING`` on an existing candidate — never ``PUBLIC``,
  ``READY``, ``AUTH_FAILED`` or ``ACCESS_REVOKED``;
- a registry qualification decision is never a fabricated
  ``ReconAssessment``; an ``AGENT_RECON`` decision must reference a real
  assessment that belongs to the same candidate;
- every value is bounded, control-free, UTC-normalized, and safe to log
  without leaking credentials, cookies, tokens, or raw hostile content.

Deliberately NOT frozen here: fetching/polling registries (PR 18),
candidate reconciliation/admission and trusted qualification policy (PR 19),
crawl authorization/DNS/egress policy (PR 20), link extraction (PR 22),
semantic references (PR 23), MonitoringProfiles (PR 24/25), and credential
provisioning/verification/session lifecycle (PR 26).
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from darkula.domain.identifiers import (
    AccessEvidenceId,
    CandidateEventId,
    DiscoveryObservationId,
    QualificationDecisionId,
    ReconAssessmentId,
    SourceCandidateId,
    SourceEndpointId,
)
from darkula.domain.source import (
    METADATA_SECRET_MARKERS,
    CandidateEventType,
    CandidateStatus,
    EndpointType,
)

#: Bounded validation limits (measured in Unicode code points in Python and
#: ``char_length`` in PostgreSQL).
MAX_PROVIDER_ID_LENGTH = 100
MAX_PROVIDER_RECORD_ID_LENGTH = 256
MAX_PROVIDER_RECORD_VERSION_LENGTH = 128
MAX_DISCOVERY_REFERENCE_LENGTH = 2048
MAX_EVIDENCE_EXCERPT_LENGTH = 512
MAX_POLICY_ID_LENGTH = 100
MAX_POLICY_VERSION_LENGTH = 100
MAX_ACCESS_REASON_CODE_LENGTH = 100
MAX_EVIDENCE_OBSERVATIONS = 64

#: Provider IDs are stable lower-case ASCII logical names. Uppercase is
#: rejected rather than silently folded because provider identity is
#: semantic.
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
#: Policy identity/version are stable developer-controlled ASCII names.
_POLICY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s")
_ENCODED_CONTROL = re.compile(r"%(?:0[0-9a-fA-F]|1[0-9a-fA-F]|7[fF])")
_ENCODED_DOT = re.compile(r"%2e", re.IGNORECASE)
_ENCODED_SEPARATOR = re.compile(r"%2f|%5c", re.IGNORECASE)
#: A host consisting only of digits/dots is treated as an IPv4 literal
#: candidate and must parse or be rejected (never guessed).
_IPV4_LITERAL = re.compile(r"^[0-9.]+$")
#: DNS hostname labels (ASCII/IDNA form). Underscores and other characters
#: that the IDNA codec tolerates are rejected rather than interpreted.
_DNS_HOST = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
#: Access reason codes are a bounded developer-controlled vocabulary, so the
#: secret-marker heuristic does not apply (for example
#: ``PROVIDER_AUTH_CLAIM`` legitimately contains "auth").
_ACCESS_REASON_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_]*$")

_SUPPORTED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


class DiscoveryReferenceKind(StrEnum):
    """How a provider identified the claimed resource."""

    LOCATOR = "LOCATOR"
    NAME_ONLY = "NAME_ONLY"


class DiscoveryDetectionMethod(StrEnum):
    """How the untrusted claim was produced."""

    REGISTRY = "REGISTRY"
    CONTENT_LINK = "CONTENT_LINK"
    CONTENT_SEMANTIC = "CONTENT_SEMANTIC"
    MANUAL = "MANUAL"


class DiscoveryValidationStatus(StrEnum):
    """Outcome of the deterministic locator classification."""

    VALID_LOCATOR = "VALID_LOCATOR"
    UNRESOLVED_NAME = "UNRESOLVED_NAME"


class AccessStatus(StrEnum):
    """Orthogonal access-readiness vocabulary (never a lifecycle status).

    PR17's public ingest/write paths create only ``UNKNOWN`` observation
    claims and ``CREDENTIALS_PENDING`` evidence for an existing candidate.
    ``PUBLIC``/``READY`` require independently verified access and are never
    inferred from a provider's ``available``/``requires_auth`` flag.
    """

    UNKNOWN = "UNKNOWN"
    PUBLIC = "PUBLIC"
    CREDENTIALS_PENDING = "CREDENTIALS_PENDING"
    READY = "READY"
    AUTH_FAILED = "AUTH_FAILED"
    ACCESS_REVOKED = "ACCESS_REVOKED"


class QualificationMethod(StrEnum):
    """Who/which policy produced a qualification decision."""

    REGISTRY_POLICY = "REGISTRY_POLICY"
    AGENT_RECON = "AGENT_RECON"


class QualificationOutcome(StrEnum):
    """The finite qualification outcome vocabulary."""

    QUALIFIED = "QUALIFIED"
    RECONNAISSANCE_PENDING = "RECONNAISSANCE_PENDING"
    REJECTED = "REJECTED"


#: Every outcome maps to exactly one existing candidate status and one
#: existing candidate event type. No new lifecycle vocabulary is invented.
_QUALIFICATION_STATUS: dict[QualificationOutcome, CandidateStatus] = {
    QualificationOutcome.QUALIFIED: CandidateStatus.QUALIFIED,
    QualificationOutcome.RECONNAISSANCE_PENDING: (
        CandidateStatus.RECONNAISSANCE_PENDING
    ),
    QualificationOutcome.REJECTED: CandidateStatus.REJECTED,
}

_QUALIFICATION_EVENT: dict[QualificationOutcome, CandidateEventType] = {
    QualificationOutcome.QUALIFIED: CandidateEventType.QUALIFIED,
    QualificationOutcome.RECONNAISSANCE_PENDING: CandidateEventType.NEEDS_MORE_RECON,
    QualificationOutcome.REJECTED: CandidateEventType.REJECTED,
}


def qualification_new_status(outcome: QualificationOutcome) -> CandidateStatus:
    """Return the candidate status an outcome must transition to."""
    return _QUALIFICATION_STATUS[outcome]


def qualification_event_type(outcome: QualificationOutcome) -> CandidateEventType:
    """Return the exact candidate event type an outcome commits with."""
    return _QUALIFICATION_EVENT[outcome]


def _reject_secret_markers(value: str, *, field_name: str) -> None:
    """Reject credentials/secrets detectable through the frozen vocabulary."""
    lowered = value.lower()
    if any(marker in lowered for marker in METADATA_SECRET_MARKERS):
        raise ValueError(f"{field_name} must not contain credential-like values")


def _validate_text(
    value: str,
    *,
    field_name: str,
    max_length: int,
    allow_internal_whitespace: bool = False,
    check_secrets: bool = True,
) -> str:
    """Return a bounded, control-free text value with stable surrounding text.

    Leading/trailing whitespace is always rejected (never silently trimmed)
    for identity/reference fields; excerpts are trimmed explicitly by the
    caller. Internal whitespace is rejected unless the field is explicitly
    human-readable (name-only references).
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if len(value) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{field_name} must not contain control characters")
    if not allow_internal_whitespace and _WHITESPACE.search(value):
        raise ValueError(f"{field_name} must not contain whitespace")
    if check_secrets:
        _reject_secret_markers(value, field_name=field_name)
    return value


def _validate_optional_text(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
) -> str | None:
    """Return ``None`` or one validated bounded text value."""
    if value is None:
        return None
    return _validate_text(value, field_name=field_name, max_length=max_length)


def _validate_excerpt(value: str | None) -> str | None:
    """Return ``None`` or one bounded, trimmed evidence excerpt.

    Excerpts may be trimmed (they are content fragments, not identity) but
    are still bounded, control-free, and never logged.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("evidence_excerpt must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("evidence_excerpt must not be blank")
    return _validate_text(
        trimmed,
        field_name="evidence_excerpt",
        max_length=MAX_EVIDENCE_EXCERPT_LENGTH,
        allow_internal_whitespace=True,
    )


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    """Return ``value`` normalized to timezone-aware UTC (naive rejected)."""
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


def _require_instance(
    value: object, expected_type: type[object], *, field_name: str
) -> None:
    """Reject foreign values instead of coercing them."""
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} must be a {expected_type.__name__}")


def _validate_provider_id(value: str) -> str:
    """Validate the stable lower-case provider identity."""
    if not isinstance(value, str):
        raise ValueError("provider_id must be a string")
    if len(value) > MAX_PROVIDER_ID_LENGTH:
        raise ValueError(
            f"provider_id must not exceed {MAX_PROVIDER_ID_LENGTH} characters"
        )
    if _PROVIDER_ID.fullmatch(value) is None:
        raise ValueError(
            "provider_id must be lower-case ASCII [a-z0-9][a-z0-9._-]* "
            "(uppercase is rejected, never folded)"
        )
    return value


def _validate_policy_name(value: str, *, field_name: str) -> str:
    """Validate a stable developer-controlled policy ID/version."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if len(value) > MAX_POLICY_ID_LENGTH:
        raise ValueError(
            f"{field_name} must not exceed {MAX_POLICY_ID_LENGTH} characters"
        )
    if _POLICY_NAME.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a stable ASCII name")
    return value


def _validate_path_and_query(path: str, query: str) -> None:
    """Reject ambiguous path/query encodings without rewriting the URI."""
    if _ENCODED_SEPARATOR.search(path) or _ENCODED_SEPARATOR.search(query):
        raise ValueError("locator must not contain percent-encoded slash or backslash")
    if _ENCODED_CONTROL.search(path) or _ENCODED_CONTROL.search(query):
        raise ValueError("locator must not contain percent-encoded control characters")
    for segment in path.split("/"):
        # Reject dot segments (including percent-encoded dots) rather than
        # resolving them into a different identity.
        if _ENCODED_DOT.sub(".", segment) in (".", ".."):
            raise ValueError("locator must not contain dot path segments")
    for key, _value in parse_qsl(query, keep_blank_values=True):
        lowered = key.lower()
        if any(marker in lowered for marker in METADATA_SECRET_MARKERS):
            raise ValueError("locator query must not contain secret-like keys")


def _normalize_locator_once(raw: str) -> str:
    """Normalize one absolute HTTP(S) locator exactly once (no I/O)."""
    if not isinstance(raw, str):
        raise ValueError("locator must be a string")
    if not raw:
        raise ValueError("locator must not be blank")
    if len(raw) > MAX_DISCOVERY_REFERENCE_LENGTH:
        raise ValueError(
            f"locator must not exceed {MAX_DISCOVERY_REFERENCE_LENGTH} characters"
        )
    if raw != raw.strip():
        raise ValueError("locator must not have leading/trailing whitespace")
    if _WHITESPACE.search(raw) or _CONTROL_CHARS.search(raw):
        raise ValueError("locator must not contain whitespace or control characters")
    if "\\" in raw:
        raise ValueError("locator must not contain backslashes")
    if "#" in raw:
        raise ValueError("locator must not contain a fragment")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:  # malformed IPv6 literal / bracket syntax
        raise ValueError("locator is not a valid URI reference") from exc
    scheme = parts.scheme.lower()
    if scheme not in _SUPPORTED_SCHEMES:
        raise ValueError("locator scheme must be http or https")
    netloc = parts.netloc
    if not netloc:
        raise ValueError("locator must include a hostname")
    if "@" in netloc:
        raise ValueError("locator must not embed credentials")
    if netloc.endswith(":"):
        raise ValueError("locator port is empty")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("locator port is invalid") from exc
    if host is None or not host:
        raise ValueError("locator must include a hostname")
    if ":" in host:
        if "%" in host:
            raise ValueError("locator must not contain an IPv6 zone identifier")
        try:
            parsed_ipv6 = ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError("locator IPv6 address is invalid") from exc
        normalized_host = f"[{parsed_ipv6.compressed}]"
    elif _IPV4_LITERAL.fullmatch(host) is not None:
        try:
            parsed_ipv4 = ipaddress.IPv4Address(host)
        except ValueError as exc:
            raise ValueError("locator IPv4 address is invalid") from exc
        normalized_host = str(parsed_ipv4)
    else:
        if host.endswith("."):
            raise ValueError("locator host must not have a trailing dot")
        try:
            # Standard-library IDNA only; non-ASCII hosts must yield a valid
            # ASCII hostname or the observation is rejected.
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("locator hostname is not a valid DNS name") from exc
        normalized_host = ascii_host.lower()
        if _DNS_HOST.fullmatch(normalized_host) is None:
            raise ValueError("locator hostname is not a valid DNS name")
    _validate_path_and_query(parts.path, parts.query)
    default_port = _DEFAULT_PORTS[scheme]
    if port is None or port == default_port:
        normalized_netloc = normalized_host
    else:
        if port < 1 or port > 65535:
            raise ValueError("locator port is invalid")
        normalized_netloc = f"{normalized_host}:{port}"
    return urlunsplit((scheme, normalized_netloc, parts.path or "/", parts.query, ""))


def normalize_discovery_locator(raw: str) -> str:
    """Return the conservative, identity-safe syntactic locator form.

    Deterministic and I/O-free: no DNS, HTTP, Tor, redirect, unshortening,
    or source-equivalence logic. Private/link-local addresses are **not**
    blocked here (they remain evidence); crawl authorization is a later,
    independent decision. The result is idempotent: normalizing it again
    yields the same string.
    """
    normalized = _normalize_locator_once(raw)
    if _normalize_locator_once(normalized) != normalized:
        raise ValueError("locator normalization is not idempotent")
    return normalized


def _length_prefixed_digest(components: tuple[str, ...]) -> str:
    """SHA-256 over UTF-8, 4-byte big-endian length-prefixed components."""
    digest = hashlib.sha256()
    for component in components:
        encoded = component.encode("utf-8")
        if len(encoded) >= 2**32:
            raise ValueError("semantic key component is too large")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def discovery_semantic_key(
    *,
    provider_id: str,
    provider_record_id: str,
    provider_record_version: str,
    detection_method: DiscoveryDetectionMethod,
    reference_kind: DiscoveryReferenceKind,
) -> str:
    """Return the stable semantic replay key for one provider observation.

    The tuple distinguishes a provider's distinct records and revisions. It
    never equates equal URLs from different providers and never incorporates
    candidate identity, timestamps, broker positions, or generated UUIDs.
    """
    return _length_prefixed_digest(
        (
            provider_id,
            provider_record_id,
            provider_record_version,
            detection_method.value,
            reference_kind.value,
        )
    )


def qualification_decision_key(
    *,
    candidate_id: SourceCandidateId,
    method: QualificationMethod,
    policy_id: str,
    policy_version: str,
    evidence_observation_ids: tuple[DiscoveryObservationId, ...],
    recon_assessment_id: ReconAssessmentId | None,
) -> str:
    """Return the stable replay key for one qualification decision."""
    ordered_evidence = sorted(str(item) for item in evidence_observation_ids)
    components = [
        str(candidate_id),
        method.value,
        policy_id,
        policy_version,
        *ordered_evidence,
        "" if recon_assessment_id is None else str(recon_assessment_id),
    ]
    return _length_prefixed_digest(tuple(components))


@dataclass(frozen=True, slots=True)
class DiscoveryInput:
    """One bounded, provider-neutral, untrusted discovery claim.

    ``observed_at`` is the provider observation time;
    :attr:`DiscoveryObservation.ingested_at` is Darkula acceptance time.
    ``provider_record_version`` must be an explicit provider revision or a
    stable adapter-assigned record-content digest — never a fetch timestamp.
    """

    provider_id: str
    provider_record_id: str
    provider_record_version: str
    detection_method: DiscoveryDetectionMethod
    reference_kind: DiscoveryReferenceKind
    asserted_reference: str
    observed_at: datetime
    evidence_reference: str
    source_kind_hint: EndpointType | None = None
    requires_auth_claim: bool | None = None
    origin_reference: str | None = None
    evidence_excerpt: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _validate_provider_id(self.provider_id))
        object.__setattr__(
            self,
            "provider_record_id",
            _validate_text(
                self.provider_record_id,
                field_name="provider_record_id",
                max_length=MAX_PROVIDER_RECORD_ID_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "provider_record_version",
            _validate_text(
                self.provider_record_version,
                field_name="provider_record_version",
                max_length=MAX_PROVIDER_RECORD_VERSION_LENGTH,
            ),
        )
        _require_instance(
            self.detection_method,
            DiscoveryDetectionMethod,
            field_name="detection_method",
        )
        _require_instance(
            self.reference_kind, DiscoveryReferenceKind, field_name="reference_kind"
        )
        object.__setattr__(
            self,
            "observed_at",
            _require_utc(self.observed_at, field_name="observed_at"),
        )
        object.__setattr__(
            self,
            "evidence_reference",
            _validate_text(
                self.evidence_reference,
                field_name="evidence_reference",
                max_length=MAX_DISCOVERY_REFERENCE_LENGTH,
            ),
        )
        if self.source_kind_hint is not None:
            _require_instance(
                self.source_kind_hint, EndpointType, field_name="source_kind_hint"
            )
        if self.requires_auth_claim is not None and not isinstance(
            self.requires_auth_claim, bool
        ):
            raise ValueError("requires_auth_claim must be a bool or None")
        object.__setattr__(
            self,
            "origin_reference",
            _validate_optional_text(
                self.origin_reference,
                field_name="origin_reference",
                max_length=MAX_DISCOVERY_REFERENCE_LENGTH,
            ),
        )
        object.__setattr__(
            self, "evidence_excerpt", _validate_excerpt(self.evidence_excerpt)
        )
        asserted = self.asserted_reference
        if not isinstance(asserted, str):
            raise ValueError("asserted_reference must be a string")
        if self.reference_kind is DiscoveryReferenceKind.NAME_ONLY:
            if "://" in asserted:
                raise ValueError(
                    "a name-only reference must not be an absolute URL; "
                    "classify it as LOCATOR instead"
                )
            if asserted != asserted.strip():
                raise ValueError(
                    "a name-only reference must not have leading/trailing whitespace"
                )
            object.__setattr__(
                self,
                "asserted_reference",
                _validate_text(
                    asserted,
                    field_name="asserted_reference",
                    max_length=MAX_DISCOVERY_REFERENCE_LENGTH,
                    allow_internal_whitespace=True,
                ),
            )
        else:
            normalize_discovery_locator(asserted)


@dataclass(frozen=True, slots=True)
class DiscoveryObservation:
    """One immutable accepted discovery observation.

    ``candidate_id`` is always ``None`` for PR17 ingestion: discovery never
    creates, reconciles, or promotes candidates (PR 19 owns admission).
    """

    observation_id: DiscoveryObservationId
    provider_id: str
    provider_record_id: str
    provider_record_version: str
    detection_method: DiscoveryDetectionMethod
    reference_kind: DiscoveryReferenceKind
    asserted_reference: str
    normalized_locator: str | None
    validation_status: DiscoveryValidationStatus
    observed_at: datetime
    ingested_at: datetime
    evidence_reference: str
    semantic_key: str
    source_kind_hint: EndpointType | None = None
    requires_auth_claim: bool | None = None
    origin_reference: str | None = None
    evidence_excerpt: str | None = None
    candidate_id: SourceCandidateId | None = None

    def __post_init__(self) -> None:
        _require_instance(
            self.observation_id, DiscoveryObservationId, field_name="observation_id"
        )
        source = DiscoveryInput(
            provider_id=self.provider_id,
            provider_record_id=self.provider_record_id,
            provider_record_version=self.provider_record_version,
            detection_method=self.detection_method,
            reference_kind=self.reference_kind,
            asserted_reference=self.asserted_reference,
            observed_at=self.observed_at,
            evidence_reference=self.evidence_reference,
            source_kind_hint=self.source_kind_hint,
            requires_auth_claim=self.requires_auth_claim,
            origin_reference=self.origin_reference,
            evidence_excerpt=self.evidence_excerpt,
        )
        object.__setattr__(
            self,
            "ingested_at",
            _require_utc(self.ingested_at, field_name="ingested_at"),
        )
        if self.candidate_id is not None:
            _require_instance(
                self.candidate_id, SourceCandidateId, field_name="candidate_id"
            )
        expected_locator, expected_status = classify_discovery_reference(source)
        if self.normalized_locator != expected_locator:
            raise ValueError(
                "normalized_locator does not match the asserted reference "
                "classification"
            )
        _require_instance(
            self.validation_status,
            DiscoveryValidationStatus,
            field_name="validation_status",
        )
        if self.validation_status is not expected_status:
            raise ValueError(
                "validation_status does not match the asserted reference classification"
            )
        expected_key = discovery_semantic_key(
            provider_id=source.provider_id,
            provider_record_id=source.provider_record_id,
            provider_record_version=source.provider_record_version,
            detection_method=source.detection_method,
            reference_kind=source.reference_kind,
        )
        if self.semantic_key != expected_key:
            raise ValueError("semantic_key does not match the observation identity")


def classify_discovery_reference(
    source: DiscoveryInput,
) -> tuple[str | None, DiscoveryValidationStatus]:
    """Return the normalized locator (or ``None``) and validation status."""
    if source.reference_kind is DiscoveryReferenceKind.NAME_ONLY:
        return None, DiscoveryValidationStatus.UNRESOLVED_NAME
    return (
        normalize_discovery_locator(source.asserted_reference),
        DiscoveryValidationStatus.VALID_LOCATOR,
    )


def observation_from_input(
    source: DiscoveryInput,
    *,
    observation_id: DiscoveryObservationId,
    ingested_at: datetime,
) -> DiscoveryObservation:
    """Build the immutable observation an adapter persists for ``source``.

    PR17 ingestion never links a candidate, so ``candidate_id`` is always
    ``None`` here.
    """
    normalized_locator, validation_status = classify_discovery_reference(source)
    return DiscoveryObservation(
        observation_id=observation_id,
        provider_id=source.provider_id,
        provider_record_id=source.provider_record_id,
        provider_record_version=source.provider_record_version,
        detection_method=source.detection_method,
        reference_kind=source.reference_kind,
        asserted_reference=source.asserted_reference,
        normalized_locator=normalized_locator,
        validation_status=validation_status,
        observed_at=source.observed_at,
        ingested_at=ingested_at,
        evidence_reference=source.evidence_reference,
        semantic_key=discovery_semantic_key(
            provider_id=source.provider_id,
            provider_record_id=source.provider_record_id,
            provider_record_version=source.provider_record_version,
            detection_method=source.detection_method,
            reference_kind=source.reference_kind,
        ),
        source_kind_hint=source.source_kind_hint,
        requires_auth_claim=source.requires_auth_claim,
        origin_reference=source.origin_reference,
        evidence_excerpt=source.evidence_excerpt,
        candidate_id=None,
    )


@dataclass(frozen=True, slots=True)
class AccessEvidence:
    """One immutable, non-authorizing access observation.

    Exactly one of ``candidate_id``/``source_endpoint_id`` is set: access
    requirements discovered before admission stay on the discovery
    observation, and PR17 never fabricates a candidate identity.
    """

    access_evidence_id: AccessEvidenceId
    observation_id: DiscoveryObservationId
    candidate_id: SourceCandidateId | None
    source_endpoint_id: SourceEndpointId | None
    status: AccessStatus
    reason_code: str
    recorded_at: datetime
    evidence_reference: str

    def __post_init__(self) -> None:
        _require_instance(
            self.access_evidence_id, AccessEvidenceId, field_name="access_evidence_id"
        )
        _require_instance(
            self.observation_id, DiscoveryObservationId, field_name="observation_id"
        )
        if self.candidate_id is not None:
            _require_instance(
                self.candidate_id, SourceCandidateId, field_name="candidate_id"
            )
        if self.source_endpoint_id is not None:
            _require_instance(
                self.source_endpoint_id,
                SourceEndpointId,
                field_name="source_endpoint_id",
            )
        if (self.candidate_id is None) == (self.source_endpoint_id is None):
            raise ValueError(
                "exactly one of candidate_id or source_endpoint_id must be set"
            )
        _require_instance(self.status, AccessStatus, field_name="status")
        reason_code = self.reason_code
        if not isinstance(reason_code, str):
            raise ValueError("reason_code must be a string")
        if len(reason_code) > MAX_ACCESS_REASON_CODE_LENGTH:
            raise ValueError(
                "reason_code must not exceed "
                f"{MAX_ACCESS_REASON_CODE_LENGTH} characters"
            )
        if _ACCESS_REASON_CODE.fullmatch(reason_code) is None:
            raise ValueError(
                "reason_code must be a stable upper-case vocabulary token "
                "[A-Z0-9][A-Z0-9_]*"
            )
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(
            self,
            "recorded_at",
            _require_utc(self.recorded_at, field_name="recorded_at"),
        )
        object.__setattr__(
            self,
            "evidence_reference",
            _validate_text(
                self.evidence_reference,
                field_name="evidence_reference",
                max_length=MAX_DISCOVERY_REFERENCE_LENGTH,
            ),
        )


@dataclass(frozen=True, slots=True)
class QualificationDecision:
    """One immutable qualification decision (persistence primitive only).

    No PR17 service produces decisions: PR 19 owns deciding eligibility,
    policy trust, recon routing, and invoking
    :meth:`~darkula.app.repositories.QualificationRepository.decide_and_transition`.
    """

    decision_id: QualificationDecisionId
    candidate_id: SourceCandidateId
    method: QualificationMethod
    outcome: QualificationOutcome
    evaluated_at: datetime
    policy_id: str
    policy_version: str
    evidence_observation_ids: tuple[DiscoveryObservationId, ...]
    event_id: CandidateEventId
    expected_status: CandidateStatus
    new_status: CandidateStatus
    decision_key: str
    provider_id: str | None = None
    provider_record_id: str | None = None
    recon_assessment_id: ReconAssessmentId | None = None

    def __post_init__(self) -> None:
        _require_instance(
            self.decision_id, QualificationDecisionId, field_name="decision_id"
        )
        _require_instance(
            self.candidate_id, SourceCandidateId, field_name="candidate_id"
        )
        _require_instance(self.method, QualificationMethod, field_name="method")
        _require_instance(self.outcome, QualificationOutcome, field_name="outcome")
        _require_instance(self.event_id, CandidateEventId, field_name="event_id")
        _require_instance(
            self.expected_status, CandidateStatus, field_name="expected_status"
        )
        _require_instance(self.new_status, CandidateStatus, field_name="new_status")
        object.__setattr__(
            self,
            "evaluated_at",
            _require_utc(self.evaluated_at, field_name="evaluated_at"),
        )
        object.__setattr__(
            self,
            "policy_id",
            _validate_policy_name(self.policy_id, field_name="policy_id"),
        )
        object.__setattr__(
            self,
            "policy_version",
            _validate_policy_name(self.policy_version, field_name="policy_version"),
        )
        evidence = self.evidence_observation_ids
        if not isinstance(evidence, tuple) or not evidence:
            raise ValueError("evidence_observation_ids must be a non-empty tuple")
        if len(evidence) > MAX_EVIDENCE_OBSERVATIONS:
            raise ValueError(
                "evidence_observation_ids must not exceed "
                f"{MAX_EVIDENCE_OBSERVATIONS} entries"
            )
        for item in evidence:
            _require_instance(
                item, DiscoveryObservationId, field_name="evidence_observation_id"
            )
        if len(set(evidence)) != len(evidence):
            raise ValueError("evidence_observation_ids must be unique")
        if self.method is QualificationMethod.REGISTRY_POLICY:
            if self.provider_id is None or self.provider_record_id is None:
                raise ValueError(
                    "a REGISTRY_POLICY decision requires provider_id and "
                    "provider_record_id"
                )
            if self.recon_assessment_id is not None:
                raise ValueError(
                    "a REGISTRY_POLICY decision must not reference a "
                    "ReconAssessment (never fabricate one)"
                )
        else:
            if self.recon_assessment_id is None:
                raise ValueError(
                    "an AGENT_RECON decision requires a real recon_assessment_id"
                )
        if self.provider_id is not None:
            object.__setattr__(
                self, "provider_id", _validate_provider_id(self.provider_id)
            )
        if self.provider_record_id is not None:
            object.__setattr__(
                self,
                "provider_record_id",
                _validate_text(
                    self.provider_record_id,
                    field_name="provider_record_id",
                    max_length=MAX_PROVIDER_RECORD_ID_LENGTH,
                ),
            )
        if self.recon_assessment_id is not None:
            _require_instance(
                self.recon_assessment_id,
                ReconAssessmentId,
                field_name="recon_assessment_id",
            )
        expected_status = qualification_new_status(self.outcome)
        if self.new_status is not expected_status:
            raise ValueError(
                f"outcome {self.outcome.value} must transition to "
                f"{expected_status.value}"
            )
        if self.expected_status is self.new_status:
            raise ValueError("expected_status must differ from new_status")
        expected_key = qualification_decision_key(
            candidate_id=self.candidate_id,
            method=self.method,
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            evidence_observation_ids=evidence,
            recon_assessment_id=self.recon_assessment_id,
        )
        if self.decision_key != expected_key:
            raise ValueError("decision_key does not match the decision identity")


__all__ = [
    "MAX_ACCESS_REASON_CODE_LENGTH",
    "MAX_DISCOVERY_REFERENCE_LENGTH",
    "MAX_EVIDENCE_EXCERPT_LENGTH",
    "MAX_EVIDENCE_OBSERVATIONS",
    "MAX_POLICY_ID_LENGTH",
    "MAX_POLICY_VERSION_LENGTH",
    "MAX_PROVIDER_ID_LENGTH",
    "MAX_PROVIDER_RECORD_ID_LENGTH",
    "MAX_PROVIDER_RECORD_VERSION_LENGTH",
    "AccessEvidence",
    "AccessStatus",
    "DiscoveryDetectionMethod",
    "DiscoveryInput",
    "DiscoveryObservation",
    "DiscoveryReferenceKind",
    "DiscoveryValidationStatus",
    "QualificationDecision",
    "QualificationMethod",
    "QualificationOutcome",
    "classify_discovery_reference",
    "discovery_semantic_key",
    "normalize_discovery_locator",
    "observation_from_input",
    "qualification_decision_key",
    "qualification_event_type",
    "qualification_new_status",
]
