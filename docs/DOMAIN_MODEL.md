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

**PR 14 delivered**: `SourceAssessment` is produced by the explicit,
caller/policy-triggered `SourceAnalysisService` over a bounded historical
window of one managed `Source`. It carries bounded `profile_name`/
`profile_version` provenance (`source-analysis/v1` for PR 14; pre-PR14 rows
round-trip as `legacy/v0`). `window_start`/`window_end` describe the
historical observation period (`observed_at` inclusive on both bounds) and
`assessed_at` describes when Darkula made the assessment. The score semantics
are bounded judgments, not truth probability or credibility:

- `confidence` — confidence in the overall assessment given supplied evidence;
- `relevance` — relevance to Darkula intelligence collection during the window;
- `activity` — degree of observed activity during the window;
- `novelty` — meaningful newness supported by the bounded context (may be
  `None` when unsupported).

`evidence_references` are durable trusted references (`content:<id>`,
`entity:<id>`, `geography:<id>`, `relationship:<id>`,
`collection-run:<id>`) generated by trusted code from supplied invocation
refs. `characteristics` is the finite `SourceCharacteristics` schema. The
semantic idempotency key is
`(source_id, window_start, window_end, profile_name, profile_version)`.
Assessments never mutate `Source` or extraction history, and the absence of
collected evidence is never inferred to mean inactivity.

## v0.2 discovery foundation (PR 17 delivered; registry polling PR 18 delivered; candidate admission PR 19 delivered)

PR 17 delivers the executable discovery/provenance domain, additive
PostgreSQL persistence, and the atomic qualification persistence primitive.
PR 18 delivers bounded registry observation ingestion and a durable poll
checkpoint, still without candidate admission. See
[v0.2 Roadmap](ROADMAP_V02.md) and [Architecture](ARCHITECTURE.md).

The v0.1 semantics of `SourceCandidate`, `Source`, `SourceEndpoint`,
`ReconAssessment`, `CollectionPolicy`, content artifacts, and
`SourceAssessment` are unchanged.

### DiscoveryObservation (PR 17)

An immutable, provenance-bearing observation of one untrusted provider claim
(structured registry, collected link, semantic reference, or manual note).
It is evidence, never source truth, never a candidate, and never crawl
authorization.

Frozen enums:

| Enum | Values |
| --- | --- |
| `DiscoveryReferenceKind` | `LOCATOR`, `NAME_ONLY` |
| `DiscoveryDetectionMethod` | `REGISTRY`, `CONTENT_LINK`, `CONTENT_SEMANTIC`, `MANUAL` |
| `DiscoveryValidationStatus` | `VALID_LOCATOR`, `UNRESOLVED_NAME` |
| `AccessStatus` | `UNKNOWN`, `PUBLIC`, `CREDENTIALS_PENDING`, `READY`, `AUTH_FAILED`, `ACCESS_REVOKED` |
| `QualificationMethod` | `REGISTRY_POLICY`, `AGENT_RECON` |
| `QualificationOutcome` | `QUALIFIED`, `RECONNAISSANCE_PENDING`, `REJECTED` |

Persisted fields: `observation_id` (UUID), `provider_id`,
`provider_record_id`, `provider_record_version`, `detection_method`,
`reference_kind`, `asserted_reference`, `normalized_locator`,
`validation_status`, `source_kind_hint`, `requires_auth_claim`,
`origin_reference`, `evidence_reference`, `evidence_excerpt`, `observed_at`
(provider time), `ingested_at` (Darkula acceptance time), `candidate_id`
(always `NULL` from PR 17 ingestion), and `semantic_key`.

**Identity and replay.** The semantic replay tuple is
`(provider_id, provider_record_id, provider_record_version,
detection_method, reference_kind)`, stored as five columns under one UNIQUE
constraint. `semantic_key` is a deterministic SHA-256 over UTF-8, 4-byte
big-endian length-prefixed components of that tuple. Python `hash()`,
random UUIDs, broker positions, timestamps, candidate IDs, and URIs are never
replay identity. A replay with an identical normalized payload returns the
stored original row (original `observation_id`/`ingested_at`) with no second
row. A same-tuple payload difference (including a changed `observed_at`) is a
conflict: no overwrite, no merge, no second row. Distinct provider record
versions and distinct providers coexist as separate immutable observations;
equal locators never collapse evidence.

