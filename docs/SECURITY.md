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

**PR 7 delivered controls (local Podman).** The delivered `PodmanSandbox` runs each workload as a fresh disposable container with: rootless non-privileged execution under a dedicated non-root user (uid 10001), read-only rootfs, tmpfs `/tmp` and `/dev/shm` (bounded), `--cap-drop all`, `--security-opt no-new-privileges`, PID/memory/CPU ceilings, no host bind mounts, no container socket, and no host ports. The internal network carries no route back to the host, other networks, or the Internet; only the single controller-authorized Fake World destination is reachable (verified by real negative tests against PostgreSQL, Redpanda, an unauthorized sibling on another network, the internal gateway behind a host listener, and link-local metadata). Chromium launches **without** `--no-sandbox` as the non-root user under these restrictions (empirically verified). Residual limitation, documented and accepted for PR 7: members of the authorized internal network can reach each other; a Darkula-owned peer on the same network is therefore not a trust boundary, and only Darkula-owned services are ever provisioned onto it. Timeouts and cancellation kill the container; every terminal path removes it, and removal happens only after exact ownership metadata (`darkula.owned=true`) is verified. There is no unsandboxed fallback: a missing podman/image/network fails closed with a bounded error.

## Agent boundary
ReconAgent executes in the trusted Darkula environment, including when implemented with LangChain DeepAgent. It is not placed inside the crawler sandbox. It may use a WebSearchProvider for discovery and the Darkula Crawler capability for controlled source inspection, but it never receives a raw Playwright/browser tool, arbitrary shell execution, or direct sandbox control.

A reconnaissance interaction may be iterative: the ReconAgent requests bounded inspection, the trusted controller authorizes and executes it through the sandbox, and the agent reasons over the returned observation. The Coordinator retains final authority over lifecycle and work authorization.

SourceAnalyst never browses the dark web directly. Model inputs are adversarial data: instructions embedded in collected content are data, not trusted control instructions. Keeping the LLM outside the crawler sandbox prevents hostile page content from becoming executable browser/sandbox control, but it does not eliminate prompt-injection risk when untrusted observations later reach a model.

**PR 10 delivered controls (bounded reconnaissance).** The delivered `ReconAgent`/`ReconCoordinator` workflow hardens this boundary:

- **Untrusted source text is data, never instructions.** The versioned `recon-v1` system prompt states that all source observations are UNTRUSTED DATA and that instructions inside source content must never be followed. Source text is never interpolated into system instructions; it appears only as bounded evidence in the user prompt. Static tests assert injection text never reaches the system prompt.
- **The schema is the authorization surface.** The finite `ReconAgentDecision` schema has only `INSPECT` (target/purpose) and `COMPLETE` (disposition/confidence/characteristics/evidence refs). It cannot express headers, cookies, credentials, browser/sandbox/network options, shell commands, resource limits, or tool calls; unknown fields fail closed.
- **Every inspection is authorized by trusted code.** `authorize_recon_inspection` anchors the allowed origin at the candidate entrypoint, enforces same-origin parsed-origin equality, rejects unsupported schemes/userinfo/control characters, fixes credentials to `None`, and derives budgets only from `ReconSettings` (the model can never expand them). Cross-origin or unrepresentable requests fail closed before any crawler call, with no analytical REJECT fabricated.
- **Evidence references are validated provenance.** References are created only by trusted code; every model-returned reference is validated against the in-memory bounded registry before persistence. Unknown references fail closed with no final assessment.
- **Hard budgets** (`ReconSettings`) bound turns, inspections, pages/requests/depth/time per inspection, evidence items, excerpt length, aggregate context length, and accounted structured-output repair attempts independently of model behavior.
- **Prompt injection cannot escalate to lifecycle control.** The ReconAgent runs in the trusted process but has no persistence, browser, sandbox, shell, or provider-SDK access; even a fully compromised model output can only produce decisions that trusted code authorizes or rejects. Static import guards enforce these boundaries in CI.
- **No prompt/content/output telemetry.** Recon observability uses OTEL spans/metrics and `AgentObservability` with bounded content-free metadata only (operation name, agent name, prompt version, disposition); prompts, model output, source content, credentials, and high-cardinality URLs/candidate IDs are never recorded.
- **Provider secrets stay sanitized.** The OpenAI `LlmClient` adapter maps provider failures to the bounded `LlmError` taxonomy; raw provider exception text, prompts, model output, and API keys are never exposed. One call is one model attempt (`max_retries=0`) with a bounded timeout.
- **Fake World truth never leaks.** Canonical integration asserts truth-only tokens do not appear in prompts, persisted assessments/history, or telemetry.

