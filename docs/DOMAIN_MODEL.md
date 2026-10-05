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

## PR 11 frozen status — deterministic extraction

PR 11 gave the deterministic extraction concepts their first executable form
in `src/darkula/domain/extraction.py` and persisted them through the
PostgreSQL adapter. Extraction converts one persisted canonical normalized
representation into bounded, provenance-bearing structured observations; it
is **not** analysis and performs no LLM/semantic/geographic/relationship
reasoning.

Identity/version rules frozen in code:

- `ExtractionResultId` — persisted result identity; the semantic/idempotency
  key is `(content_id, profile_name, profile_version)` and is UNIQUE, never a
  generated UUID, an `ObjectKey`, a content hash, or a broker position;
- `ExtractorIdentity` — bounded developer-controlled `name` + `version`
  (for example `ip/v1`), logical extractor identity, never a value;
- `ExtractedEntityId` — one extracted **occurrence**; a value at two spans is
  two occurrence records;
- `SourceSpan` — half-open `[start, end)` code-point offsets into exact
  canonical text, with `0 <= start < end` and `text[start:end] == raw_value`;
- `type + normalized_value` is a semantic value, never occurrence identity.

Semantics frozen in code and persistence:

- `EntityType` is the finite PR 11 vocabulary `IP_ADDRESS`, `DOMAIN`, `URL`,
  `EMAIL`, `HASH`; PR 12 owns semantic/geographic types. `HashSubtype`
  (`MD5`/`SHA1`/`SHA256`) is the only current bounded subtype.
- `ExtractionResult.extractor_manifest` is the exact historical manifest, and
  `entity_count` equals the number of persisted occurrences.
- Results/occurrences are append-only/immutable. An incompatible extractor
  change requires a new profile/extractor version and a new result.
- The same value in two content observations is two provenance-bearing
  occurrences; extracted facts are **not** global truth and assert no
  maliciousness, ownership, victimhood, identity equivalence, or relationship.
- `SourceSpan` is valid only against the exact persisted canonical text; the
  DB preview is never an extraction source.

## PR 12 frozen status — semantic extraction and geographic resolution

PR 12 extends the extraction domain with a finite semantic vocabulary and a
separate geographic-resolution lifecycle.

- `EntityType` now holds a **disjoint** PR 11 syntactic set
  (`DETERMINISTIC_ENTITY_TYPES`) and PR 12 semantic set
  (`SEMANTIC_ENTITY_TYPES`): `PERSON`, `ORGANIZATION`, `ONLINE_IDENTITY`,
  `THREAT_ACTOR`, `MALWARE`, `LOCATION`, `INDUSTRY`, `ORGANIZATION_TYPE`,
  `CREDENTIAL_TYPE`, `ACCESS_TYPE`, `CRYPTO_ADDRESS`. There is deliberately
  no `OTHER`/catch-all.
- `ExtractedEntity.extraction_confidence` is `None` for deterministic
  occurrences (no fabricated probabilistic confidence) and a finite `[0, 1]`
  value for model-backed semantic occurrences. It is **not** geographic
  resolution confidence.
- Semantic extraction is the fixed `semantic-entities/v1` profile with the
  logical `semantic-llm/v1` extractor and uses the existing `LlmClient`.
- `GeographicResolution` (`src/darkula/domain/geography.py`) is an immutable
  observation of how one `LOCATION` occurrence resolves: identity
  `GeographicResolutionId`, the resolved `ExtractedEntityId`, status
  (`RESOLVED`/`AMBIGUOUS`/`UNRESOLVED`), `GeographicResolverIdentity`
  (name/version), resolved timestamp, canonical name/country/administrative
  area/locality, WGS84 latitude/longitude, its **own** confidence, and a
  bounded provider-neutral reference. Identity stays distinct from resolved
  attributes.
- Status invariants are enforced in code: `RESOLVED` requires a canonical
  name and confidence; `AMBIGUOUS`/`UNRESOLVED` must not carry canonical
  fields or geometry. A resolution is append-only and versioned by
  `(extracted_entity_id, resolver_name, resolver_version)`; a new resolver
  version coexists and never rewrites history.