**Locator validation.** `normalize_discovery_locator` is a deterministic,
I/O-free, conservative syntactic normalizer for absolute `http`/`https`
locators (including `.onion` hosts). It lowercases scheme/host, applies
standard-library IDNA to non-ASCII hosts, validates IPv4/IPv6 literals,
removes only the scheme's default port, preserves path/query byte-for-byte,
rejects userinfo/fragments/controls/backslashes/trailing-dot hosts/invalid
ports/ambiguous percent-encoding/dot segments/secret-like query keys, and is
idempotent. It does not resolve DNS, fetch, follow redirects, unshorten, or
grant egress: DNS/redirect/private-address authorization remains PR 20.
A `NAME_ONLY` reference has `normalized_locator = NULL`,
`validation_status = UNRESOLVED_NAME`, and is rejected if it parses as an
absolute URL. No URL is ever synthesized for a name.

**Bounds.** Provider ID 1-100 lower-case ASCII `[a-z0-9][a-z0-9._-]*`
(uppercase rejected, never folded); record ID 1-256; record version 1-128
(opaque, never trimmed/folded); asserted/origin/evidence reference <= 2048;
excerpt <= 512; no C0/DEL controls; no blank values; credential/secret-like
values rejected where detectable; timestamps timezone-aware and normalized to
UTC (naive rejected). Validation runs in Python before any transaction and is
mirrored by database CHECK constraints.

### SourceDiscoveryService (PR 17)

Deterministic, one-observation-at-a-time ingest boundary:
`DiscoveryInput -> validate/normalize -> one short UnitOfWork -> versioned
stored function -> DiscoveryObservation`. It performs no network access,
crawling, DNS, LLM call, candidate/source/endpoint/policy/run creation,
event/outbox append, or access/crawl authorization. `DiscoveryProvider` is a
provider-neutral protocol; PR 18 supplies one bounded registry adapter and a
one-shot poller, PR 19 owns candidate admission/reconciliation.

### RegistryPollCheckpoint (PR 18)

Durable **polling position** for one allowlisted registry provider, not a
source/candidate/policy identity. Fields: `provider_id`, `cursor` (the exact
bounded upstream entry name last fully ingested, or `NULL` for the explicit
end-of-pass state), `generation` (optimistic compare-and-swap counter; `0`
means not yet persisted), `cooldown_until`, and `last_completed_at`.
`advance_registry_checkpoint` moves the cursor only after every accepted
observation for that entry committed; `defer_registry_checkpoint` records a
429/transient cooldown without moving the cursor. The cursor is never a URL,
broker offset, source identity, or observation identity, and a checkpoint is
shared poll state (not owned by a MonitoringProfile). Provider record
identity/revision remain distinct from the Darkula `observation_id` and its
acceptance `ingested_at`; replay reuses the first-seen `observed_at` when the
registry supplies no stable timestamp.

### AccessEvidence (PR 17)

An immutable, non-authorizing access record with `access_evidence_id`,
`observation_id`, exactly one of `candidate_id`/`source_endpoint_id`,
`status`, `reason_code`, `recorded_at`, and `evidence_reference`. Access
readiness is orthogonal to `CandidateStatus` and `SourceStatus`.

PR 17 ingestion with no admitted candidate creates **no** access record (it
persists the `requires_auth_claim` on the observation only). The only PR 17
write operation appends `CREDENTIALS_PENDING` with reason
`PROVIDER_AUTH_CLAIM` for an **already existing** candidate whose observation
claims `requires_auth=True`; it never changes candidate lifecycle and never
produces `PUBLIC`/`READY`/`AUTH_FAILED`/`ACCESS_REVOKED`. Provider
`requires_auth=false` or `available=true` claims never imply `PUBLIC` or
`READY`. Replay of the same `(observation_id, candidate_id, status)` returns
the existing immutable record; a different status for the same tuple is a
conflict. Latest state is derived from ordered immutable evidence
(`recorded_at`, `access_evidence_id`), never a mutable current-status column.

### QualificationDecision (PR 17 persistence primitive)

An immutable decision record distinct from `ReconAssessment` (which only
records actual ReconAgent reasoning). It carries `decision_id`,
`candidate_id`, `method`, `outcome`, `evaluated_at`, `policy_id`,
`policy_version`, optional `provider_id`/`provider_record_id`, evidence
`observation_id`s (1-64 unique), optional real `recon_assessment_id`,
`event_id`, `expected_status`, `new_status`, and a deterministic
`decision_key`.