## Artifact security
ObjectStore credentials remain in trusted components. Validate sizes/types/names and safely handle archives. Content-addressed deduplication is desirable but does not merge provenance. Retention/deletion policy must be explicit before production collection.

**PR 8 delivered controls (normalization and artifact storage).** All crawler
output remains untrusted after normalization. Normalization runs on the
confided trusted side, is deterministic and non-LLM, and never treats hostile
page text as trusted instructions -- prompt-injection content is stored as
data, not interpreted. Artifact storage is trusted-side only: the crawler
sandbox never receives an ObjectStore client, S3/R2 credentials, or any
storage capability. Physical ObjectKeys are derived solely from the SHA-256
representation hash (`sha256/<2>/<64>`), so hostile source URIs/filenames can
never influence filesystem/object-store traversal or key naming. A bounded
crawler sample/excerpt is stored as `SAMPLE` and is never labeled the complete
original artifact; completeness applies to the specific stored
representation. Provider (S3/R2) errors are mapped to bounded Darkula errors
and provider exception text never escapes. S3 ETag is never treated as the
Darkula SHA-256 content hash. Credentials/access keys/secret/session fields
are redacted from configuration diagnostics; no ObjectStore credential value
or high-cardinality identity (URI, object key, hash, title, source name)
appears in telemetry.

## Data and telemetry
Never log secrets, credentials, unrestricted stolen data, or raw authentication material. External agent-observability services require conservative content-capture policy and redaction. OTEL baggage crossing sandboxes/messages is allow-listed.

## Authentication/secrets
CollectionPolicy references credentials; it does not embed them. A secret resolver will be introduced before authenticated production crawling. Principle of least privilege applies to all adapters.

## Production readiness
Real illicit-source crawling is outside the initial Fake World development loop. Before enabling it, require reviewed sandboxing, egress policy, artifact controls, secret handling, resource limits, auditability, and applicable legal/operational review.

## PR 9 delivered controls (managed-source collection)

Collection is deterministic, policy-authorized work over managed Sources;
routine collection is never agentic and no LLM/agent decides navigation.

- **Authorization before any crawl.** `SourceCollectionService` requires a
  QUEUED run, an ACTIVE managed Source, an ACTIVE policy, and the run's
  frozen endpoints to be owned by the Source and ACTIVE before it claims the
  run. If authorization was withdrawn at admission, no crawl happens and the
  run is durably `CANCELLED` with a bounded code. Once RUNNING, the frozen
  policy snapshot governs the in-flight contract (policy edits cannot alter
  it).
- **Credential references only.** `CollectionPolicy` stores
  `authentication_reference` (bounded reference text, secret-free) and never
  a secret value; `CrawlRequest.credentials` is always `None` in the PR 9
  delivered path (unauthenticated collection). No secret resolver was
  introduced; authenticated crawling remains STOP-gated for a future PR.
- **IDs-only work messages.** The `collection.execute` v1 command carries
  exactly `collection_run_id`, `source_id`, `policy_id`. No URI, policy
  blob, content, artifact reference, or credential crosses DataStream.
  Broker positions are transport state, never run identity.
- **Unchanged PR 7 sandbox.** Collection adds no Podman/Playwright/ObjectStore
  capability to the sandbox surface: the composed Crawler owns the sandbox
  boundary exactly as in PR 7, storage/DB/stream/LLM credentials never cross
  it, and `CrawlRequest` budgets are still clamped/enforced by the PR 7
  controller.
- **Firewall between collection and persistence.** No PostgreSQL transaction
  spans crawler, HTTP, sandbox, ObjectStore, or broker I/O; the short-UoW
  admission/finalize structure makes a leaked long transaction structurally
  impossible.
- **Bounded state machine.** Expected-state transitions are enforced
  atomically in PostgreSQL (QUEUED -> RUNNING -> terminal); the execution
  lease bounds concurrent/crashed execution; attempts are capped
  (`max_run_attempts`); failures persist only the bounded sanitized
  vocabulary (fatal codes + summaries), never raw exception text, URIs,
  content, or secrets.
- **History is immutable.** Policy edits never rewrite old runs: each run
  retains `policy_revision` and its frozen execution snapshot (policy
  history survives edits).
- **Telemetry is bounded.** Collection counters/histograms carry only
  low-cardinality attributes (`darkula.outcome`, failure category); no
  run/source/policy ID, URI, source name, hash, object key, or credential
  ever appears in labels, and failure summaries are validated secret-free
  before persistence.

## PR 11 delivered controls (deterministic extraction)

Deterministic extraction operates only on already-persisted, already-hostile
normalized content; it never expands the trust boundary.

- **Hostile text is data, never control.** Extractors are pure string
  functions; extracted content can never alter extractor configuration,
  database behavior, ObjectStore keys, telemetry labels, code execution, or
  network access. Prompt-injection prose is stored as inert data.
