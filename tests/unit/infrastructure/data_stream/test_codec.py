# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic wire-codec tests (DS1-DS8, DS16).

Covers the canonical round-trip, absent-optional determinism, UTC
timestamp preservation, strict rejection of malformed/missing/invalid
envelopes, and the guarantee that trace headers never enter the payload.
No broker is involved.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import pytest

from darkula.app.data_stream import StreamMessage
from darkula.domain.identifiers import (
    CausationId,
    CorrelationId,
    MessageId,
)
from darkula.infrastructure.data_stream.codec import (
    ENVELOPE_VERSION,
    MessageCodecError,
    decode_stream_message,
    encode_stream_message,
)

_T0 = datetime(2026, 3, 4, 5, 6, 7, 891011, tzinfo=UTC)


def _message(**overrides: object) -> StreamMessage:
    """Build a fully-populated valid message with override support."""
    base: dict[str, object] = {
        "message_id": MessageId.generate(),
        "message_type": "crawl_requested",
        "schema_version": 2,
        "occurred_at": _T0,
        "payload": {"url": "https://example.com/a", "depth": 1, "flags": ["x"]},
        "correlation_id": CorrelationId.generate(),
        "causation_id": CausationId.generate(),
        "routing_key": "lane-alpha",
    }
    base.update(overrides)
    return StreamMessage(
        message_id=base["message_id"],  # type: ignore[arg-type]
        message_type=base["message_type"],  # type: ignore[arg-type]
        schema_version=base["schema_version"],  # type: ignore[arg-type]
        occurred_at=base["occurred_at"],  # type: ignore[arg-type]
        payload=base["payload"],  # type: ignore[arg-type]
        correlation_id=base["correlation_id"],  # type: ignore[arg-type]
        causation_id=base["causation_id"],  # type: ignore[arg-type]
        routing_key=base["routing_key"],  # type: ignore[arg-type]
    )


def _decoded(envelope: dict[str, object]) -> StreamMessage:
    """Encode an envelope dict and decode it back."""
    return decode_stream_message(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _envelope(message: StreamMessage) -> dict[str, object]:
    """Return the decoded wire envelope of a message (test inspection)."""
    return cast(
        "dict[str, object]",
        json.loads(encode_stream_message(message).decode("utf-8")),
    )


class TestRoundTrip:
    """DS1/DS3: exact semantic round-trip; UTC preserved."""

    def test_valid_envelope_round_trips_exactly(self) -> None:
        message = _message()
        decoded = decode_stream_message(encode_stream_message(message))
        assert decoded == message

    def test_utc_timestamp_preserved_exactly(self) -> None:
        micros = datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
        message = _message(occurred_at=micros)
        decoded = decode_stream_message(encode_stream_message(message))
        assert decoded.occurred_at == micros

    def test_envelope_version_field_is_written(self) -> None:
        assert _envelope(_message())["envelope_version"] == ENVELOPE_VERSION


class TestAbsentOptionals:
    """DS2: absent optional identifiers have one deterministic encoding."""

    def test_absent_optionals_are_omitted(self) -> None:
        message = StreamMessage(
            message_id=MessageId.generate(),
            message_type="ping",
            schema_version=1,
            occurred_at=_T0,
            payload={},
        )
        wire = encode_stream_message(message)
        decoded = json.loads(wire.decode("utf-8"))
        assert "correlation_id" not in decoded
        assert "causation_id" not in decoded
        assert "routing_key" not in decoded
        assert decode_stream_message(wire) == message

    def test_encoding_is_deterministic(self) -> None:
        message = _message()
        assert encode_stream_message(message) == encode_stream_message(message)


class TestStrictRejection:
    """DS4-DS6: malformed/missing/invalid envelopes are rejected boundedly."""

    def test_malformed_json_rejected(self) -> None:
        with pytest.raises(MessageCodecError):
            decode_stream_message(b"{not json")

    def test_non_utf8_rejected(self) -> None:
        with pytest.raises(MessageCodecError):
            decode_stream_message(b"\xff\xfe\x00")

    def test_missing_required_field_rejected(self) -> None:
        envelope = _envelope(_message())
        del envelope["message_type"]
        with pytest.raises(MessageCodecError):
            _decoded(envelope)

    def test_unknown_top_level_field_rejected(self) -> None:
        envelope = _envelope(_message())
        envelope["surprise"] = 1
        with pytest.raises(MessageCodecError):
            _decoded(envelope)

    def test_unsupported_schema_version_rejected(self) -> None:
        envelope = _envelope(_message())
        envelope["schema_version"] = 0
        with pytest.raises(MessageCodecError):
            _decoded(envelope)
        envelope["schema_version"] = -2
        with pytest.raises(MessageCodecError):
            _decoded(envelope)

    def test_unsupported_envelope_version_rejected(self) -> None:
        envelope = _envelope(_message())
        envelope["envelope_version"] = 99
        with pytest.raises(MessageCodecError):
            _decoded(envelope)

    def test_invalid_identifiers_rejected(self) -> None:
        envelope = _envelope(_message())
        envelope["message_id"] = "not-a-uuid"
        with pytest.raises(MessageCodecError):
            _decoded(envelope)
        envelope = _envelope(_message())
        envelope["correlation_id"] = "also-not-a-uuid"
        with pytest.raises(MessageCodecError):
            _decoded(envelope)

    def test_invalid_timestamp_rejected(self) -> None:
        envelope = _envelope(_message())
        envelope["occurred_at"] = "yesterday-ish"
        with pytest.raises(MessageCodecError):
            _decoded(envelope)
        envelope = _envelope(_message())
        envelope["occurred_at"] = "2026-01-01T00:00:00"  # naive
        with pytest.raises(MessageCodecError):
            _decoded(envelope)

    def test_non_object_root_rejected(self) -> None:
        with pytest.raises(MessageCodecError):
            decode_stream_message(b"[1, 2, 3]")
        with pytest.raises(MessageCodecError):
            decode_stream_message(b'"just a string"')

    def test_payload_required_and_mapping(self) -> None:
        envelope = _envelope(_message())
        envelope["payload"] = None
        with pytest.raises(MessageCodecError):
            _decoded(envelope)


class TestTraceHeaderPurity:
    """DS7: trace-header names never enter the payload/encoding."""

    def test_trace_header_keys_rejected_in_payload(self) -> None:
        for key in ("traceparent", "tracestate", "baggage", "TraceParent"):
            with pytest.raises(ValueError):
                _message(payload={key: "00-000-"})

    def test_wire_envelope_never_contains_trace_keys(self) -> None:
        text = encode_stream_message(_message()).decode("utf-8")
        assert "traceparent" not in text
        assert "tracestate" not in text
        assert "baggage" not in text


class TestBoundedErrors:
    """DS16: public codec errors never echo payloads or identifiers."""

    def test_error_messages_are_bounded_and_data_free(self) -> None:
        envelope = _envelope(_message())
        envelope["payload"] = {"secret_token": "super-secret-value"}
        envelope["message_id"] = "will-fail"
        with pytest.raises(MessageCodecError) as captured:
            _decoded(envelope)
        text = str(captured.value)
        assert "super-secret-value" not in text
        assert "will-fail" not in text
        assert "https://example.com/a" not in text