Rules: registry decisions require provider/record identity and must not
reference a `ReconAssessment`; agent decisions require a real assessment that
belongs to the same candidate. Every outcome maps to exactly one existing
candidate status/event: `QUALIFIED -> QUALIFIED`, `RECONNAISSANCE_PENDING ->
RECONNAISSANCE_PENDING` (`NEEDS_MORE_RECON`), `REJECTED -> REJECTED`.
`decision_key` is SHA-256 over length-prefixed
`(candidate UUID, method, policy ID, policy version, sorted evidence UUIDs,
recon assessment UUID or empty)`.

The decision, exactly one candidate status transition, and exactly one
candidate event commit atomically in one stored function/transaction: the
candidate `UPDATE`, the event `INSERT`, and the decision `INSERT` share one
protected PL/pgSQL block, so a `unique_violation` on the decision row also
rolls back the status change and the event even if the caller commits the unit
of work after handling the typed conflict.

Replay is exact: the supplied event must be linked by
`event.event_id == decision.event_id`, and an existing `decision_key` replays
only when every immutable decision field, the original `evaluated_at`, and the
persisted event's type, `occurred_at`, nullable `context` (JSONB content, key
order irrelevant), `reason`, and `provenance` all match. A regenerated
`decision_id` is permitted on an otherwise exact retry and the stored ID is
returned; any other difference is a `ConflictError` with zero durable effects
and never a second decision.

A `REGISTRY_POLICY` decision must also cite at least one observation whose
`detection_method = 'REGISTRY'` and whose `provider_id`/
`provider_record_id` equal the claimed pair. A matching observation that is
merely present in the database, or a non-registry observation with equal
strings, does not satisfy the provenance rule (`IntegrityError`, no effects);
`AGENT_RECON` instead requires the genuine same-candidate assessment.

**No PR 17 service produces decisions**: PR 19 owns deciding eligibility,
policy trust, recon routing, and invoking the repository operation. The
existing PR 10 `ReconCoordinator` path is unchanged; PR 19 must coordinate
ownership so a candidate is not double-transitioned.

### Candidate admission (PR 19)

The explicit, trusted post-ingest step over an immutable
`DiscoveryObservation`. It is deterministic application code, never an agent:

`DiscoveryObservation -> bounded exact reconciliation -> durable ledger
outcome -> (optional separate) policy routing`.

**Identity separation.** `DiscoveryObservationId` is the claim identity; the
persisted `normalized_locator` is a syntactic key; `sha256(normalized
locator)` is the *admission-slot lookup digest*; `SourceCandidateId` is the
candidate identity; `SourceId` is a logical managed source; `SourceEndpointId`
is an endpoint identity; the `QualificationDecision` key is the immutable
policy/evidence replay identity. A normalized URL is never a Source identity,
a locator slot is never a Source relationship, and an equal digest/locator
pair never proves two claims describe the same real organization.

**Finite admission vocabulary.** `AdmissionOutcome` = `UNRESOLVED_NAME`,
`KNOWN_ENDPOINT`, `ADMITTED_NEW`, `ADMITTED_EXISTING`, `REVIEW_REQUIRED`;
`AdmissionReason` = `NAME_ONLY`, `EXACT_MANAGED_ENDPOINT`, `NEW_LOCATOR`,
`EXISTING_LOCATOR_SLOT`, `EXISTING_LEGACY_CANDIDATE`,
`MULTIPLE_LEGACY_CANDIDATES`, `EXCESS_ENDPOINT_MATCHES`. `AdmissionResult`
carries the observation, outcome/reason, optional candidate, bounded exact
`SourceEndpointId` matches (never URIs), and a `replayed` flag. `ADMITTED_*`
carries a candidate and no endpoints; `KNOWN_ENDPOINT` carries at least one
endpoint and no candidate; the remaining outcomes carry neither.

**Deterministic decision matrix (first match wins).** Ledger replay; name-only
-> `UNRESOLVED_NAME` (no synthetic locator); at most 32 exact
`source_endpoint.uri` matches -> `KNOWN_ENDPOINT` (evidence only; no Source,
endpoint, policy, run, or collection write); at most 2 exact
`source_candidate.entrypoint` matches with no existing slot -> reuse the UUID
(`PROMOTED` and ambiguous multi-match stay `REVIEW_REQUIRED`); otherwise a new
`DISCOVERED` candidate via the existing `candidate_create_v1`. More than 32
endpoint matches -> `REVIEW_REQUIRED/EXCESS_ENDPOINT_MATCHES`; more than one
legacy candidate -> `REVIEW_REQUIRED/MULTIPLE_LEGACY_CANDIDATES`. Matching is
exact-string only: no case folding, slash guessing, DNS, fuzzy matching,
`ILIKE`, or shortener expansion, and no global URI->Source uniqueness.

