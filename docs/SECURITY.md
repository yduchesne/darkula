# Darkula Security Architecture

## Threat model
All external collected content is hostile. Risks include malicious HTML/JS/files, browser exploits, decompression/parser abuse, unsafe redirects/downloads, credential exposure, PII/stolen data, prompt injection, resource exhaustion, and attempts to escape collection policy.

## Crawler and sandbox boundary
Actual hostile retrieval/rendering executes in a disposable/restricted sandbox controlled by a trusted `CrawlerController`. The application-facing Crawler capability is deliberately split: the trusted controller validates and authorizes a bounded `CrawlRequest`; a minimal `CrawlerRuntime` inside the sandbox performs HTTP/browser/Tor interaction and hostile parsing/rendering.

`Sandbox` is a first-class Darkula SPI and a security boundary, not merely a crawler implementation detail. A sandbox execution grants an allow-listed workload only explicit resource, network, filesystem, time, input/output, and credential capabilities. The contract must not expose a general arbitrary-command/remote-shell facility.

Sandbox policy is a generic capability allowlist, not a blacklist of Darkula services. The default is deny across network, filesystem, runtime, resource, credential, and output capabilities. A workload receives only capabilities explicitly required for that execution. PostgreSQL, Redpanda, ObjectStore, host services, private networks, cloud metadata, and unrelated Internet destinations are examples of things a crawler normally cannot reach; the Sandbox abstraction must not encode product-specific deny flags for them.

Network policy permits only explicitly authorized destinations, protocols/ports, DNS behavior, and proxies/egress mechanisms, with connection/traffic bounds where practical. A Fake World execution may be restricted to its Fake World HTTP endpoint; a future Tor execution may be restricted to a controlled Tor proxy rather than receiving arbitrary Internet access. Workloads that do not require networking receive no network capability.

Filesystem policy is read-only except for explicitly granted inputs and bounded ephemeral writable storage; no host filesystem, container-engine socket, or arbitrary bind mount is exposed. Runtime policy requires an unprivileged container, drops unnecessary Linux capabilities, applies no-new-privileges, and restricts device and namespace exposure where supported. Resource policy bounds CPU, memory, processes/PIDs, disk/tmp, open files where practical, and execution time. Output policy bounds artifact count, per-artifact size, aggregate output, and permitted output forms.

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
