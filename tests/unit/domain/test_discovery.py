# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic domain tests for the PR 17 discovery foundation (U01-U18).

These tests exercise only the pure domain contract: bounded validation,
conservative locator normalization, stable replay keys, access-evidence
ownership, and the immutable qualification-decision rules. No database,
network, DNS, or model is involved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from darkula.domain.discovery import (
    MAX_DISCOVERY_REFERENCE_LENGTH,
    AccessEvidence,
    AccessStatus,
    DiscoveryDetectionMethod,
    DiscoveryInput,
    DiscoveryObservation,
    DiscoveryReferenceKind,
    DiscoveryValidationStatus,
    QualificationDecision,
    QualificationMethod,
    QualificationOutcome,
    classify_discovery_reference,
    discovery_semantic_key,
    normalize_discovery_locator,
    observation_from_input,
    qualification_decision_key,
    qualification_event_type,
    qualification_new_status,
)
from darkula.domain.identifiers import (
    AccessEvidenceId,
    CandidateEventId,
    DiscoveryObservationId,
    QualificationDecisionId,
    ReconAssessmentId,
    SourceCandidateId,
    SourceEndpointId,
)
from darkula.domain.source import CandidateEventType, CandidateStatus, EndpointType

OBSERVED_AT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
INGESTED_AT = datetime(2026, 11, 11, 12, 5, tzinfo=UTC)


def locator_input(url: str = "https://example.com/") -> DiscoveryInput:
    """Return a valid untrusted registry LOCATOR observation input."""
    return DiscoveryInput(
        provider_id="ransomlook",
        provider_record_id="record-1",
        provider_record_version="v1",
        detection_method=DiscoveryDetectionMethod.REGISTRY,
        reference_kind=DiscoveryReferenceKind.LOCATOR,
        asserted_reference=url,
        observed_at=OBSERVED_AT,
        evidence_reference="registry://ransomlook/records/record-1",
        requires_auth_claim=None,
    )


def name_only_input(name: str = "Example Forum") -> DiscoveryInput:
    """Return a valid NAME_ONLY observation input (no locator)."""
    return DiscoveryInput(
        provider_id="manual",
        provider_record_id="note-1",
        provider_record_version="1",
        detection_method=DiscoveryDetectionMethod.MANUAL,
        reference_kind=DiscoveryReferenceKind.NAME_ONLY,
        asserted_reference=name,
        observed_at=OBSERVED_AT,
        evidence_reference="operator://note/note-1",
    )


def qualification_decision(**overrides: object) -> QualificationDecision:
    """Build one valid REGISTRY_POLICY qualification decision (PR 19 shape)."""
    candidate_id = SourceCandidateId.generate()
    evidence = (DiscoveryObservationId.generate(),)
    policy_id = "registry-trust"
    policy_version = "v1"
    values: dict[str, object] = {
        "decision_id": QualificationDecisionId.generate(),
        "candidate_id": candidate_id,
        "method": QualificationMethod.REGISTRY_POLICY,
        "outcome": QualificationOutcome.QUALIFIED,
        "evaluated_at": OBSERVED_AT,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "provider_id": "ransomlook",
        "provider_record_id": "record-1",
        "evidence_observation_ids": evidence,
        "recon_assessment_id": None,
        "event_id": CandidateEventId.generate(),
        "expected_status": CandidateStatus.DISCOVERED,
        "new_status": CandidateStatus.QUALIFIED,
        "decision_key": qualification_decision_key(
            candidate_id=candidate_id,
            method=QualificationMethod.REGISTRY_POLICY,
            policy_id=policy_id,
            policy_version=policy_version,
            evidence_observation_ids=evidence,
            recon_assessment_id=None,
        ),
    }
    values.update(overrides)
    return QualificationDecision(**values)  # type: ignore[arg-type]


