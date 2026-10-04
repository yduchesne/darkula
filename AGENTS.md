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
Unit tests are fast and deterministic. Scenario/integration tests reuse Fake World scenarios and normally fake at infrastructure/LLM boundaries, not by replacing the behavior under test. Live-model behavior belongs in evaluations. Add infrastructure integration coverage for PostgreSQL, Redpanda, ObjectStore adapters, OTEL export, and sandbox boundaries as those components arrive.

## Tooling and quality gates
Darkula is a Python 3.14 project managed by `uv` with a `src/` layout. Reproduce the environment with `uv sync --locked` (committed `uv.lock`). The repository-owned quality path is:

```text
./build.sh --qa      ruff format --check, ruff check, strict mypy, pytest (coverage >= 85%)
./build.sh --sec     bandit source scan + pip-audit
```

`uv run pytest` runs the same unit suite. Async tests use pytest-asyncio strict mode (`@pytest.mark.asyncio`). Pre-commit runs fast lint/format hooks only; it is not a substitute for `./build.sh --qa`. PR CI (`.github/workflows/ci.yml`) executes the same QA and security commands under Python 3.14. Do not add infrastructure dependencies (aiokafka, boto3, psycopg, langchain, fastapi, ...) without an explicit PR scope that requires them; application contracts must stay provider-neutral.

## ATI reuse
Agentic Threat Investigator (ATI) is an implementation reference for compatible patterns including configuration layering, DataStream, LlmClient/FakeLlmClient, OTEL conventions, and eval/test fixture reuse. Darkula must remain self-contained: inspect fresh ATI main before deliberately reusing a pattern; do not create an undocumented runtime dependency on ATI.
