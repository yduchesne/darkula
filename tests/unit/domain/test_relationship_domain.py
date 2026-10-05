# SPDX-License-Identifier: AGPL-3.0-only
"""Relationship-assertion domain matrices (PR 13 RD1-RD14)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from darkula.domain.extraction import ExtractorIdentity, SourceSpan
from darkula.domain.identifiers import (
    ExtractedEntityId,
    ExtractedRelationshipId,
    NormalizedContentId,
    RelationshipExtractionResultId,
)
from darkula.domain.relationships import (
    RELATIONSHIP_ASSERTIONS_PROFILE_NAME,
    RELATIONSHIP_ASSERTIONS_PROFILE_VERSION,
    RELATIONSHIP_LLM_EXTRACTOR_NAME,
    RELATIONSHIP_LLM_EXTRACTOR_VERSION,
    ExtractedRelationship,
    RelationshipExtractionResult,
    RelationshipPredicate,
)

_MOMENT = datetime(2026, 5, 1, tzinfo=UTC)
_EXTRACTOR = ExtractorIdentity(
    name=RELATIONSHIP_LLM_EXTRACTOR_NAME,
    version=RELATIONSHIP_LLM_EXTRACTOR_VERSION,
)


def _result(**overrides: object) -> RelationshipExtractionResult:
    values: dict[str, object] = {
        "result_id": RelationshipExtractionResultId.generate(),
        "content_id": NormalizedContentId.generate(),
        "profile_name": RELATIONSHIP_ASSERTIONS_PROFILE_NAME,
        "profile_version": RELATIONSHIP_ASSERTIONS_PROFILE_VERSION,
        "extractor_manifest": (_EXTRACTOR,),
        "extracted_at": _MOMENT,
        "relationship_count": 0,
    }
    values.update(overrides)
    return RelationshipExtractionResult(**values)  # type: ignore[arg-type]


def _relationship(**overrides: object) -> ExtractedRelationship:
    values: dict[str, object] = {
        "relationship_id": ExtractedRelationshipId.generate(),
        "extraction_result_id": RelationshipExtractionResultId.generate(),
        "content_id": NormalizedContentId.generate(),
        "source_entity_id": ExtractedEntityId.generate(),
        "predicate": RelationshipPredicate.LOCATED_IN,
        "target_entity_id": ExtractedEntityId.generate(),
        "support_span": SourceSpan(start=0, end=5),
        "support_text": "hello",
        "extractor": _EXTRACTOR,
        "extraction_confidence": 0.5,
    }
    values.update(overrides)
    return ExtractedRelationship(**values)  # type: ignore[arg-type]


class TestRelationshipExtractionResult:
    def test_rd1_valid_result(self) -> None:
        result = _result(relationship_count=3)
        assert result.profile_name == "relationship-assertions"
        assert result.relationship_count == 3

    def test_rd2_blank_profile_rejected(self) -> None:
        with pytest.raises(ValueError):
            _result(profile_name="   ")
        with pytest.raises(ValueError):
            _result(profile_version="")

    def test_rd3_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError):
            _result(extracted_at=datetime(2026, 5, 1))

    def test_rd3_utc_timestamp_normalized(self) -> None:
        from datetime import timedelta, timezone

        result = _result(
            extracted_at=datetime(2026, 5, 1, 1, tzinfo=timezone(timedelta(hours=1)))
        )
        assert result.extracted_at.tzinfo is UTC

    def test_rd4_negative_count_rejected(self) -> None:
        with pytest.raises(ValueError):
            _result(relationship_count=-1)

    def test_rd4_non_integer_count_rejected(self) -> None:
        with pytest.raises(ValueError):
            _result(relationship_count=1.5)

    def test_rd5_empty_or_duplicate_manifest_rejected(self) -> None:
        with pytest.raises(ValueError):
            _result(extractor_manifest=())
        with pytest.raises(ValueError):
            _result(extractor_manifest=(_EXTRACTOR, _EXTRACTOR))

    def test_rd5_manifest_wrong_type_rejected(self) -> None:
        with pytest.raises(ValueError):
            _result(extractor_manifest=("x",))


class TestExtractedRelationship:
    def test_rd6_valid_assertion(self) -> None:
        assertion = _relationship()
        assert assertion.predicate is RelationshipPredicate.LOCATED_IN
        assert assertion.support_text == "hello"

    def test_rd7_span_length_must_match_support(self) -> None:
        with pytest.raises(ValueError):
            _relationship(support_span=SourceSpan(start=0, end=4))
        with pytest.raises(ValueError):
            _relationship(support_span=SourceSpan(start=0, end=0))

    def test_rd8_blank_support_rejected(self) -> None:
        with pytest.raises(ValueError):
            _relationship(support_span=SourceSpan(start=0, end=3), support_text="   ")

    def test_rd8_oversized_support_rejected(self) -> None:
        oversized = "a" * 9000
        with pytest.raises(ValueError):
            _relationship(
                support_span=SourceSpan(start=0, end=len(oversized)),
                support_text=oversized,
            )

    def test_rd9_confidence_outside_range_rejected(self) -> None:
        for value in (-0.1, 1.1):
            with pytest.raises(ValueError):
                _relationship(extraction_confidence=value)

    def test_rd10_non_finite_confidence_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                _relationship(extraction_confidence=value)

    def test_rd11_self_edge_rejected(self) -> None:
        entity_id = ExtractedEntityId.generate()
        with pytest.raises(ValueError):
            _relationship(source_entity_id=entity_id, target_entity_id=entity_id)

    def test_rd12_predicate_enum_round_trip(self) -> None:
        for predicate in RelationshipPredicate:
            assert RelationshipPredicate(predicate.value) is predicate

    def test_rd13_arbitrary_predicate_rejected(self) -> None:
        with pytest.raises(ValueError):
            _relationship(predicate="FRIENDS_WITH")
        with pytest.raises(ValueError):
            _relationship(predicate="mentions")

    def test_rd6_assertion_is_immutable(self) -> None:
        assertion = _relationship()
        with pytest.raises(FrozenInstanceError):
            assertion.support_text = "changed"  # type: ignore[misc]

    def test_rd14_identity_classes_are_distinct(self) -> None:
        from uuid import uuid4

        value = uuid4()
        result_id = RelationshipExtractionResultId(value=value)
        relationship_id = ExtractedRelationshipId(value=value)
        assert result_id != relationship_id  # type: ignore[comparison-overlap]
        assert ExtractedEntityId(value=value) != relationship_id  # type: ignore[comparison-overlap]
        assert NormalizedContentId(value=value) != result_id  # type: ignore[comparison-overlap]