class TestLocatorNormalization:
    """U01/U02/U03: deterministic, conservative, idempotent normalization."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("HTTP://Example.COM:80", "http://example.com/"),
            ("http://example.com", "http://example.com/"),
            ("https://Example.com:443/a/b?z=1&a=2", "https://example.com/a/b?z=1&a=2"),
            (
                "http://example.com:8080/Path?Q=Value",
                "http://example.com:8080/Path?Q=Value",
            ),
            ("http://[::1]:8080/a", "http://[::1]:8080/a"),
            ("http://example.com/%7Euser", "http://example.com/%7Euser"),
            ("http://bücher.example/", "http://xn--bcher-kva.example/"),
            ("http://10.0.0.5/private", "http://10.0.0.5/private"),
        ],
    )
    def test_normalizes_syntactically(self, raw: str, expected: str) -> None:
        assert normalize_discovery_locator(raw) == expected

    def test_idempotent(self) -> None:
        once = normalize_discovery_locator("HTTPS://Example.COM:443/a//b/?x=1")
        assert normalize_discovery_locator(once) == once

    def test_onion_host_accepted_without_network(self) -> None:
        normalized = normalize_discovery_locator("http://3g2upl4pq6kufc4m.onion/")
        assert normalized == "http://3g2upl4pq6kufc4m.onion/"

    @pytest.mark.parametrize(
        "raw",
        [
            "ftp://example.com/",
            "ws://example.com/",
            "file:///tmp/x",
            "javascript:alert(1)",
            "data:text/plain,hello",
            "mailto:someone@example.com",
            "//example.com/",
            "example.com",
            "http://user:pass@example.com/",
            "http://user@example.com/",
            "http://example.com/#fragment",
            "http://example.com/a b",
            " http://example.com/",
            "http://example.com/\x00",
            "http://example.com/a\\b",
            "http://example.com/%2e%2e/etc",
            "http://example.com/%2E%2E/etc",
            "http://example.com/a%2fb",
            "http://example.com/a%5Cb",
            "http://example.com/?q=%00",
            "http://[fe80::1%25eth0]/",
            "http://example.com:0/",
            "http://example.com:99999/",
            "http://example.com:/",
            "http://example.com./",
            "http://999.1.1.1/",
            "http://exa_mple.com/",
            "http://example.com/?token=abc",
            "http://example.com/?api_key=abc",
            "",
        ],
    )
    def test_rejects_instead_of_repairing(self, raw: str) -> None:
        with pytest.raises(ValueError):
            normalize_discovery_locator(raw)


class TestReferenceClassification:
    """U06/U07: NAME_ONLY never becomes a synthetic locator."""

    def test_name_only_has_no_locator(self) -> None:
        source = name_only_input("Example Forum")
        locator, status = classify_discovery_reference(source)
        assert locator is None
        assert status is DiscoveryValidationStatus.UNRESOLVED_NAME

    @pytest.mark.parametrize(
        "value", ["https://example.com", "ftp://example.com", "http://x.onion"]
    )
    def test_name_only_url_rejected(self, value: str) -> None:
        with pytest.raises(ValueError):
            name_only_input(value)

    def test_name_only_requires_nonblank(self) -> None:
        with pytest.raises(ValueError):
            name_only_input("   ")

    def test_locator_classification(self) -> None:
        locator, status = classify_discovery_reference(
            locator_input("https://Example.com:443/a")
        )
        assert locator == "https://example.com/a"
        assert status is DiscoveryValidationStatus.VALID_LOCATOR


class TestTimestampsAndBounds:
    """U08/U09/U10: UTC canonicalization and fail-closed bounds."""

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError):
            DiscoveryInput(
                provider_id="ransomlook",
                provider_record_id="r1",
                provider_record_version="v1",
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=DiscoveryReferenceKind.LOCATOR,
                asserted_reference="https://example.com/",
                observed_at=datetime(2026, 11, 11, 12, 0),
                evidence_reference="registry://ransomlook/r1",
            )

    def test_aware_non_utc_normalized(self) -> None:
        source = DiscoveryInput(
            provider_id="ransomlook",
            provider_record_id="r1",
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
            asserted_reference="https://example.com/",
            observed_at=datetime(
                2026, 11, 11, 14, 0, tzinfo=timezone(timedelta(hours=2))
            ),
            evidence_reference="registry://ransomlook/r1",
        )
        assert source.observed_at == OBSERVED_AT
        assert source.observed_at.tzinfo is UTC

    @pytest.mark.parametrize(
        "reference",
        [
            "registry://provider/record?password=hunter2",
            "registry://provider/record#token=abc",
            "registry://provider/auth/record",
            "registry://provider/session/record",
            "registry://provider/" + "a" * MAX_DISCOVERY_REFERENCE_LENGTH,
        ],
    )
    def test_secret_like_or_oversized_reference_rejected(self, reference: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            DiscoveryInput(
                provider_id="ransomlook",
                provider_record_id="r1",
                provider_record_version="v1",
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=DiscoveryReferenceKind.LOCATOR,
                asserted_reference="https://example.com/",
                observed_at=OBSERVED_AT,
                evidence_reference=reference,
            )
        assert "hunter2" not in str(excinfo.value)
        assert "token=abc" not in str(excinfo.value)

    def test_provider_id_uppercase_rejected(self) -> None:
        with pytest.raises(ValueError):
            DiscoveryInput(
                provider_id="RansomLook",
                provider_record_id="r1",
                provider_record_version="v1",
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=DiscoveryReferenceKind.LOCATOR,
                asserted_reference="https://example.com/",
                observed_at=OBSERVED_AT,
                evidence_reference="registry://ransomlook/r1",
            )

    def test_provider_record_identity_not_trimmed(self) -> None:
        with pytest.raises(ValueError):
            DiscoveryInput(
                provider_id="ransomlook",
                provider_record_id=" r1",
                provider_record_version="v1",
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=DiscoveryReferenceKind.LOCATOR,
                asserted_reference="https://example.com/",
                observed_at=OBSERVED_AT,
                evidence_reference="registry://ransomlook/r1",
            )

    def test_requires_auth_claim_must_be_bool(self) -> None:
        with pytest.raises(ValueError):
            DiscoveryInput(
                provider_id="ransomlook",
                provider_record_id="r1",
                provider_record_version="v1",
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=DiscoveryReferenceKind.LOCATOR,
                asserted_reference="https://example.com/",
                observed_at=OBSERVED_AT,
                evidence_reference="registry://ransomlook/r1",
                requires_auth_claim=1,  # type: ignore[arg-type]
            )


class TestSemanticKey:
    """U11/U12: stable length-prefixed replay identity."""

    def test_known_stable_digest(self) -> None:
        # Hard-coded across processes: 4-byte big-endian length prefix per
        # component over the frozen tuple order.
        assert (
            discovery_semantic_key(
                provider_id="ransomlook",
                provider_record_id="rec-1",
                provider_record_version="v1",
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=DiscoveryReferenceKind.LOCATOR,
            )
            == "124a94b157fc1637c8343ea806e17e574aae87dbc4f84785fbaff4e33da12846"
        )

    def test_tuple_components_are_length_prefixed(self) -> None:
        first = discovery_semantic_key(
            provider_id="ab",
            provider_record_id="c",
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
        )
        second = discovery_semantic_key(
            provider_id="a",
            provider_record_id="bc",
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
        )
        assert first != second

    def test_distinct_record_versions_and_providers(self) -> None:
        base = discovery_semantic_key(
            provider_id="ransomlook",
            provider_record_id="rec-1",
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
        )
        new_version = discovery_semantic_key(
            provider_id="ransomlook",
            provider_record_id="rec-1",
            provider_record_version="v2",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
        )
        other_provider = discovery_semantic_key(
            provider_id="other",
            provider_record_id="rec-1",
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
        )
        assert base not in (new_version, other_provider)
        assert new_version != other_provider

    def test_equal_locators_from_different_records_are_distinct(self) -> None:
        first = observation_from_input(
            locator_input("https://example.com/"),
            observation_id=DiscoveryObservationId.generate(),
            ingested_at=INGESTED_AT,
        )
        other_input = DiscoveryInput(
            provider_id="other-provider",
            provider_record_id="record-9",
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
            asserted_reference="https://example.com/",
            observed_at=OBSERVED_AT,
            evidence_reference="registry://other/record-9",
        )
        second = observation_from_input(
            other_input,
            observation_id=DiscoveryObservationId.generate(),
            ingested_at=INGESTED_AT,
        )
        assert first.normalized_locator == second.normalized_locator
        assert first.semantic_key != second.semantic_key
        assert first.observation_id != second.observation_id


class TestObservation:
    """U13/U14/U19: immutable observation semantics, no authorization."""

    def test_observation_round_trips_input_fields(self) -> None:
        source = locator_input("https://Example.com:443/a")
        observation = observation_from_input(
            source,
            observation_id=DiscoveryObservationId.generate(),
            ingested_at=INGESTED_AT,
        )
        assert observation.normalized_locator == "https://example.com/a"
        assert observation.validation_status is DiscoveryValidationStatus.VALID_LOCATOR
        assert observation.ingested_at == INGESTED_AT
        assert observation.candidate_id is None
        assert observation.semantic_key == discovery_semantic_key(
            provider_id=source.provider_id,
            provider_record_id=source.provider_record_id,
            provider_record_version=source.provider_record_version,
            detection_method=source.detection_method,
            reference_kind=source.reference_kind,
        )

    def test_same_key_different_payload_is_not_the_same_observation(self) -> None:
        source = locator_input()
        first = observation_from_input(
            source,
            observation_id=DiscoveryObservationId.generate(),
            ingested_at=INGESTED_AT,
        )
        later = observation_from_input(
            locator_input(),
            observation_id=DiscoveryObservationId.generate(),
            ingested_at=INGESTED_AT + timedelta(hours=1),
        )
        assert first.semantic_key == later.semantic_key
        assert first.ingested_at != later.ingested_at

    def test_mismatched_normalized_locator_rejected(self) -> None:
        with pytest.raises(ValueError):
            DiscoveryObservation(
                observation_id=DiscoveryObservationId.generate(),
                provider_id="ransomlook",
                provider_record_id="record-1",
                provider_record_version="v1",
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=DiscoveryReferenceKind.LOCATOR,
                asserted_reference="https://example.com/",
                normalized_locator="http://example.com/",
                validation_status=DiscoveryValidationStatus.VALID_LOCATOR,
                observed_at=OBSERVED_AT,
                ingested_at=INGESTED_AT,
                evidence_reference="registry://ransomlook/record-1",
                semantic_key=discovery_semantic_key(
                    provider_id="ransomlook",
                    provider_record_id="record-1",
                    provider_record_version="v1",
                    detection_method=DiscoveryDetectionMethod.REGISTRY,
                    reference_kind=DiscoveryReferenceKind.LOCATOR,
                ),
            )

    def test_auth_claim_stays_an_untrusted_boolean(self) -> None:
        source = DiscoveryInput(
            provider_id="ransomlook",
            provider_record_id="record-1",
            provider_record_version="v1",
            detection_method=DiscoveryDetectionMethod.REGISTRY,
            reference_kind=DiscoveryReferenceKind.LOCATOR,
            asserted_reference="https://example.com/",
            observed_at=OBSERVED_AT,
            evidence_reference="registry://ransomlook/record-1",
            requires_auth_claim=True,
            source_kind_hint=EndpointType.ONION,
        )
        observation = observation_from_input(
            source,
            observation_id=DiscoveryObservationId.generate(),
            ingested_at=INGESTED_AT,
        )
        assert observation.requires_auth_claim is True
        assert observation.source_kind_hint is EndpointType.ONION
        assert observation.candidate_id is None


class TestAccessEvidence:
    """Access evidence ownership is exactly-one-target and immutable."""

    def test_candidate_evidence(self) -> None:
        evidence = AccessEvidence(
            access_evidence_id=AccessEvidenceId.generate(),
            observation_id=DiscoveryObservationId.generate(),
            candidate_id=SourceCandidateId.generate(),
            source_endpoint_id=None,
            status=AccessStatus.CREDENTIALS_PENDING,
            reason_code="PROVIDER_AUTH_CLAIM",
            recorded_at=OBSERVED_AT,
            evidence_reference="registry://ransomlook/record-1",
        )
        assert evidence.status is AccessStatus.CREDENTIALS_PENDING

    def test_endpoint_evidence(self) -> None:
        evidence = AccessEvidence(
            access_evidence_id=AccessEvidenceId.generate(),
            observation_id=DiscoveryObservationId.generate(),
            candidate_id=None,
            source_endpoint_id=SourceEndpointId.generate(),
            status=AccessStatus.UNKNOWN,
            reason_code="OBSERVED",
            recorded_at=OBSERVED_AT,
            evidence_reference="registry://ransomlook/record-1",
        )
        assert evidence.source_endpoint_id is not None
        assert evidence.candidate_id is None

    def test_no_target_rejected(self) -> None:
        with pytest.raises(ValueError):
            AccessEvidence(
                access_evidence_id=AccessEvidenceId.generate(),
                observation_id=DiscoveryObservationId.generate(),
                candidate_id=None,
                source_endpoint_id=None,
                status=AccessStatus.UNKNOWN,
                reason_code="OBSERVED",
                recorded_at=OBSERVED_AT,
                evidence_reference="registry://ransomlook/record-1",
            )

    def test_both_targets_rejected(self) -> None:
        with pytest.raises(ValueError):
            AccessEvidence(
                access_evidence_id=AccessEvidenceId.generate(),
                observation_id=DiscoveryObservationId.generate(),
                candidate_id=SourceCandidateId.generate(),
                source_endpoint_id=SourceEndpointId.generate(),
                status=AccessStatus.CREDENTIALS_PENDING,
                reason_code="PROVIDER_AUTH_CLAIM",
                recorded_at=OBSERVED_AT,
                evidence_reference="registry://ransomlook/record-1",
            )


class TestQualificationDecision:
    """U15-U18: immutable, provenance-checked decision rules."""

    def test_registry_decision_without_recon(self) -> None:
        decision = qualification_decision()
        assert decision.recon_assessment_id is None
        assert decision.new_status is CandidateStatus.QUALIFIED
        assert qualification_new_status(decision.outcome) is CandidateStatus.QUALIFIED
        assert (
            qualification_event_type(decision.outcome) is CandidateEventType.QUALIFIED
        )

    def test_registry_decision_with_recon_rejected(self) -> None:
        with pytest.raises(ValueError):
            qualification_decision(recon_assessment_id=ReconAssessmentId.generate())

    def test_agent_decision_requires_real_recon(self) -> None:
        candidate_id = SourceCandidateId.generate()
        evidence = (DiscoveryObservationId.generate(),)
        with pytest.raises(ValueError):
            qualification_decision(
                method=QualificationMethod.AGENT_RECON,
                candidate_id=candidate_id,
                evidence_observation_ids=evidence,
                provider_id=None,
                provider_record_id=None,
                recon_assessment_id=None,
                decision_key=qualification_decision_key(
                    candidate_id=candidate_id,
                    method=QualificationMethod.AGENT_RECON,
                    policy_id="registry-trust",
                    policy_version="v1",
                    evidence_observation_ids=evidence,
                    recon_assessment_id=None,
                ),
            )

    def test_agent_decision_with_recon(self) -> None:
        candidate_id = SourceCandidateId.generate()
        evidence = (DiscoveryObservationId.generate(),)
        recon_id = ReconAssessmentId.generate()
        decision = qualification_decision(
            method=QualificationMethod.AGENT_RECON,
            candidate_id=candidate_id,
            evidence_observation_ids=evidence,
            provider_id=None,
            provider_record_id=None,
            recon_assessment_id=recon_id,
            decision_key=qualification_decision_key(
                candidate_id=candidate_id,
                method=QualificationMethod.AGENT_RECON,
                policy_id="registry-trust",
                policy_version="v1",
                evidence_observation_ids=evidence,
                recon_assessment_id=recon_id,
            ),
        )
        assert decision.recon_assessment_id == recon_id

    def test_empty_evidence_rejected(self) -> None:
        with pytest.raises(ValueError):
            qualification_decision(evidence_observation_ids=())

    def test_duplicate_evidence_rejected(self) -> None:
        duplicate = DiscoveryObservationId.generate()
        with pytest.raises(ValueError):
            qualification_decision(evidence_observation_ids=(duplicate, duplicate))

    def test_mismatched_outcome_status_rejected(self) -> None:
        with pytest.raises(ValueError):
            qualification_decision(new_status=CandidateStatus.REJECTED)

    def test_mismatched_event_type_rejected(self) -> None:
        # The event type is validated by the repository primitive against the
        # frozen mapping; verify the mapping itself here.
        assert (
            qualification_event_type(QualificationOutcome.RECONNAISSANCE_PENDING)
            is CandidateEventType.NEEDS_MORE_RECON
        )
        assert (
            qualification_event_type(QualificationOutcome.REJECTED)
            is CandidateEventType.REJECTED
        )
        assert (
            qualification_new_status(QualificationOutcome.RECONNAISSANCE_PENDING)
            is CandidateStatus.RECONNAISSANCE_PENDING
        )

    def test_decision_key_is_order_insensitive(self) -> None:
        candidate_id = SourceCandidateId.generate()
        first = DiscoveryObservationId.generate()
        second = DiscoveryObservationId.generate()
        assert qualification_decision_key(
            candidate_id=candidate_id,
            method=QualificationMethod.REGISTRY_POLICY,
            policy_id="registry-trust",
            policy_version="v1",
            evidence_observation_ids=(first, second),
            recon_assessment_id=None,
        ) == qualification_decision_key(
            candidate_id=candidate_id,
            method=QualificationMethod.REGISTRY_POLICY,
            policy_id="registry-trust",
            policy_version="v1",
            evidence_observation_ids=(second, first),
            recon_assessment_id=None,
        )

    def test_decision_key_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError):
            qualification_decision(decision_key="0" * 64)

    def test_policy_identity_must_be_ascii_name(self) -> None:
        with pytest.raises(ValueError):
            qualification_decision(policy_id="registry trust")

    def test_expected_status_must_differ(self) -> None:
        with pytest.raises(ValueError):
            qualification_decision(expected_status=CandidateStatus.QUALIFIED)
