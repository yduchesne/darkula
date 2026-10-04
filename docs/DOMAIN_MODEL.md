# Darkula Domain Model

This document defines semantic concepts, not a relational schema.

## PR 4 frozen status

PR 4 gave the following concepts their first executable form in
`src/darkula/domain/source.py` and persisted them through the PostgreSQL
adapter:

- `CandidateStatus` — `DISCOVERED`, `RECONNAISSANCE_PENDING`, `UNDER_RECONNAISSANCE`, `QUALIFIED`, `REJECTED`, `PROMOTED`.
- `CandidateEventType` — explicit event semantics (`DISCOVERED`, `RECONNAISSANCE_STARTED`, `RECONNAISSANCE_COMPLETED`, `NEEDS_MORE_RECON`, `QUALIFIED`, `REJECTED`, `PROMOTED`); event types are historical occurrences and never stand in for current status.
- `SourceStatus` — minimal managed lifecycle `ACTIVE` / `INACTIVE`.
- `EndpointType` — `ONION`, `CLEARNET`, `MIRROR`, `API`, `FEED`.
- `EndpointStatus` — minimal lifecycle `ACTIVE` / `INACTIVE` (no health semantics).
- `ReconDisposition` — `QUALIFY`, `NEEDS_MORE_RECON`, `REJECT`.
- One bounded `Confidence` representation: a float in inclusive `[0, 1]` shared by all PR 4 assessment records.

Identity rules frozen in code:

- candidate identity is a `SourceCandidateId` (UUID), never the entrypoint text;
- Source identity is a `SourceId` (UUID) and survives endpoint/mirror rotation;
- endpoint URIs are locators, not identity (no global URI->Source uniqueness is asserted);
- event/assessment IDs (`CandidateEventId`, `ReconAssessmentId`, `SourceAssessmentId`) identify immutable historical observations.

History semantics frozen in code and persistence:

- `SourceCandidateEventHistory` is append-only; production provides no update/delete.
- `ReconAssessment` and `SourceAssessment` are immutable; production provides no update/delete; `SourceAssessment` is time-windowed (`window_start <= window_end`).
- All times are timezone-aware UTC (`timestamptz`); naive datetimes are rejected.
- Structured JSON fields (`discovery_context`, `context`, `metadata`, `characteristics`) are validated JSON documents; keys and string values must not embed credentials/secrets.
- Status changes are explicit expected-state transitions, atomic with exactly one history event (see PR 4 persistence).

## PR 8 frozen status — normalization and content artifacts

PR 8 gave the content-domain concepts their first executable form in
`src/darkula/domain/content.py` and persisted structured metadata through
PostgreSQL, with artifact bytes owned by ObjectStore.

Identity rules frozen in code (section 1.4 of the PR 8 plan):

- `NormalizedContent.id` (`NormalizedContentId`) — Darkula
  semantic/persistence identity for one normalized observation;
- `ContentArtifact.id` (`ContentArtifactId`) — Darkula identity for one
  artifact reference/record;
- `ObjectKey` — a logical ObjectStore address (never identity, never
  provenance, never a hash);
- `ContentHash` — representation/change identity for exact bytes
  (sha-256);
- `source URI + observation context` — provenance, never storage identity.

Frozen semantics in code and persistence:

- `ArtifactCompleteness` (`COMPLETE`/`SAMPLE`) is explicit; a bounded
  crawler excerpt is `SAMPLE` and never the complete original body. A
  `NORMALIZED_TEXT` artifact may be `COMPLETE` only for the synthesized
  normalized representation, never the unknown original.
- `ArtifactKind` — `NORMALIZED_TEXT`, `SOURCE_BODY`, `DOWNLOAD_SAMPLE`
  (only kinds PR 8 exercises exist).
- `ContentObservation` is the bounded, Darkula-owned normalization input DTO
  (never a provider/runtime type); completeness is explicit at input.
- Normalization is deterministic, versioned (`normalization_version =
  "text-v1"`), and non-LLM: the same supported input yields the same
  canonical bytes/hash; hostile content is stored as untrusted data, never
  interpreted.
- Physical deduplication is content-addressed (`sha256/<2>/<64>` ObjectKey)
  and never merges provenance: many observations may reference one physical
  object and remain distinct immutable records.
