# Darkula Coding-Agent Guide

## Purpose
Darkula is a secure, evidence-oriented source discovery, crawling, content-extraction, and source-assessment platform. v0.1 prioritizes the generic crawling framework; multi-tenancy and product-specific commercial capabilities are later concerns.

## Required reading
Before changing architecture or domain behavior, read docs/ARCHITECTURE.md, docs/DOMAIN_MODEL.md, docs/SECURITY.md, docs/TESTING.md, docs/EVALUATIONS.md, docs/OBSERVABILITY.md, docs/CONFIGURATION.md, docs/FAKE_WORLD.md, and docs/ROADMAP_V01.md.

## Architectural invariants
- Distinguish agents from deterministic services. ReconAgent and SourceAnalyst reason; SourceCollectionService, crawler execution, normalization, and Content Extraction are services/infrastructure.
- Extraction produces structured facts/annotations. Analysis produces assessments.
- SourceAnalyst never receives unrestricted browser/Tor access. Hostile retrieval executes behind the crawler sandbox boundary.
- PostgreSQL is authoritative for structured domain state. ObjectStore is authoritative for large/blob artifacts. DataStream carries asynchronous work/events and references, not large artifacts.
- Production Python contains no database-behavior/data-access SQL. All database behavior (table access, joins, predicates, mutations, DDL, transactions) is implemented by versioned PostgreSQL stored functions owned by versioned SQL artifacts and invoked by infrastructure repository classes; application/domain code remains SQL-free and reaches persistence only through Darkula persistence/repository and UnitOfWork abstractions. PostgreSQL infrastructure repositories may contain only fixed, parameterized invocations of approved versioned stored functions — `SELECT * FROM <name>_v<positive integer>(%s, ...)` — with every value bound as a separate Psycopg parameter; no f-strings, `.format()`, concatenation, identifier interpolation, `%`-formatting, or embedded values. Test Python code may execute SQL directly for database setup/cleanup, fixture maintenance, verification/assertions, fault injection, and independent inspection of database state; this exception must not become an alternate production persistence path.
- DataStream, ObjectStore, LlmClient, persistence, and agent-observability capabilities are accessed through Darkula-owned interfaces.
- Redpanda is the initial DataStream implementation, not an application-layer dependency.
- LLM consumers depend only on LlmClient. Production adapters may include LangChainLlmClient; deterministic tests use FakeLlmClient.
- Operational telemetry uses OpenTelemetry. Do not create a vendor-neutral replacement for OTEL.
- Agent-specific observability is provider-neutral behind an interface so LangSmith, Langfuse, and future adapters remain replaceable.
- Prefer Darkula OTEL decorators for method/function tracing, timers, and counters when invocation boundaries match the measurement. Use OTEL APIs directly for internal events/dynamic measurements.
- Configuration uses Pydantic Settings and layered profiles. Non-empty environment variables are the ultimate override. Unset or empty environment variables have no override effect.
- Configuration selects implementations centrally at the composition root; do not scatter driver checks through application/domain code.
- Preserve provenance even when physical artifacts are content-deduplicated.
- Assume at-least-once stream delivery; consumers must be idempotent.
- Durable state change plus outgoing event must use a transactional-outbox style guarantee where applicable.
- Broker positions are transport state, never domain identity; durable consumer idempotency uses the stable `message_id` (scoped by `(stream_name, consumer_id)`), never an offset.
- Polling never acknowledges. A broker position is acknowledged only after the corresponding durable processing committed; never acknowledge before commit, and never hold a DB transaction open during broker I/O.
- Stream messages are small commands/events/references. Raw HTML, PDFs, screenshots, and other large artifacts belong in ObjectStore, never in the stream.
- Kafka/Redpanda types never leak into domain/application code; the `DataStream` boundary is the only transport interface application code may use.
- Treat all collected content as untrusted even after normalization.
- Normalization/artifact invariants (PR 8, durable): ObjectStore owns bytes;
  PostgreSQL owns structured identity/provenance for `ContentArtifact` and
  `NormalizedContent`; an `ObjectKey` is a storage address (never identity,
  never provenance, never a content hash); physical content-addressed
  deduplication NEVER merges provenance — two observations with identical
  bytes/hash remain distinct immutable observations; a bounded
  sample/excerpt is never represented as the complete original artifact
  (completeness applies to the specific stored representation); the crawler
  sandbox never receives any ObjectStore client, S3/R2 credentials, or
  storage capability (storage is trusted-side only); normalization is
  deterministic and non-LLM — the same supported input yields the same
  canonical bytes/hash and hostile content is stored as data, never
  interpreted; no raw hostile artifact body or high-cardinality identity
  (URI/object key/hash/title) may be placed in telemetry; S3 ETag is never
  treated as the Darkula SHA-256 representation hash.
