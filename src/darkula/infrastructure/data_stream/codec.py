# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic versioned wire codec for Darkula stream messages (PR 5).

Maps the application :class:`~darkula.app.data_stream.StreamMessage` envelope
onto canonical UTF-8 JSON bytes and back, with no provider types involved.
The encoding is shared by the Redpanda adapter (and any future transport
adapter): exactly one wire representation exists for every valid envelope.

Wire format (envelope version 1):

.. code-block:: json

    {
      "envelope_version": 1,
      "message_id": "6f1b...<uuid>",
      "message_type": "crawl_requested",
      "schema_version": 1,
      "occurred_at": "2026-01-01T00:00:00+00:00",
      "payload": { },
      "correlation_id": "6f1b...",   // omitted when absent
      "causation_id": "6f1b...",     // omitted when absent
      "routing_key": "lane-a"        // omitted when absent
    }

Canonical rules:

- identifiers are canonical UUID strings;
- ``occurred_at`` is UTC ISO-8601 (``+00:00``) and is preserved exactly;
- absent optional fields are **omitted** (one representation for absent
  optionals: the key never appears);
- encoding is deterministic (sorted keys, fixed separators, ASCII
  escaping — same bytes for the same envelope);
- strict decoding rejects malformed JSON, missing required fields,
  invalid identifiers/timestamps, unsupported envelope or payload schema
  versions, and unknown top-level fields.

Trace context never lives in the payload: it travels in broker transport
metadata only (see :mod:`darkula.infrastructure.data_stream.redpanda`).

All failures surface as bounded :class:`MessageCodecError` instances whose
public messages never echo the wire bytes, payload values, or identifiers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from darkula.app.data_stream import (
    JsonValue,
    StreamMessage,
    validate_payload,
)
from darkula.domain.identifiers import (
    CausationId,
    CorrelationId,
    MessageId,
)

#: Current wire envelope format version.
ENVELOPE_VERSION = 1

#: Required top-level envelope fields.
_REQUIRED_FIELDS = frozenset(
    {
        "envelope_version",
        "message_id",
        "message_type",
        "schema_version",
        "occurred_at",
        "payload",
    }
)

#: Optional top-level envelope fields (omitted when absent).
_OPTIONAL_FIELDS = frozenset(
    {
        "correlation_id",
        "causation_id",
        "routing_key",
    }
)

_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS


