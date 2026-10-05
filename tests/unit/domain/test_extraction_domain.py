# SPDX-License-Identifier: AGPL-3.0-only
"""Domain matrix for deterministic extraction value objects (PR 11 DM1-DM10)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from darkula.domain.extraction import (
    DETERMINISTIC_ENTITY_TYPES,
    SEMANTIC_ENTITY_TYPES,
    EntityType,
    ExtractedEntity,
    ExtractionResult,
    ExtractorIdentity,
    HashSubtype,
    SourceSpan,
)
from darkula.domain.identifiers import (
    ExtractedEntityId,
    ExtractionResultId,
    NormalizedContentId,
)

_MOMENT = datetime(2026, 1, 1, tzinfo=UTC)


def _run_identity() -> ExtractorIdentity:
    return ExtractorIdentity(name="ip", version="v1")


class TestSourceSpan:
    def test_dm1_valid_span_accepted(self) -> None:
        assert SourceSpan(start=3, end=7).length == 4

    def test_dm2_negative_start_rejected(self) -> None:
        with pytest.raises(ValueError):
            SourceSpan(start=-1, end=4)

    def test_dm3_end_not_greater_than_start_rejected(self) -> None:
        with pytest.raises(ValueError):
            SourceSpan(start=4, end=4)
        with pytest.raises(ValueError):
            SourceSpan(start=4, end=3)

    def test_bool_is_not_an_integer_span(self) -> None:
        with pytest.raises(ValueError):
            SourceSpan(start=True, end=4)


class TestExtractorIdentity:
    def test_dm4_valid_identity_accepted(self) -> None:
        identity = _run_identity()
        assert identity.name == "ip"
        assert identity.version == "v1"
        assert str(identity) == "ip/v1"

    def test_dm5_blank_and_control_identity_rejected(self) -> None:
        with pytest.raises(ValueError):
            ExtractorIdentity(name="   ", version="v1")
        with pytest.raises(ValueError):
            ExtractorIdentity(name="ip", version="")
        with pytest.raises(ValueError):
            ExtractorIdentity(name="ip\n", version="v1")


class TestExtractionResult:
    def test_dm6_valid_result_accepted(self) -> None:
        result = ExtractionResult(
            result_id=ExtractionResultId.generate(),
            content_id=NormalizedContentId.generate(),
            profile_name="deterministic-observables",
            profile_version="v1",
            extractor_manifest=(_run_identity(),),
            extracted_at=_MOMENT,
            entity_count=0,
        )
        assert result.entity_count == 0

    def test_dm7_duplicate_manifest_entry_rejected(self) -> None:
        with pytest.raises(ValueError):
            ExtractionResult(
                result_id=ExtractionResultId.generate(),
                content_id=NormalizedContentId.generate(),
                profile_name="deterministic-observables",
                profile_version="v1",
                extractor_manifest=(_run_identity(), _run_identity()),
                extracted_at=_MOMENT,
                entity_count=0,
            )

    def test_dm8_invalid_entity_count_rejected(self) -> None:
        with pytest.raises(ValueError):
            ExtractionResult(
                result_id=ExtractionResultId.generate(),
                content_id=NormalizedContentId.generate(),
                profile_name="deterministic-observables",
                profile_version="v1",
                extractor_manifest=(_run_identity(),),
                extracted_at=_MOMENT,
                entity_count=-1,
            )


class TestExtractedEntity:
    def test_dm9_overlong_values_rejected(self) -> None:
        with pytest.raises(ValueError):
            ExtractedEntity(
                entity_id=ExtractedEntityId.generate(),
                extraction_result_id=ExtractionResultId.generate(),
                content_id=NormalizedContentId.generate(),
                entity_type=EntityType.DOMAIN,
                raw_value="a" * 5000,
                normalized_value="a.com",
                source_span=SourceSpan(start=0, end=1),
                extractor=_run_identity(),
            )

    def test_dm9_overlong_normalized_value_rejected(self) -> None:
        with pytest.raises(ValueError):
            ExtractedEntity(
                entity_id=ExtractedEntityId.generate(),
                extraction_result_id=ExtractionResultId.generate(),
                content_id=NormalizedContentId.generate(),
                entity_type=EntityType.DOMAIN,
                raw_value="a.com",
                normalized_value="a" * 5000,
                source_span=SourceSpan(start=0, end=5),
                extractor=_run_identity(),
            )

    def test_dm10_entity_types_are_the_finite_pr11_set(self) -> None:
        assert {
            EntityType.IP_ADDRESS,
            EntityType.DOMAIN,
            EntityType.URL,
            EntityType.EMAIL,
            EntityType.HASH,
        } == DETERMINISTIC_ENTITY_TYPES
        # PR 12 adds a disjoint finite semantic vocabulary (no OTHER).
        assert {
            EntityType.PERSON,
            EntityType.ORGANIZATION,
            EntityType.ONLINE_IDENTITY,
            EntityType.THREAT_ACTOR,
            EntityType.MALWARE,
            EntityType.LOCATION,
            EntityType.INDUSTRY,
            EntityType.ORGANIZATION_TYPE,
            EntityType.CREDENTIAL_TYPE,
            EntityType.ACCESS_TYPE,
            EntityType.CRYPTO_ADDRESS,
        } == SEMANTIC_ENTITY_TYPES
        assert DETERMINISTIC_ENTITY_TYPES.isdisjoint(SEMANTIC_ENTITY_TYPES)
        assert set(EntityType) == DETERMINISTIC_ENTITY_TYPES | SEMANTIC_ENTITY_TYPES
        assert {item.value for item in HashSubtype} == {"MD5", "SHA1", "SHA256"}
