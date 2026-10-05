# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula cross-cutting identifier and bounded-name value types.

PR 2 introduces the foundational value types (message/stream/consumer/
operation/object-key identifiers); PR 4 adds the source-domain persistence
identity types (candidate/event/source/endpoint/recon/source-assessment).

- ``MessageId`` / ``CorrelationId`` / ``CausationId`` are UUID-backed
  identifiers with deterministic equality and hashing. They are identity
  types, never transport state and never PostgreSQL idempotency keys.
- ``StreamName`` / ``ConsumerId`` are bounded logical names for the
  provider-neutral ``DataStream`` contract.
- ``OperationName`` is the bounded, stable operation identifier accepted by
  the ``LlmClient`` and observability contracts.
- ``ObjectKey`` is a logical object-store address: not a filesystem path and
  not an S3/R2 URI.
- ``SourceCandidateId`` / ``CandidateEventId`` / ``SourceId`` /
  ``SourceEndpointId`` / ``ReconAssessmentId`` / ``SourceAssessmentId`` are
  the PR 4 structured-domain persistence identities referenced by the
  source-domain records and their repositories.

Rules frozen here:

- values are immutable and compare/hash deterministically;
- blank logical names are rejected;
- names carrying control characters are rejected;
- provider-specific URI assumptions are never special-cased by these types;
- Python's built-in ``hash()`` is never used for durable routing or identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Self
from uuid import UUID, uuid4

#: Upper bound for logical names that may enter telemetry, logs, or
#: observability metadata (streams, consumers, operations).
MAX_LOGICAL_NAME_LENGTH = 200

#: Upper bound for logical object-store keys.
MAX_OBJECT_KEY_LENGTH = 1024

#: Control characters (C0 plus DEL) are rejected wherever a value may reach
#: logs, telemetry, or an object-store key namespace.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _validate_logical_name(
    raw: str,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """Return a trimmed, non-blank, bounded, control-free logical name."""
    if not raw:
        raise ValueError(f"{field_name} must not be blank")
    value = raw.strip()
    if not value:
        raise ValueError(f"{field_name} must not be blank")
    if len(value) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


def _validate_object_key(raw: str) -> str:
    """Validate a logical object-store key.

    An ``ObjectKey`` is a logical address, not a filesystem path and not a
    provider URI. Only blank, control-character, absolute-path, and length
    violations are rejected; provider-specific path rules are deliberately
    not over-designed here (PR 8 owns real key layouts).
    """
    value = _validate_logical_name(
        raw, field_name="object key", max_length=MAX_OBJECT_KEY_LENGTH
    )
    if value.startswith("/"):
        raise ValueError("object key must not be an absolute path")
    return value