class MessageCodecError(ValueError):
    """A bounded wire-codec failure.

    The message never echoes raw wire bytes, decoded payload values, or
    message identifiers. Codec failures are non-retryable contract
    failures: the transport adapter maps them to a bounded ``PollError``
    without acknowledging the offending record.
    """


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    """Encode one mapping as canonical deterministic UTF-8 JSON bytes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _encode_optional_uuid(field: str, raw: str | None) -> str | None:
    """Return the canonical UUID string or ``None`` (never validated here
    because the value already passed identifier construction)."""
    return raw


def encode_stream_message(message: StreamMessage) -> bytes:
    """Return the canonical wire bytes for one validated stream message.

    ``message`` is already fully validated by its own constructor; a
    programming error that produces unencodable values surfaces as a
    bounded :class:`MessageCodecError` (the public message stays data-free).
    """
    envelope: dict[str, JsonValue] = {
        "envelope_version": ENVELOPE_VERSION,
        "message_id": str(message.message_id),
        "message_type": message.message_type,
        "schema_version": message.schema_version,
        "occurred_at": message.occurred_at.isoformat(),
        "payload": message.payload,
    }
    if message.correlation_id is not None:
        envelope["correlation_id"] = str(message.correlation_id)
    if message.causation_id is not None:
        envelope["causation_id"] = str(message.causation_id)
    if message.routing_key is not None:
        envelope["routing_key"] = message.routing_key
    try:
        return _canonical_json(envelope)
    except (TypeError, ValueError) as exc:
        raise MessageCodecError("stream message is not wire-encodable") from exc


def _as_mapping(value: object) -> Mapping[str, Any]:
    """Require a JSON object at the envelope root."""
    if not isinstance(value, dict):
        raise MessageCodecError("stream message envelope must be a JSON object")
    return value


def _require_str_field(envelope: Mapping[str, Any], field: str) -> str:
    """Return a required string field or raise a bounded codec error."""
    value = envelope.get(field)
    if not isinstance(value, str):
        raise MessageCodecError(f"stream message field {field!r} is invalid")
    return value


def _require_int_field(envelope: Mapping[str, Any], field: str) -> int:
    """Return a required integer field or raise a bounded codec error."""
    value = envelope.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MessageCodecError(f"stream message field {field!r} is invalid")
    return value


def _optional_uuid_field(envelope: Mapping[str, Any], field: str) -> UUID | None:
    """Return an optional identifier field as UUID, ``None`` when absent."""
    value = envelope.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MessageCodecError(f"stream message field {field!r} is invalid")
    try:
        return UUID(value)
    except ValueError as exc:
        raise MessageCodecError(f"stream message field {field!r} is invalid") from exc


def decode_stream_message(data: bytes) -> StreamMessage:
    """Strictly decode canonical wire bytes into a validated stream message.

    Rejects malformed JSON, unknown top-level fields, missing required
    fields, invalid identifiers/timestamps, unsupported envelope versions,
    and unsupported payload schema versions. Every rejection is a bounded
    :class:`MessageCodecError` that never echoes the offending value.
    """
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MessageCodecError("stream message is not valid wire JSON") from exc

    envelope = _as_mapping(raw)
    unknown = sorted(set(envelope) - _ALLOWED_FIELDS)
    if unknown:
        raise MessageCodecError("stream message carries unknown envelope fields")

    if envelope.get("envelope_version") != ENVELOPE_VERSION:
        raise MessageCodecError("unsupported stream message envelope version")

    envelope_version = _require_int_field(envelope, "envelope_version")
    if envelope_version != ENVELOPE_VERSION:
        raise MessageCodecError("unsupported stream message envelope version")

    message_id_raw = _require_str_field(envelope, "message_id")
    try:
        message_id = MessageId.from_str(message_id_raw)
    except ValueError as exc:
        raise MessageCodecError("stream message field 'message_id' is invalid") from exc

    schema_version = _require_int_field(envelope, "schema_version")
    if schema_version < 1:
        raise MessageCodecError("unsupported stream message schema version")

    occurred_raw = _require_str_field(envelope, "occurred_at")
    try:
        occurred_at = datetime.fromisoformat(occurred_raw)
    except ValueError as exc:
        raise MessageCodecError(
            "stream message field 'occurred_at' is invalid"
        ) from exc
    if occurred_at.tzinfo is None or occurred_at.utcoffset() != UTC.utcoffset(None):
        raise MessageCodecError("stream message field 'occurred_at' must be UTC")

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise MessageCodecError("stream message field 'payload' is invalid")
    try:
        validate_payload(payload)
    except ValueError as exc:
        raise MessageCodecError("stream message payload is invalid") from exc

    correlation_raw = _optional_uuid_field(envelope, "correlation_id")
    causation_raw = _optional_uuid_field(envelope, "causation_id")
    routing_key = envelope.get("routing_key")
    if routing_key is not None and not isinstance(routing_key, str):
        raise MessageCodecError("stream message field 'routing_key' is invalid")

    try:
        return StreamMessage(
            message_id=message_id,
            message_type=_require_str_field(envelope, "message_type"),
            schema_version=schema_version,
            occurred_at=occurred_at,
            payload=payload,
            correlation_id=(
                None
                if correlation_raw is None
                else CorrelationId(value=correlation_raw)
            ),
            causation_id=(
                None if causation_raw is None else CausationId(value=causation_raw)
            ),
            routing_key=routing_key,
        )
    except ValueError as exc:
        raise MessageCodecError("stream message envelope is invalid") from exc


__all__ = [
    "ENVELOPE_VERSION",
    "MessageCodecError",
    "decode_stream_message",
    "encode_stream_message",
]
