# Darkula Domain Model

This document defines semantic concepts, not a relational schema.

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
