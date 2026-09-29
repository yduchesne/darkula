# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for Darkula cross-cutting identifier/value types (ID-*)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from darkula.domain.identifiers import (
    MAX_LOGICAL_NAME_LENGTH,
    MAX_OBJECT_KEY_LENGTH,
    CausationId,
    ConsumerId,
    CorrelationId,
    MessageId,
    ObjectKey,
    OperationName,
    StreamName,
    _UuidId,
)

_IDENTIFIER_TYPES = (MessageId, CorrelationId, CausationId)


class TestUuidIdentifiers:
    """ID-01/ID-04: UUID-backed identity semantics."""

    @pytest.mark.parametrize("identifier_type", _IDENTIFIER_TYPES)
    def test_generate_produces_valid_uuid(
        self,
        identifier_type: type[_UuidId],
    ) -> None:
        identifier = identifier_type.generate()
        assert UUID(str(identifier.value)) == identifier.value

    @pytest.mark.parametrize("identifier_type", _IDENTIFIER_TYPES)
    def test_from_str_parses_canonical_form(
        self,
        identifier_type: type[_UuidId],
    ) -> None:
        raw = str(uuid4())
        identifier = identifier_type.from_str(raw)
        assert str(identifier) == raw

    @pytest.mark.parametrize("identifier_type", _IDENTIFIER_TYPES)
    def test_invalid_uuid_string_rejected(
        self,
        identifier_type: type[_UuidId],
    ) -> None:
        with pytest.raises(ValueError):
            identifier_type.from_str("not-a-uuid")

    def test_same_uuid_is_equal_and_hash_stable(self) -> None:
        raw = str(uuid4())
        first = MessageId.from_str(raw)
        second = MessageId.from_str(raw)
        assert first == second
        assert hash(first) == hash(second)
        assert first.value == second.value

    def test_different_identifier_types_never_equal(self) -> None:
        raw = str(uuid4())
        message = MessageId.from_str(raw)
        correlation = CorrelationId.from_str(raw)
        causation = CausationId.from_str(raw)
        assert message != correlation  # type: ignore[comparison-overlap]
        assert correlation != causation  # type: ignore[comparison-overlap]
        assert message != causation  # type: ignore[comparison-overlap]

    def test_generate_messages_are_distinct(self) -> None:
        assert MessageId.generate() != MessageId.generate()


class TestBoundedNames:
    """ID-01/ID-02/ID-03: bounded logical names."""

    @pytest.mark.parametrize(
        "name_type",
        [StreamName, ConsumerId, OperationName],
    )
    def test_valid_names_accepted(self, name_type: type) -> None:
        value = name_type("sources.events.2026-01")
        assert value.value == "sources.events.2026-01"
        assert str(value) == "sources.events.2026-01"

    @pytest.mark.parametrize(
        "name_type",
        [StreamName, ConsumerId, OperationName, ObjectKey],
    )
    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_blank_name_rejected(self, name_type: type, blank: str) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            name_type(blank)

    @pytest.mark.parametrize(
        "name_type",
        [StreamName, ConsumerId, OperationName],
    )
    @pytest.mark.parametrize(
        "control",
        ["sources\x00events", "line\nbreak", "carriage\rreturn"],
    )
    def test_control_characters_rejected(self, name_type: type, control: str) -> None:
        with pytest.raises(ValueError, match="control characters"):
            name_type(control)

    @pytest.mark.parametrize(
        "name_type",
        [StreamName, ConsumerId, OperationName],
    )
    def test_overlong_name_rejected(self, name_type: type) -> None:
        with pytest.raises(ValueError, match="exceed"):
            name_type("x" * (MAX_LOGICAL_NAME_LENGTH + 1))

    def test_same_bounded_name_is_equal_and_hash_stable(self) -> None:
        assert StreamName("events") == StreamName("events")
        assert hash(StreamName("events")) == hash(StreamName("events"))
        assert StreamName("events") != ConsumerId("events")  # type: ignore[comparison-overlap]

    def test_names_are_trimmed(self) -> None:
        assert StreamName("  events  ").value == "events"


class TestObjectKey:
    """ID-01/ID-03/ID-05: logical object-store keys."""

    def test_valid_key_accepted(self) -> None:
        key = ObjectKey("sources/2026/001/report.pdf")
        assert key.value == "sources/2026/001/report.pdf"

    def test_s3_style_key_not_special_cased(self) -> None:
        # ID-05: provider-specific URI assumptions are not required or
        # special-cased by the contract. A generic URI-looking string is a
        # normal logical key, neither rejected nor interpreted.
        key = ObjectKey("s3://bucket/path/file.bin")
        assert key.value == "s3://bucket/path/file.bin"

    def test_absolute_path_semantics_rejected(self) -> None:
        with pytest.raises(ValueError, match="absolute path"):
            ObjectKey("/etc/passwd")

    @pytest.mark.parametrize("control", ["key\x00null", "key\nnewline"])
    def test_control_characters_rejected(self, control: str) -> None:
        with pytest.raises(ValueError, match="control characters"):
            ObjectKey(control)

    def test_overlong_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceed"):
            ObjectKey("a" * (MAX_OBJECT_KEY_LENGTH + 1))

    def test_equal_keys_compare_and_hash(self) -> None:
        assert ObjectKey("a/b") == ObjectKey("a/b")
        assert hash(ObjectKey("a/b")) == hash(ObjectKey("a/b"))