@dataclass(frozen=True, slots=True)
class _UuidId:
    """Immutable UUID-backed identifier base.

    Equality and hashing are derived from the UUID value, and distinct
    identifier classes never compare equal even when they share a UUID.
    """

    value: UUID

    @classmethod
    def generate(cls) -> Self:
        """Return a new random identifier of this type."""
        return cls(value=uuid4())

    @classmethod
    def from_str(cls, raw: str) -> Self:
        """Return an identifier parsed from its canonical UUID string."""
        return cls(value=UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class MessageId(_UuidId):
    """Identity of one logical stream message.

    ``message_id`` is the identity of one logical message. It is never equal
    to a stream position and is not a PostgreSQL idempotency record.
    """


@dataclass(frozen=True, slots=True)
class CorrelationId(_UuidId):
    """Workflow/request correlation identifier.

    ``correlation_id`` correlates the messages and events of one workflow or
    request across asynchronous boundaries.
    """


@dataclass(frozen=True, slots=True)
class CausationId(_UuidId):
    """Identity of the message/event that caused another message.

    ``causation_id`` records which message/event produced a follow-up
    message; it is evidence lineage, not transport state.
    """


class SourceCandidateId(_UuidId):
    """Persistence identity of one :class:`~darkula.domain.source.SourceCandidate`.

    A discovered resource, distinct from a managed
    :class:`~darkula.domain.source.Source`. Candidate identity is not the
    entrypoint text.
    """


class CandidateEventId(_UuidId):
    """Identity of one immutable candidate lifecycle history record."""


class SourceId(_UuidId):
    """Persistence identity of one managed :class:`~darkula.domain.source.Source`.

    A logical managed source, not a URL: identity survives endpoint/mirror
    rotation.
    """


class SourceEndpointId(_UuidId):
    """Identity of one :class:`~darkula.domain.source.SourceEndpoint`."""


class CollectionPolicyId(_UuidId):
    """Identity of one :class:`~darkula.domain.collection.CollectionPolicy` (PR 9).

    A policy is durable authorization/configuration for the managed-source
    collection lifecycle; it is never a URI and never a broker position.
    """


class CollectionRunId(_UuidId):
    """Identity of one :class:`~darkula.domain.collection.CollectionRun` (PR 9).

    A run is one historical execution of a policy occurrence. The run id is
    the authoritative work identity for collection execution and is never a
    broker position and never an endpoint URI.
    """


class ReconAssessmentId(_UuidId):
    """Identity of one immutable recon assessment record."""


class SourceAssessmentId(_UuidId):
    """Identity of one immutable, time-windowed source assessment record."""


class ContentArtifactId(_UuidId):
    """Identity of one :class:`~darkula.domain.content.ContentArtifact`.

    A logical artifact record/reference; distinct from the physical
    ``ObjectKey`` it addresses and from every ``NormalizedContentId`` that
    observes it. PR 8 freezes the rule that bytes deduplication never merges
    these records.
    """


class NormalizedContentId(_UuidId):
    """Identity of one normalized content observation (PR 8).

    One observation's semantic/persistence identity, distinct from
    ``ContentArtifactId``, from ``ObjectKey``, and from ``ContentHash``. Two
    observations may share a physical artifact and its hash while remaining
    distinct provenance records.
    """


class ExtractionResultId(_UuidId):
    """Identity of one persisted extraction result (PR 11).

    Distinct from the ``NormalizedContentId`` it was derived from, from every
    ``ExtractedEntityId`` occurrence, and from a semantic value. The
    semantic/idempotency identity is ``(content_id, profile_name,
    profile_version)``, never this generated UUID and never an ObjectKey or
    content hash.
    """


class ExtractedEntityId(_UuidId):
    """Identity of one persisted extracted-entity occurrence (PR 11).

    Preserves ``fact != global truth``. A value extracted at two spans is two
    ``ExtractedEntity`` records; this identity is one persistence occurrence,
    never the semantic value and never a global IOC identity.
    """


@dataclass(frozen=True, slots=True)
class _BoundedName:
    """Immutable bounded-name base for logical Darkula names."""

    value: str
    max_length: ClassVar[int] = MAX_LOGICAL_NAME_LENGTH

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _validate_logical_name(
                self.value,
                field_name=type(self).__name__,
                max_length=self.max_length,
            ),
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class StreamName(_BoundedName):
    """Logical name of one DataStream.

    Transport-neutral: it is neither a Redpanda topic name nor a Kafka
    ``TopicPartition``. PR 5 maps logical stream names onto broker
    topology.
    """


@dataclass(frozen=True, slots=True)
class ConsumerId(_BoundedName):
    """Identity of one consumer/consumer group attached to a stream.

    Named without Kafka terminology (``consumer group``, ``client id``) so
    the application contract stays provider-neutral.
    """


@dataclass(frozen=True, slots=True)
class OperationName(_BoundedName):
    """Stable, bounded operation identifier for LLM/observability calls.

    Always static developer-controlled metadata, never prompt content.
    """


@dataclass(frozen=True, slots=True)
class ObjectKey(_BoundedName):
    """Logical object-store address.

    Not a filesystem path and not an S3/R2 URI. An ``ObjectKey`` is never
    equal to a content hash.
    """

    max_length: ClassVar[int] = MAX_OBJECT_KEY_LENGTH

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_object_key(self.value))


__all__ = [
    "MAX_LOGICAL_NAME_LENGTH",
    "MAX_OBJECT_KEY_LENGTH",
    "CandidateEventId",
    "CausationId",
    "CollectionPolicyId",
    "CollectionRunId",
    "ConsumerId",
    "ContentArtifactId",
    "CorrelationId",
    "ExtractedEntityId",
    "ExtractionResultId",
    "MessageId",
    "NormalizedContentId",
    "ObjectKey",
    "OperationName",
    "ReconAssessmentId",
    "SourceAssessmentId",
    "SourceCandidateId",
    "SourceEndpointId",
    "SourceId",
    "StreamName",
]