- Crawler/sandbox invariants (PR 7, durable): exactly one fresh disposable container per sandbox execution, never container reuse; default-deny isolation (network, filesystem, runtime, resource, input, output capabilities granted only when the specific execution needs them); a crawl's authorized destination is exactly its `AllowedOrigin`, enforced below workload logic AND by the runtime's origin check; the sandbox never contains or receives Darkula application credentials, LLM/DB/DataStream/ObjectStore clients, or general secrets (only the narrowly scoped source credential of that crawl may cross); there is never an unsandboxed fallback — a missing podman/image/network fails closed; controller and sandbox podsman interactions use no broad cleanup and remove/repurpose only resources positively verified `darkula.owned=true` by exact metadata; raw HTML/PDF/download bodies never cross the boundary as output (only bounded JSON observations/samples; artifact storage is ObjectStore/PR 8 work); caller cancellation must terminate the workload and remove the disposable container before propagating.
- Collection invariants (PR 9, durable): routine collection is deterministic policy-authorized work — `CollectionPolicy -> CollectionScheduler -> outbox -> CollectionWorker -> SourceCollectionService -> Crawler -> ContentIngestService` — never agentic; `Source` is logical identity, `SourceEndpoint` is a locator, and `CollectionPolicyId`/`CollectionRunId`/broker `message_id`/broker position are distinct identities (never use a URI or broker offset as collection identity); `CollectionRunId` is the authoritative work identity and the occurrence identity is `(policy_id, scheduled_for)`; PostgreSQL owns policy/run state/schedules/leases/counters and expected-state transitions are enforced atomically in stored functions; a run is an immutable historical execution carrying `policy_revision` + frozen `policy_snapshot` so policy edits never rewrite history; DataStream carries only small IDs-only commands/events (never URI/content/credentials); no PostgreSQL transaction spans broker/crawler/sandbox/HTTP/ObjectStore I/O (short admission UoW -> external work -> short finalize UoW; outbox appended in the same transaction as the run it announces); collection work is never executed inside the ReliableConsumer handler (a collection-specific worker poll -> durable admission -> execute -> terminal -> ack instead); terminal durable state is required before acknowledgement, terminal runs never recrawl, unexpired leases forbid concurrent execution, expired leases are reclaimed as the same run within bounded `max_run_attempts`; policy stores credential references only (no secret values) and PR 9 delivers unauthenticated crawling; failure summaries are the bounded sanitized vocabulary and telemetry carries no run/source/policy ID, URI, source name, hash, object key, or credential.
- Extraction invariants (PR 11, durable): deterministic extraction converts one persisted normalized representation into bounded, provenance-bearing structured observations; it is NOT analysis and performs no semantic/model-backed reasoning, relationship inference, geographic resolution, reputation judgment, or global-truth promotion (PR 12-14 own those). Extraction is deterministic and LLM-free: no `LlmClient`, provider SDK, browser, sandbox, DataStream, DNS/WHOIS/RDAP/geocoding, wall-clock matching semantics, randomness, or network I/O, and production extraction modules must not import those boundaries (static guards enforced). The extraction source is the canonical `NORMALIZED_TEXT` representation in ObjectStore, NEVER the bounded PostgreSQL text preview, with no preview fallback; object existence, bounded byte count, SHA-256 against persisted `ContentArtifact.content_hash`, expected kind/type, strict UTF-8, and the normalized-text byte bound are verified before any extractor runs (S3/R2 ETag is never the Darkula SHA-256 hash). No PostgreSQL transaction spans ObjectStore I/O: short metadata read UoW -> close -> ObjectStore bounded read -> deterministic extraction outside any UoW -> short write UoW that commits the `ExtractionResult` and ALL `ExtractedEntity` rows atomically. Identities stay distinct (`NormalizedContentId`, `ContentArtifactId`, `ObjectKey`, `ContentHash`, `ExtractionResultId`, `ExtractedEntityId`, extractor name/version, `SourceSpan`); semantic idempotency is `(content_id, profile_name, profile_version)`, never an ObjectKey, content hash, timestamp, generated UUID, or broker position. Every occurrence retains exact content/result/extractor/version/span provenance (`text[start:end] == raw_value`) and raw+normalized values; the same value at two spans or in two observations is two append-only occurrences, and an `ExtractedEntity` asserts only deterministic recognition, never maliciousness/ownership/identity/relationship. Results/occurrences are immutable/append-only/versioned: a new extractor/profile version creates a new result and never rewrites history, and the exact historical extractor manifest is persisted. Extraction is bounded (input bytes, value lengths, names/versions, manifest count, spans, total entities) and a cap overflow fails typed instead of silently truncating. Routine extraction produces no relationships, no `SourceAssessment`, and no automatic Source creation. Errors/telemetry are content-free (no URI, ObjectKey, content hash, high-cardinality ID, raw/normalized entity value, title/text, or credential); `asyncio.CancelledError` propagates and leaves persistence clean.
- Semantic extraction invariants (PR 12, durable): model-backed semantic extraction is a separate profile (`semantic-entities/v1`, extractor `semantic-llm/v1`) that never replaces or reconfigures the deterministic `deterministic-observables/v1` profile; it is extraction, not analysis, and produces occurrences (never global entities or relationships). It must use the existing Darkula `LlmClient.generate_structured` with a strict Pydantic response model — never a provider SDK, LangChain, ReconAgent/DeepAgent, tools, browsing, or network from extraction code. Source content is UNTRUSTED DATA: never follow embedded instructions, never put source text in system instructions, and never let the model invent provenance — every persisted semantic occurrence must satisfy `canonical_text[start:end] == raw_value` under exact code-point matching (no fuzzy/case-folded/whitespace-normalized alignment, no trusted model offsets); ungrounded or ambiguous repeated mentions are rejected/counted, never guessed, and entity overflow fails typed rather than truncating. `extraction_confidence` is a bounded extracted-concept confidence, persists separately, and remains `None` for deterministic PR 11 occurrences. Semantic result idempotency reuses `(content_id, profile_name, profile_version)`; semantic and deterministic results coexist immutably; ObjectStore bytes (never the DB preview) drive extraction with size/hash/UTF-8 verification; and no PostgreSQL transaction spans ObjectStore or LLM I/O (short read UoW -> close -> external work -> short atomic write UoW).
- Geographic resolution invariants (PR 12, durable): a `LOCATION` `ExtractedEntity` is a content mention, never a canonical place; geographic resolution is a separate, explicit, immutable/versioned interpretation (`RESOLVED`/`AMBIGUOUS`/`UNRESOLVED`) with its own `GeographicResolverIdentity` name/version and its own resolution confidence, distinct from extraction confidence. Resolution must go through the Darkula-owned provider-neutral `GeographicResolver` SPI (never a concrete geocoder SDK/HTTP client/provider payload in application/domain code; raw provider responses are never persisted), passes only a bounded mention plus exact code-point context, and must never mutate the occurrence's raw/normalized value, span, or extraction confidence. RESOLVED requires canonical name + confidence; AMBIGUOUS/UNRESOLVED must not carry canonical fields or geometry; a new resolver version coexists under `(extracted_entity_id, resolver_name, resolver_version)`. No production fake is silently selected (enabling geography without a resolver fails fast), no live Internet geocoder is required by CI, and no PostgreSQL transaction spans resolver or ObjectStore I/O. Fake World truth and production BlackGate/`"Washington"` special cases must never enter extraction/resolution code.
- For independent I/O-bound operations, prefer bounded asynchronous concurrency so I/O waits can overlap. Use `asyncio.gather()` where it fits, with an explicit concurrency bound (for example bounded batches, a semaphore, or a bounded worker/task pool). Never create unbounded task fan-out. Preserve ordering, transaction, rate-limit, resource, error, and cancellation semantics where they require serialization or tighter control.

