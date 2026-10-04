# SPDX-License-Identifier: AGPL-3.0-only
"""Centralized row -> domain-object mapping for the PostgreSQL adapter.

Every result-row mapping for a stored function lives here so column order
contracts are documented in exactly one place and all mapping failures are
bounded. Rows are ``tuple`` values returned by psycopg for ``SELECT * FROM
darkula.<fn>(...)`` invocations; jsonb columns arrive as native ``dict``/
``list`` values and timestamptz columns as timezone-aware ``datetime``.

Any invalid persisted value (unsupported enum label, non-UTC timestamp,
non-document jsonb, out-of-domain confidence) surfaces as a bounded
:class:`~darkula.app.persistence.MappingError`; driver values are never
echoed in messages.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from darkula.app.data_stream import StreamMessage
from darkula.app.object_store import ContentHash, ContentType
from darkula.app.persistence import MappingError
from darkula.app.repositories import OutboxRecord
from darkula.domain.content import (
    ArtifactCompleteness,
    ArtifactKind,
    ContentArtifact,
    NormalizedContent,
)
from darkula.domain.identifiers import (
    CandidateEventId,
    CausationId,
    ContentArtifactId,
    CorrelationId,
    MessageId,
    NormalizedContentId,
    ObjectKey,
    ReconAssessmentId,
    SourceAssessmentId,
    SourceCandidateId,
    SourceEndpointId,
    SourceId,
    StreamName,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    Confidence,
    EndpointStatus,
    EndpointType,
    ReconAssessment,
    ReconDisposition,
    Source,
    SourceAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
    SourceEndpoint,
    SourceStatus,
)


def _mapping_error(record: str, exc: Exception) -> MappingError:
    """Build a bounded, data-free mapping error for one record kind."""
    return MappingError(f"cannot map persisted {record}: unsupported or invalid value")


def _content_hash(algorithm: str | None, digest: str | None) -> ContentHash | None:
    """Rebuild a representation hash from persisted columns (pair)."""
    if algorithm is None and digest is None:
        return None
    return ContentHash(algorithm=str(algorithm), digest_hex=str(digest))


def map_content_artifact(row: tuple[Any, ...]) -> ContentArtifact:
    """Map one content-artifact result row.

    Column order: id, object_key, content_type, size_bytes,
    content_hash_algorithm, content_hash_digest, artifact_kind, completeness,
    created_at.
    """
    try:
        return ContentArtifact(
            artifact_id=ContentArtifactId.from_str(str(row[0])),
            object_key=ObjectKey(str(row[1])),
            content_type=None if row[2] is None else ContentType(str(row[2])),
            size_bytes=int(row[3]),
            content_hash=_content_hash(row[4], row[5]),
            artifact_kind=ArtifactKind(row[6]),
            completeness=ArtifactCompleteness(row[7]),
            created_at=row[8],
        )
    except (ValueError, TypeError) as exc:
        raise _mapping_error("content artifact", exc) from exc


def map_normalized_content(row: tuple[Any, ...]) -> NormalizedContent:
    """Map one normalized-content result row.

    Column order: content_id, artifact_id, source_uri, title, text_preview,
    content_type, observed_at, crawl_request_id, observation_index,
    normalization_version, content_hash_algorithm, content_hash_digest,
    completeness, structural_metadata, created_at.
    """
    try:
        return NormalizedContent(
            content_id=NormalizedContentId.from_str(str(row[0])),
            artifact_id=(
                None if row[1] is None else ContentArtifactId.from_str(str(row[1]))
            ),
            source_uri=str(row[2]),
            title=row[3],
            text=row[4],
            content_type=None if row[5] is None else ContentType(str(row[5])),
            observed_at=row[6],
            crawl_request_id=str(row[7]),
            observation_index=int(row[8]),
            normalization_version=str(row[9]),
            content_hash=_content_hash(row[10], row[11]),
            completeness=ArtifactCompleteness(row[12]),
            structural_metadata=row[13],
        )
    except (ValueError, TypeError) as exc:
        raise _mapping_error("normalized content", exc) from exc


def map_candidate(row: tuple[Any, ...]) -> SourceCandidate:
    """Map one ``candidate_get_v1`` result row.

    Column order: id, discovered_at, discovery_method, entrypoint, status,
    discovery_context.
    """
    try:
        return SourceCandidate(
            candidate_id=SourceCandidateId.from_str(str(row[0])),
            discovered_at=row[1],
            discovery_method=row[2],
            entrypoint=row[3],
            status=CandidateStatus(row[4]),
            discovery_context=row[5],
        )
    except (ValueError, TypeError) as exc:
        raise _mapping_error("candidate", exc) from exc


def map_event(row: tuple[Any, ...]) -> SourceCandidateEventHistory:
    """Map one ``candidate_history_list_v1`` result row.

    Column order: event_id, candidate_id, event_type, occurred_at, context,
    reason, provenance.
    """
    try:
        return SourceCandidateEventHistory(
            event_id=CandidateEventId.from_str(str(row[0])),
            candidate_id=SourceCandidateId.from_str(str(row[1])),
            event_type=CandidateEventType(row[2]),
            occurred_at=row[3],
            context=row[4],
            reason=row[5],
            provenance=row[6],
        )
    except (ValueError, TypeError) as exc:
        raise _mapping_error("candidate event", exc) from exc


def map_source(row: tuple[Any, ...]) -> Source:
    """Map one ``source_get_v1`` result row.

    Column order: id, status, created_at, updated_at, name.
    """
    try:
        return Source(
            source_id=SourceId.from_str(str(row[0])),
            status=SourceStatus(row[1]),
            created_at=row[2],
            updated_at=row[3],
            name=row[4],
        )
    except (ValueError, TypeError) as exc:
        raise _mapping_error("source", exc) from exc


def map_endpoint(row: tuple[Any, ...]) -> SourceEndpoint:
    """Map one ``source_endpoint_list_v1`` result row.

    Column order: endpoint_id, source_id, uri, endpoint_type, status,
    first_observed_at, last_observed_at, metadata.
    """
    try:
        return SourceEndpoint(
            endpoint_id=SourceEndpointId.from_str(str(row[0])),
            source_id=SourceId.from_str(str(row[1])),
            uri=row[2],
            endpoint_type=EndpointType(row[3]),
            status=EndpointStatus(row[4]),
            first_observed_at=row[5],
            last_observed_at=row[6],
            metadata=row[7],
        )
    except (ValueError, TypeError) as exc:
        raise _mapping_error("source endpoint", exc) from exc


def map_recon_assessment(row: tuple[Any, ...]) -> ReconAssessment:
    """Map one ``recon_assessment_list_v1`` result row.

    Column order: assessment_id, candidate_id, assessed_at, disposition,
    confidence, evidence_references, characteristics.
    """
    try:
        return ReconAssessment(
            assessment_id=ReconAssessmentId.from_str(str(row[0])),
            candidate_id=SourceCandidateId.from_str(str(row[1])),
            assessed_at=row[2],
            disposition=ReconDisposition(row[3]),
            confidence=Confidence(row[4]),
            evidence_references=tuple(row[5]),
            characteristics=row[6],
        )
    except (ValueError, TypeError) as exc:
        raise _mapping_error("recon assessment", exc) from exc


def map_source_assessment(row: tuple[Any, ...]) -> SourceAssessment:
    """Map one ``source_assessment_list_v1`` result row.

    Column order: assessment_id, source_id, assessed_at, window_start,
    window_end, confidence, relevance, activity, novelty,
    evidence_references, characteristics.
    """
    try:
        return SourceAssessment(
            assessment_id=SourceAssessmentId.from_str(str(row[0])),
            source_id=SourceId.from_str(str(row[1])),
            assessed_at=row[2],
            window_start=row[3],
            window_end=row[4],
            confidence=Confidence(row[5]),
            relevance=None if row[6] is None else Confidence(row[6]),
            activity=None if row[7] is None else Confidence(row[7]),
            novelty=None if row[8] is None else Confidence(row[8]),
            evidence_references=tuple(row[9]),
            characteristics=row[10],
        )
    except (ValueError, TypeError) as exc:
        raise _mapping_error("source assessment", exc) from exc


def map_outbox_record(row: tuple[Any, ...]) -> OutboxRecord:
    """Map one ``outbox_claim_v1`` result row.

    Column order: outbox_id, message_id, stream_name, message_type,
    schema_version, occurred_at, payload, correlation_id, causation_id,
    routing_key.
    """
    try:
        return OutboxRecord(
            outbox_id=row[0],
            stream_name=StreamName(str(row[2])),
            message=StreamMessage(
                message_id=MessageId.from_str(str(row[1])),
                message_type=row[3],
                schema_version=int(row[4]),
                occurred_at=row[5].astimezone(UTC),
                payload=row[6],
                correlation_id=(
                    None if row[7] is None else CorrelationId.from_str(str(row[7]))
                ),
                causation_id=(
                    None if row[8] is None else CausationId.from_str(str(row[8]))
                ),
                routing_key=row[9],
            ),
        )
    except (ValueError, TypeError) as exc:
        raise _mapping_error("outbox record", exc) from exc


__all__ = [
    "map_candidate",
    "map_content_artifact",
    "map_endpoint",
    "map_event",
    "map_normalized_content",
    "map_outbox_record",
    "map_recon_assessment",
    "map_source",
    "map_source_assessment",
]
