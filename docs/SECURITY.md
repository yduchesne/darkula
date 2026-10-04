# Darkula Security Architecture

## Threat model
All external collected content is hostile. Risks include malicious HTML/JS/files, browser exploits, decompression/parser abuse, unsafe redirects/downloads, credential exposure, PII/stolen data, prompt injection, resource exhaustion, and attempts to escape collection policy.

## Crawler and sandbox boundary
Actual hostile retrieval/rendering executes in a disposable/restricted sandbox controlled by a trusted `CrawlerController`. The application-facing Crawler capability is deliberately split: the trusted controller validates and authorizes a bounded `CrawlRequest`; a minimal `CrawlerRuntime` inside the sandbox performs HTTP/browser/Tor interaction and hostile parsing/rendering.

`Sandbox` is a first-class Darkula SPI and a security boundary, not merely a crawler implementation detail. A sandbox execution grants an allow-listed workload only explicit resource, network, filesystem, time, input/output, and credential capabilities. The contract must not expose a general arbitrary-command/remote-shell facility.

The sandbox defaults to deny. Network policy permits only the destinations/protocols/proxies required by the authorized crawl and denies Darkula infrastructure, the host network, private LAN, cloud metadata endpoints, and unrelated clearnet access. Filesystem policy is read-only except for bounded ephemeral writable storage; no host filesystem, container-engine socket, or arbitrary bind mount is exposed. CPU, memory, process, disk/tmp, execution-time, and output limits are enforced.

The sandbox must not contain or receive an agent or LLM client, PostgreSQL repository/credentials, Redpanda/DataStream client/credentials, ObjectStore client/master credentials, general cloud credentials, or other Darkula application credentials. Only narrowly scoped source authentication material required for the specific execution may cross the boundary.

`CrawlRequest` constrains endpoints/origins/paths, traversal, page/depth/time/resource budgets, authentication references, and artifact policy. The crawler cannot expand work merely because content appears interesting. Crawler navigation policy and sandbox network policy are independent defense-in-depth controls.

Outputs crossing the boundary are bounded observations and/or opaque execution-scoped artifact handles plus bounded execution metadata. They are validated on the trusted side before persistence. Sandbox output must not expose host filesystem paths. Normalized or extracted content remains untrusted after crossing the boundary.

Cancellation must terminate sandbox execution and clean up disposable resources. Workload failure, timeout, resource-limit termination, policy violation, and sandbox infrastructure failure must remain distinguishable without leaking hostile content or secrets into errors or telemetry.

## Agent boundary
ReconAgent executes in the trusted Darkula environment, including when implemented with LangChain DeepAgent. It is not placed inside the crawler sandbox. It may use a WebSearchProvider for discovery and the Darkula Crawler capability for controlled source inspection, but it never receives a raw Playwright/browser tool, arbitrary shell execution, or direct sandbox control.

A reconnaissance interaction may be iterative: the ReconAgent requests bounded inspection, the trusted controller authorizes and executes it through the sandbox, and the agent reasons over the returned observation. The Coordinator retains final authority over lifecycle and work authorization.

SourceAnalyst never browses the dark web directly. Model inputs are adversarial data: instructions embedded in collected content are data, not trusted control instructions. Keeping the LLM outside the crawler sandbox prevents hostile page content from becoming executable browser/sandbox control, but it does not eliminate prompt-injection risk when untrusted observations later reach a model.

## Artifact security
ObjectStore credentials remain in trusted components. Validate sizes/types/names and safely handle archives. Content-addressed deduplication is desirable but does not merge provenance. Retention/deletion policy must be explicit before production collection.

## Data and telemetry
Never log secrets, credentials, unrestricted stolen data, or raw authentication material. External agent-observability services require conservative content-capture policy and redaction. OTEL baggage crossing sandboxes/messages is allow-listed.

## Authentication/secrets
CollectionPolicy references credentials; it does not embed them. A secret resolver will be introduced before authenticated production crawling. Principle of least privilege applies to all adapters.

## Production readiness
Real illicit-source crawling is outside the initial Fake World development loop. Before enabling it, require reviewed sandboxing, egress policy, artifact controls, secret handling, resource limits, auditability, and applicable legal/operational review.