## Engineering rules
- Keep domain/application layers independent of Kafka/Redpanda, S3/R2, LangChain, LangSmith/Langfuse, Prometheus, Loki, Jaeger, and crawler implementation details.
- Prefer small typed contracts and explicit DTOs. Do not leak third-party framework types through Darkula interfaces.
- Fail closed at trust boundaries and fail fast for invalid startup configuration.
- Never log credentials, secrets, unrestricted collected content, or sensitive prompt payloads.
- New source/crawler behavior should be justified by a documented real-world archetype and represented in Fake World scenarios where practical.
- Keep docs synchronized with architectural changes.
- Do not prematurely freeze details explicitly marked TBD in the architecture documents.

## Fake World rules (PR 6, durable)
- Fake World truth never enters the production pipeline: production domain/
  application/infrastructure packages (and composition) must never import
  `darkula.testing.fake_world` or `FakeWorldTruth`; the static guard in
  `tests/unit/testing/fake_world/test_architecture.py` stays enforced.
- Scenarios are versioned and deterministic: same ID/version/world/request
  renders identical output; fixed UTC timestamps only, no wall clock or
  global randomness; incompatible semantic changes require a version bump,
  never silent mutation of a frozen canonical scenario.
- No live illicit content: Fake World material is fully synthetic (names,
  organizations, credentials, posts, attachments, identities); never copy
  real stolen/victim/underground data or executable malware fixtures.