**Admission slot.** `candidate_locator_slot` maps the lowercase SHA-256 hex
digest of the persisted normalized locator to one candidate and retains the
exact normalized locator for re-validation. A slot with an equal digest and a
different locator is a fatal `HASH_COLLISION` (rolled back, never aliased). The
slot suppresses duplicate candidate creation for one exact locator only; it
does not merge Sources and does not update `discovery_observation.candidate_id`
(observations remain immutable).

**Ledger.** `discovery_candidate_admission` has exactly one row per handled
observation including unresolved-name and review outcomes, so pending
selection never re-processes a handled claim forever. Pending selection is
provider-scoped, deterministic `(ingested_at, observation_id)`, bounded 1..50.
`discovery_endpoint_match` records exact managed-endpoint evidence only and is
written exclusively by the admission stored function.

**Possible mirrors/aliases.** Read-only `PossibleAliasSuggestion` claims
(`SHARED_PROVIDER_ORIGIN`) exist only for two distinct admitted candidates
sharing an exact non-null `(provider_id, origin_reference)` with different
normalized locators; at most 16 are returned and an overflow is an explicit
`too_many` result with no partial list. They never merge Sources, rotate
endpoints, admit one candidate as another, or imply trust.

**Authentication.** For each admitted candidate whose persisted observation
claims `requires_auth_claim=True`, admission appends PR 17
`CREDENTIALS_PENDING` evidence with reason `PROVIDER_AUTH_CLAIM` in the same
unit of work. Missing credentials never implies `REJECT`, `READY` is never
produced, and a candidate-only managed-endpoint match never manufactures a
candidate just to record evidence.

**Qualification routing (PR 19).** `CandidateQualificationPolicy`
(`candidate-qualification`/`v1`, rule `RANSOMLOOK_LOCATION_V1`) is pure,
fixed, default-deny operator-configured code. It can only qualify when the
operator enabled `qualification.ransomlook_location_trusted`, the candidate is
`DISCOVERED`/`RECONNAISSANCE_PENDING`, the linked claims are 1..64 immutable
versions of exactly one RansomLook `location:` record sharing provider,
origin, and normalized locator with `REGISTRY` detection,
`LOCATOR`/`VALID_LOCATOR`, matching `ONION`/`CLEARNET` hint and no auth claim,
the record ID is `location:` + 64 lowercase hex, the origin is the fixed
public RansomLook group reference, and there is no known-endpoint or review
conflict. The decision cites exactly the latest `(ingested_at,
observation_id)`; other versions stay linked in the admission ledger.
Otherwise the candidate routes to `RECONNAISSANCE_PENDING` with at most one
existing `NEEDS_MORE_RECON` event and no fabricated decision. `AGENT_RECON`
decisions are only produced by the existing `ReconCoordinator` when a trusted
`ReconQualificationContext` links the genuine same-candidate assessment.

### MonitoringProfile (v0.2, planned — owned by PR 24/25)

Caller-defined monitoring interest, **not** a tenant or account:

- `monitoring_profile_id` — generated UUID, canonical ID for profile-scoped operations;
- `name` — descriptive label;
- `correlation_id` — arbitrary caller-supplied string unique in the whole Darkula instance, or a generated UUID string when omitted;
- optional, bounded entity-match definitions.

`correlation_id` resolves to the UUID through a dedicated API; it is not accepted as the identity for ordinary profile-scoped API calls. A shared Source and source-owned content can contribute evidence to many MonitoringProfiles without repeated crawling. Profile-specific matching outputs carry profile UUID, policy/version and durable links to source/content/evidence provenance.

The existing SourceAssessment `profile_name` and `profile_version` are **analysis profile** metadata, unrelated to MonitoringProfile identity. Do not rework or migrate that field as a profile foreign key.

### Artifact ownership

Existing raw/normalized content has source provenance and shared, SHA-256-derived ObjectStore keys; it remains collected once independent of profiles. New profile-specific derived/export artifact paths must include `monitoring_profile_id` and link to shared source-owned content. Do not move or duplicate v0.1 shared artifacts under profile UUID prefixes.

## Semantic rules (v0.1 baseline)
1. Extraction produces facts/annotations; analysis produces assessments.
2. Preserve observed claims separately from resolved/canonical interpretations.
3. Preserve provenance at every stage.
4. Source identity is not endpoint identity.
5. Agent decisions are explicit outputs consumed by coordinating application logic.
6. Domain identifiers should allow correlation across collection, crawl, content, extraction, LLM/agent runs, and assessments.