- `LOCATION` mention extraction never requires successful resolution, and a
  resolution never mutates the mention's raw/normalized value, span, or
  extraction confidence.

## PR 13 frozen status — content-derived relationship assertions

PR 13 records what **one normalized content observation asserted** between two
persisted extracted-entity occurrences. A relationship is an assertion about
content, never global truth.

- `src/darkula/domain/relationships.py` owns the finite
  `RelationshipPredicate` vocabulary (`AFFILIATED_WITH`, `USES`, `OPERATES`,
  `TARGETS`, `IMPERSONATES`, `SELLS`, `OFFERS_ACCESS_TO`, `HAS_ACCESS_TO`,
  `LOCATED_IN`, `AFFECTS`), `RelationshipExtractionResult`, and
  `ExtractedRelationship`. There is no arbitrary free-text predicate and no
  ATT&CK/STIX/MISP/OpenCTI semantics.
- `RelationshipExtractionResult` is immutable/versioned under the fixed
  `relationship-assertions/v1` profile with the logical
  `relationship-llm/v1` extractor; the semantic/idempotency key is
  `(content_id, profile_name, profile_version)` and is UNIQUE. A new profile
  version coexists rather than rewriting history.
- Endpoints are `source_entity_id`/`target_entity_id`
  (`ExtractedEntityId` **occurrences**), never endpoint values, normalized
  values, global entity ids, or `GeographicResolutionId`s. Both endpoint
  occurrences must belong to the relationship content; direction is explicit
  and self-edges are rejected.
- `support_span` is exact canonical provenance: `text[start:end] ==
  support_text`, the span length equals the text length, and the span contains
  both endpoint occurrence spans. The model never chooses offsets; trusted code
  grounds the model-copied support text.
- `extraction_confidence` is the confidence that the content asserted the
  relationship — never objective truth, source credibility, endpoint
  extraction confidence, geographic resolution confidence, or a threat score.
- Assertions are append-only/immutable. Identical semantics in two content
  observations, or at two occurrence spans, remain separate provenance-bearing
  assertions; there is no global merged relationship entity.

## Discovery and reconnaissance
### SourceCandidate
A discovered resource not yet accepted as a managed Source. It has identity, discovery provenance, entrypoint, timestamps, and lifecycle status.

Candidate lifecycle should preserve history through **SourceCandidateEventHistory**, including discovery, reconnaissance attempts, temporary failures, qualification/rejection, and promotion.

### ReconAssessment
An immutable assessment produced by ReconAgent for a candidate. It captures source type, accessibility, content/navigation characteristics, discovered endpoints, authentication characteristics, proposed collection strategy, relevance, confidence, evidence references, and a disposition such as QUALIFY, NEEDS_MORE_RECON, or REJECT. The Coordinator applies lifecycle decisions.

**PR 10 semantics (delivered).** A `ReconAssessment` is a **recommendation**, never a lifecycle decision: the `ReconCoordinator` owns every candidate status transition and event append. A `QUALIFY` disposition transitions the candidate to `QUALIFIED` (a distinct state from `PROMOTED`; qualification never creates a `Source`, a `SourceEndpoint`, or a `CollectionPolicy`). `NEEDS_MORE_RECON` transitions to `RECONNAISSANCE_PENDING` and `REJECT` to `REJECTED`. Assessments are append-only and multiple assessments per candidate are normal: each reconnaissance execution that reaches an analytical conclusion appends exactly one immutable assessment, and a later execution never rewrites earlier history.

Evidence references inside an assessment are **trusted provenance**: they point to bounded observations actually presented to the model during that execution, and unknown references fail closed (no assessment is persisted). Only structured application-defined outputs are persisted; hidden chain-of-thought is never requested or stored. Operational failures (LLM/crawler/protocol/budget) are **not** analytical dispositions: they never append an assessment and return the candidate to `RECONNAISSANCE_PENDING` with a `NEEDS_MORE_RECON` event so a later execution may retry.

## Managed sources
### Source
The logical resource being monitored, independent of any one URL/onion address. Source identity survives endpoint/mirror rotation.

### SourceEndpoint
A first-class endpoint belonging to a Source, with URI, type (for example ONION, CLEARNET, MIRROR, API/FEED where applicable), status, and observation timestamps.