- The Fake World renderer is the external-world test boundary: crawler/sandbox
  PRs adapt to it, and PR 6+ tests must not require live network/browser/Tor
  or a second messaging/persistence architecture for Fake World.
- Significant canonical behaviors keep stable `FW-*` behavior IDs linked to
  scenario/version, requirement, and deterministic tests; never drop or
  silently renumber existing behavior IDs.
- PR 15 adds `darkula-cross-source` v1 (BlackGate reused, AccessBay,
  NightLeak, ShadowTalk) with additive `FW-AB-*`/`FW-NL-*`/`FW-ST-*`/`FW-XS-*`
  behavior IDs. Mirror behavior is recon-observable only: no Tor rotation,
  no new production endpoint resolver, and no automatic `SourceEndpoint`
  merging.

## Evaluation and observability rules (PR 15, durable)
- Evaluation is evaluator-side and outside the production pipeline:
  `src/darkula/evaluation/` may consume Fake World truth, but production
  domain/application/infrastructure/composition code must never import it.
  The static guard stays enforced.
- Truth is evaluator-side only: `FakeWorldTruth`, expected answers, and
  rubric answers never enter prompts, production contexts/results, or
  composition. Evaluators compare bounded production outputs with versioned
  truth after execution; hidden truth is used only for leakage detection,
  never required for recall.
- Evaluation identity is distinct from production identity: case, run,
  invocation, scenario/version, evaluator/version, and model identity are all
  explicit; a semantic change requires a version bump, and repeated
  experiments retain distinct run IDs with stable semantic case IDs.
- Evaluation reuses delivered production contracts
  (ReconAgent/Coordinator, semantic/geographic/relationship extraction,
  SourceAnalysisService/SourceAnalyst, `LlmClient`, `AgentObservability`).
  Never create eval-only production agents, extractors, crawler, a second LLM
  abstraction, or an eval PostgreSQL schema.
- Scoring is deterministic and versioned (P/R/F1, exact counts, categorical
  correctness, grounding validity, rubric coverage); no LLM-as-judge and no
  arbitrary universal threshold. Gating is explicit and caller-supplied.
- Live-model evaluation is explicit and non-CI by default: ordinary
  `--qa`/`--sec`/`--intg` builds and CI never require live-model or SaaS
  credentials; `./build.sh --eval` is the only opt-in entry point; a requested
  live run must fail fast rather than silently use `FakeLlmClient`.
- Eval artifacts are bounded and safe: never persist unrestricted bodies,
  prompts, outputs, credentials, secrets, or hidden reasoning; a
  partial/cancelled run must never be recorded as completed.
- Agent observability reuses the existing content-safe, fail-open
  `AgentObservability` SPI. Provider SDK types stay infrastructure-only;
  backend failures never alter application work or swallow cancellation;
  missing backend configuration fails fast (no silent no-op).

## Telemetry export and hardening rules (PR 16, durable)
- Operational telemetry remains OpenTelemetry behind the existing
  `configure_telemetry`/`TelemetryRuntime` composition; never add a rival
  telemetry abstraction, prometheus/jaeger/vendor client in application or
  domain code, or a second logging architecture.
- OTLP export is opt-in via typed settings (`telemetry.export`,
  `telemetry.otlp_endpoint`, bounded `timeout_seconds`/`metric_interval_seconds`);
  there is no arbitrary header map or secret-bearing endpoint. Missing/invalid
  OTLP configuration fails fast. Exporter/SDK types stay in `darkula/telemetry/`.
- Telemetry is fail-open and content-free: exporter/backend failure never
  changes domain behavior, causes duplicate work, or triggers business retries;
  prompts, content, credentials, cookies, URIs, object keys, hashes, hidden
  truth, and high-cardinality labels are never exported. Cancellation semantics
  are unchanged.
