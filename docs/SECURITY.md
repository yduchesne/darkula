# Darkula Security Architecture

## Threat model
All external collected content is hostile. Risks include malicious HTML/JS/files, browser exploits, decompression/parser abuse, unsafe redirects/downloads, credential exposure, PII/stolen data, prompt injection, resource exhaustion, and attempts to escape collection policy.

## Crawler boundary
Actual hostile retrieval/rendering executes in a disposable/restricted sandbox controlled by a trusted Crawler Worker/Controller. The controller consumes bounded CrawlRequests and owns infrastructure credentials. The sandbox should not receive general PostgreSQL, Redpanda, ObjectStore, cloud, or application credentials.

CrawlRequest constrains endpoints/origins/paths, traversal, page/depth/time/resource budgets, authentication references, and artifact policy. The crawler cannot expand work merely because content appears interesting.

Dangerous parsing/rendering should occur inside the strongest practical sandbox boundary. Outputs crossing the boundary are constrained normalized data and/or bounded artifacts. Normalized content remains untrusted.

## Agent boundary
ReconAgent may request/interpret bounded reconnaissance but does not gain arbitrary execution capability. SourceAnalyst never browses the dark web directly. Model inputs are adversarial data: instructions embedded in collected content are data, not trusted control instructions.

## Artifact security
ObjectStore credentials remain in trusted components. Validate sizes/types/names and safely handle archives. Content-addressed deduplication is desirable but does not merge provenance. Retention/deletion policy must be explicit before production collection.

## Data and telemetry
Never log secrets, credentials, unrestricted stolen data, or raw authentication material. External agent-observability services require conservative content-capture policy and redaction. OTEL baggage crossing sandboxes/messages is allow-listed.

## Authentication/secrets
CollectionPolicy references credentials; it does not embed them. A secret resolver will be introduced before authenticated production crawling. Principle of least privilege applies to all adapters.

## Production readiness
Real illicit-source crawling is outside the initial Fake World development loop. Before enabling it, require reviewed sandboxing, egress policy, artifact controls, secret handling, resource limits, auditability, and applicable legal/operational review.