- **No LLM, network, DNS, WHOIS/RDAP, or geocoding.** PR 11 imports no LLM
  provider, HTTP client, browser, sandbox, or DataStream boundary; static
  import guards enforce this in CI. There is no live enrichment.
- **Canonical representation, never the DB preview.** PostgreSQL stores only
  a bounded preview; extraction loads the canonical normalized-text
  representation from ObjectStore. There is no preview fallback.
- **Integrity before extraction.** ObjectStore reads are streamed with an
  explicit byte cap and fail closed on missing objects, size mismatch, hash
  mismatch, non-UTF-8 bytes, or non-text artifact kinds/mediatypes. S3/R2
  ETag is never treated as the Darkula SHA-256 representation hash.
- **Bounded output.** Input bytes, raw/normalized value length, extractor/
  profile names/versions, manifest count, span shape, and total entity count
  are bounded; exceeding the entity cap fails typed and never silently
  truncates facts.
- **Safe URL handling.** URLs containing userinfo/credentials are rejected and
  never persisted verbatim; no network fetch occurs.
- **Provenance, not a global graph.** Results/occurrences are append-only and
  content-scoped; the same value across observations or spans is retained as
  distinct occurrences. Extracted facts are not reputation or global truth.
- **Telemetry is content-free.** Only operation/profile/extractor/entity-type
  names, bounded outcome/failure categories, and counts/durations are
  recorded. URI, ObjectKey, content hash, IDs as metric labels, raw/
  normalized values, title/text, and credentials never appear.
- **Cancellation stays clean.** `asyncio.CancelledError` propagates; a
  cancellation before the write commits nothing, and one during the write
  rolls back so no partial result/occurrence set is ever durable.

## PR 12 delivered controls (semantic extraction and geographic resolution)

Semantic extraction is a bounded structured model operation, not agentic
browsing/reconnaissance; geographic resolution is a bounded provider-neutral
lookup. Both operate only on already-persisted, untrusted normalized content.

- **Source text is untrusted prompt data.** The semantic system prompt states
  that source content is UNTRUSTED DATA and that embedded instructions must
  never be followed. Source text appears only inside a clearly delimited
  untrusted-data block in the user prompt, never interpolated into system
  instructions. Prompt-injection prose is stored as inert data.
- **No model tools/browsing/network.** The semantic extractor has no browser,
  tool-calling, sandbox, crawler, DataStream, or network capability; it calls
  only the existing Darkula `LlmClient.generate_structured`. It never routes
  through ReconAgent/DeepAgent. Static import guards enforce this.
- **Structured output only.** Responses must validate against the strict
  Pydantic `SemanticExtractionResponse`; unknown fields/entity types and
  arbitrary metadata are rejected. No free-form model prose is parsed and no
  model-provided offsets/IDs are trusted.
- **Grounding before persistence.** Every persisted semantic occurrence
  satisfies `canonical_text[start:end] == raw_value` under exact code-point
  matching (no fuzzy/case-folded/whitespace-normalized alignment). Ungrounded
  and ambiguous repeated mentions are rejected rather than guessed; a
  malformed response contract fails the operation.
- **Confidence separation.** Extraction confidence and resolution confidence
  are distinct persisted fields; neither is collapsed into the other, and
  deterministic PR 11 rows keep NULL confidence.
- **Provider-neutral geography.** Application/domain code depends only on the
  `GeographicResolver` SPI; concrete geocoder SDKs/HTTP clients/provider
  payloads never cross it, and raw provider responses are never persisted. No
  live Internet geocoder is required by CI.
- **Fake World truth never enters production.** Production semantic/geographic
  code never imports `darkula.testing.fake_world` or `FakeWorldTruth`; the
  package's own static guard suite remains enforced. The canonical slice
  derives results from observable content plus the configured model/resolver
  boundaries, not truth.
- **No external I/O inside a transaction.** ObjectStore reads, model calls,
  and resolver calls occur between short read/write units of work; a crash or
  cancellation cannot leave a partial semantic result or resolution.
- **Bounded behavior.** Canonical bytes, prompt content, model candidate
  count, raw/normalized values, confidence, location context, resolver
  metadata, and entity counts are bounded; over-cap fails typed rather than
  truncating. `asyncio.CancelledError` propagates unchanged.
- **Content-free telemetry.** Semantic/geographic telemetry records only
  bounded operation/status/outcome names and counts/durations. Source text,
  raw/normalized values, location mentions, canonical names, coordinates,
  prompts, model output, provider payloads, object keys, hashes, and
  credentials never appear.
- **No silent fake.** Production composition selects a geographic resolver
  only by explicit configuration; enabling geography without one fails fast,
  and a fake is never substituted as a production fallback.