- The local Collector/Jaeger/Prometheus stack is an integration backend only
  (Darkula-owned, `darkula.owned=true`, pinned images, explicit prefix-`3` host
  ports, no prune, fail closed on foreign/occupied resources). It is never a
  production telemetry deployment.
- `./build.sh --intg` installs cleanup before provisioning, cleans only
  positively-verified Darkula-owned resources, and preserves the original
  non-zero exit status. Migrations `0001`–`0008` remain byte-identical
  (SHA-256 pinned); no new migration without an approved STOP. Do not add a
  PR-16-specific pipeline/orchestrator or a production fault-injection switch.

## Local Podman infrastructure
- Darkula owns only Podman resources explicitly provisioned for Darkula. Containers, pods, networks, volumes, and other named Podman resources must use an unambiguous `darkula` namespace/prefix. Darkula scripts, tests, cleanup commands, and developer tooling must never discover, stop, remove, recreate, prune, or otherwise control resources belonging to another application.
- An exact Darkula-looking name does not establish ownership. Every mutable/removable Darkula container or volume must carry a `darkula.owned=true` label, applied at creation and positively verified from exact-resource metadata (never inferred from name, mounts, ports, or timestamps) before reuse, mutation, or deletion. Unlabeled, wrongly labeled, or unverifiable Darkula-named resources are foreign/unknown and must fail closed — never auto-adopted, relabeled, recreated, or deleted.
- Do not use broad Podman cleanup/discovery commands whose selection could include non-Darkula resources. Select resources by exact Darkula-owned names or equally strict Darkula-owned labels. Never use global prune operations from Darkula automation.
- Darkula's local host-port namespace uses application prefix `3`. Keep the standard service port inside the Podman network/container, and publish the corresponding Darkula-prefixed port on the host. Example: PostgreSQL container port `5432` is published as host port `35432` (`35432:5432`); Redpanda container Kafka port `9092` is published as host port `39092` (`39092:9092`).
- Apply the prefix consistently to every host-published Darkula service port. Do not change the service's internal/container port merely to obtain host isolation. Before assigning a new published port, document the standard container port and derive the Darkula host port from the prefix-`3` convention rather than choosing an arbitrary free port.
- Container-to-container communication must use the normal service/container ports on the Darkula Podman network, not the prefixed host ports. The prefix exists to isolate host bindings between independently running local application stacks.
- A host-port collision or a pre-existing non-Darkula Podman resource is not permission to modify or remove that resource. Fail safely and report the conflict instead.
- Infrastructure integration tests must create, inspect, and clean up only the Darkula resources they own. Cleanup must be safe when other unrelated Podman stacks are running concurrently.

## Testing
Unit tests are fast and deterministic. Scenario/integration tests reuse Fake World scenarios and normally fake at infrastructure/LLM boundaries, not by replacing the behavior under test. Live-model behavior belongs in evaluations. Add infrastructure integration coverage for PostgreSQL, Redpanda, ObjectStore adapters, OTEL export, and sandbox boundaries as those components arrive. The crawler integration suite (`tests/integration/crawler/`, via `./build.sh --intg`) exercises the real CrawlerController → PodmanSandbox → runtime container → Playwright/Chromium → HTTP → Fake World stack; its fixtures fail closed when provisioned resources are missing and create/remove only Darkula-owned resources.

## Tooling and quality gates
Darkula is a Python 3.14 project managed by `uv` with a `src/` layout. Reproduce the environment with `uv sync --locked` (committed `uv.lock`). The repository-owned quality path is:

```text
./build.sh --qa      ruff format --check, ruff check, strict mypy, pytest (coverage >= 85%)
./build.sh --sec     bandit source scan + pip-audit
```

`uv run pytest` runs the same unit suite. Async tests use pytest-asyncio strict mode (`@pytest.mark.asyncio`). Pre-commit runs fast lint/format hooks only; it is not a substitute for `./build.sh --qa`. PR CI (`.github/workflows/ci.yml`) executes the same QA and security commands under Python 3.14. Do not add infrastructure dependencies (aiokafka, boto3, psycopg, langchain, fastapi, ...) without an explicit PR scope that requires them; application contracts must stay provider-neutral.

## ATI reuse
Agentic Threat Investigator (ATI) is an implementation reference for compatible patterns including configuration layering, DataStream, LlmClient/FakeLlmClient, OTEL conventions, and eval/test fixture reuse. Darkula must remain self-contained: inspect fresh ATI main before deliberately reusing a pattern; do not create an undocumented runtime dependency on ATI.