### CollectionPolicy (PR 9)
The durable, reusable authorization/configuration describing how a managed Source may be collected. It belongs to a Source (never to a URI) and authorizes managed `SourceEndpoint` identities: active flag, positive bounded `interval_seconds`, explicit UTC `next_due_at` occurrence clock, `allowed_endpoint_ids` (managed identities only, never URLs), `allowed_paths`, budgets that mirror the PR 7 `CrawlRequest` bounds (max pages/requests/depth/timeout), an optional `authentication_reference` (a **reference only**, never a secret value; PR 9 delivers unauthenticated crawling), and a monotonic `revision` counter. Each edit bumps the revision; historical runs keep the exact revision plus their frozen `policy_snapshot`, so policy edits never rewrite run history.

### CollectionRun (PR 9)
A durable **historical execution record** of one policy occurrence. The occurrence identity is `(policy_id, scheduled_for)` and is UNIQUE — concurrent schedulers can never admit the same occurrence twice. The run records: the policy's `policy_revision` and `policy_snapshot` (the frozen execution contract), source, `scheduled_for`, timestamps (`created_at`/`started_at`/`completed_at`), the finite lifecycle status (`QUEUED -> RUNNING -> SUCCEEDED/FAILED/CANCELLED`), execution lease fields, `attempt_count`, and the monotonic counters (`crawl_requests_attempted`, `pages_observed`, `content_observations`, `content_created`, `content_deduplicated`). Failures carry a bounded sanitized `failure_code`/`failure_summary`. Runs are immutable historical executions; a later schedule creates a new run.

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
Structured facts/annotations associated with NormalizedContent, with
result/profile identity, the exact historical extractor manifest,
extraction time, and occurrence count. **PR 11 delivered**: deterministic
observable extraction under the fixed `deterministic-observables/v1` profile,
immutable/versioned and idempotent on
`(content_id, profile_name, profile_version)`. PR 12 may add
`semantic-geographic/<version>` as a sibling profile.

### ExtractedEntity
A content-derived entity occurrence with type, raw and normalized values,
exact source span, and extractor name/version provenance. **PR 11
delivered**: deterministic observable occurrences. **PR 12 delivered**: the
same occurrence model carrying bounded `extraction_confidence` for
model-backed semantic types (PERSON/ORGANIZATION/ONLINE_IDENTITY/
THREAT_ACTOR/MALWARE/LOCATION/INDUSTRY/ORGANIZATION_TYPE/CREDENTIAL_TYPE/
ACCESS_TYPE/CRYPTO_ADDRESS). Occurrence-based (never a global IOC/entity
table), append-only, and never a fabricated probabilistic confidence for
PR 11 regexes.

### GeographicResolution (PR 12)
An immutable, versioned interpretation of one extracted `LOCATION` mention:
status (`RESOLVED`/`AMBIGUOUS`/`UNRESOLVED`), logical resolver name/version,
resolved timestamp, canonical attributes, WGS84 geometry when available, its
own resolution confidence, and bounded provider-neutral provenance. It is
separate from extraction and never promotes a mention to global truth; there
is no global location table or entity registry in PR 12.

### GeographicResolver (PR 12)
A Darkula-owned provider-neutral SPI that resolves one bounded location
mention + exact source context into a `GeographicResolverResult`. Concrete
gocoder SDKs, HTTP clients, and provider payloads never cross it.

### GeographicResolution
Resolution of an extracted geographic mention, separate from extraction confidence. It supports RESOLVED, AMBIGUOUS, and UNRESOLVED outcomes plus canonical geography, country/admin/locality information, geometry when available, resolver provenance, and confidence. PostgreSQL/PostGIS geometry is a likely persistence choice but is not fixed by PR 1.

### ExtractedRelationship (PR 13)
A content-derived assertion linking two persisted extracted-entity
occurrences with a finite predicate, direction, bounded extraction
confidence, exact supporting canonical span/text, and extractor
provenance. **PR 13 delivered**: the `relationship-assertions/v1` profile
over already-persisted `deterministic-observables/v1` and
`semantic-entities/v1` occurrences. It is evidence that one content asserted
the relationship, not automatically global truth; there is no global graph
edge or entity-merge behavior.

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