- `ContentArtifact`/`NormalizedContent` are immutable historical records;
  a new observation for the same URI at a new time is a new row. A
  (crawl_request_id, observation_index, source_uri) unique key makes retried
  observations idempotent.

## Discovery and reconnaissance
### SourceCandidate
A discovered resource not yet accepted as a managed Source. It has identity, discovery provenance, entrypoint, timestamps, and lifecycle status.

Candidate lifecycle should preserve history through **SourceCandidateEventHistory**, including discovery, reconnaissance attempts, temporary failures, qualification/rejection, and promotion.

### ReconAssessment
An immutable assessment produced by ReconAgent for a candidate. It captures source type, accessibility, content/navigation characteristics, discovered endpoints, authentication characteristics, proposed collection strategy, relevance, confidence, evidence references, and a disposition such as QUALIFY, NEEDS_MORE_RECON, or REJECT. The Coordinator applies lifecycle decisions.

## Managed sources
### Source
The logical resource being monitored, independent of any one URL/onion address. Source identity survives endpoint/mirror rotation.

### SourceEndpoint
A first-class endpoint belonging to a Source, with URI, type (for example ONION, CLEARNET, MIRROR, API/FEED where applicable), status, and observation timestamps.

### CollectionPolicy
The durable policy describing how a Source may be collected: allowed endpoints/paths, traversal constraints, schedule, budgets, authentication reference, extraction/normalization policy, and active state. Credentials are referenced, never embedded.

### CollectionRun
A durable execution record for one collection cycle: source/policy, status/timestamps, work attempted, discovered/changed content, failures, and metrics/references.

## Crawl and content
### CrawlRequest
A bounded instruction for crawler execution: endpoint, allowed origins/paths, navigation constraints, depth/page/time budgets, authentication reference, and content-handling policy.

### CrawlResult
Execution outcome containing references to collected artifacts/content plus discovered links, redirects, failures, and execution metadata.

### ContentArtifact
Metadata/provenance for a stored artifact. Large/raw bytes live in ObjectStore. Physical content deduplication must never collapse separate observations/provenance.

### NormalizedContent
Canonical representation derived from hostile source material, including source/run provenance, source URI, media/content type, title/text when applicable, authored/observed/retrieved times, author reference, structural metadata, hashes, and artifact references. Normalized does not mean trusted.

## Extraction
### ExtractionResult
Structured facts/annotations associated with NormalizedContent. It records entities, relationships, classifications, extractor/version provenance, and completion metadata.

### ExtractedEntity
A content-derived entity with type, raw and normalized values, confidence, source span/reference, and extractor provenance. Initial families include network observables (IP/domain/URL/email/hash/crypto address), organizations/people/online identities/threat actors/malware, locations, industry/organization type, credential type, and access type. The exact ontology is TBD.

### GeographicResolution
Resolution of an extracted geographic mention, separate from extraction confidence. It supports RESOLVED, AMBIGUOUS, and UNRESOLVED outcomes plus canonical geography, country/admin/locality information, geometry when available, resolver provenance, and confidence. PostgreSQL/PostGIS geometry is a likely persistence choice but is not fixed by PR 1.

### ExtractedRelationship
A content-derived assertion linking extracted entities, with relationship type, confidence, source span/reference, and extractor provenance. It is evidence from content, not automatically global truth.

Example: an ORGANIZATION_TYPE "hospital" may have geographic_scope -> Washington State and associated_with -> compromised credentials.

## Source intelligence
### SourceAssessment
An immutable, time-windowed SourceAnalyst result. It may characterize relevance, activity, novelty, content composition, geography/industry focus, cross-source overlap, confidence, and evidence references. Assessments are historical observations; do not overwrite a mutable source.relevance field as a substitute for history.

## Semantic rules
1. Extraction produces facts/annotations; analysis produces assessments.
2. Preserve observed claims separately from resolved/canonical interpretations.
3. Preserve provenance at every stage.
4. Source identity is not endpoint identity.
5. Agent decisions are explicit outputs consumed by coordinating application logic.
6. Domain identifiers should allow correlation across collection, crawl, content, extraction, LLM/agent runs, and assessments.
