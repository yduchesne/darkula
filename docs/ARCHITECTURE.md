# Darkula Architecture

## Purpose and scope
Darkula v0.1 is a generic secure framework for discovering sources, qualifying them, collecting them repeatedly, normalizing hostile content, extracting structured facts, and assessing source value. Multi-tenancy and product-specific commercial features are intentionally deferred.

## Primary flow
```text
SourceCandidate -> Coordinator -> ReconAgent -> ReconAssessment
                                      |
                               qualify/promote
                                      v
                                    Source
                                      |
                              CollectionPolicy
                                      |
                           SourceCollectionService
                                      |
                                 CrawlRequest
                                      v
                         Crawler Worker/Controller
                                      |
                         [disposable sandbox]
                                      |
                                CrawlResult
                                      v
                              Normalization
                                      |
                              NormalizedContent
                                      v
                            Content Extraction
                                      |
                              ExtractionResult
                                      v
                               SourceAnalyst
                                      |
                              SourceAssessment
```

The Coordinator owns final reconnaissance/crawling decisions; agents recommend and provide evidence rather than silently mutating lifecycle state.

## Responsibilities
**ReconAgent** reasons about candidate identity, accessibility, structure, useful content, navigation, collection feasibility, overlap, and whether more reconnaissance is needed.

**SourceCollectionService** deterministically turns a Source and CollectionPolicy into bounded collection work. It is not an agent.

**Crawler** executes hostile retrieval/traversal behind a sandbox boundary. A trusted worker/controller should hold infrastructure credentials and provide a narrow contract to disposable sandbox execution.

**Content Extraction** extracts entities, observables/IOCs, geography, topics/classifications, and relationships. Deterministic and model-backed extractors may coexist. Extraction is not source analysis.

**SourceAnalyst** consumes source metadata, collection history, normalized/extracted facts, and prior assessments to create source-level assessments. It does not browse directly.

## Data plane
```text
PostgreSQL        DataStream                 ObjectStore
structured        async commands/events      raw/large artifacts
domain state      + durable references       screenshots/PDFs/etc.
    ^                  ^                          ^
    |                  |                          |
    +---------------- Darkula --------------------+
```
PostgreSQL is the structured system of record. DataStream is an application-owned producer/consumer abstraction; Redpanda/Kafka is the initial adapter. ObjectStore is an application-owned streaming artifact interface with local filesystem, S3, R2-compatible, and in-memory/test implementations.

Messages are small, versioned, correlated, and normally reference persisted state/artifacts. Assume at-least-once delivery and idempotent consumers. Use transactional outbox semantics to avoid committed state without corresponding events.

Commands express requested work (for example crawl/recon/extraction/analysis requested). Events express completed facts (for example crawl completed, content normalized/extracted, source assessed). Exact topics and schemas are TBD.

## Async I/O concurrency
Darkula should overlap independent I/O waits when doing so improves throughput or latency. For bounded sets of independent I/O operations, prefer asynchronous concurrency and use `asyncio.gather()` where it is a natural fit. Concurrency must always be explicitly bounded, for example by bounded batches, a semaphore, or a bounded worker/task pool; do not create one unbounded task per discovered URL, artifact, provider call, database operation, or other potentially large input.

Concurrency is an optimization subject to correctness. Keep work serialized, or apply a stricter concurrency policy, when ordering, transaction boundaries, external rate limits, resource budgets, backpressure, or other invariants require it. Concurrent code must have explicit error and cancellation behavior and must not leave orphaned background tasks.

## SPIs and adapters
Foundational Darkula-owned interfaces include persistence (DarkulaSpi or equivalent), DataStream, ObjectStore, LlmClient, and AgentObservability. External framework/provider types must not leak through them.

LlmClient supports structured, schema-validated model interactions. FakeLlmClient is the deterministic test boundary; LangChainLlmClient is an intended production adapter.

AgentObservability abstracts AI-specific observability semantics so LangSmith, Langfuse, and future providers are replaceable. OpenTelemetry itself is the selected operational telemetry standard and is not hidden behind a generic observability SPI.

## Security boundary
Hostile network interaction and dangerous parsing/rendering should occur inside the strongest practical sandbox boundary. Normalized content remains untrusted. SourceAnalyst and ordinary application code receive constrained artifacts/data, not arbitrary browsing capability. See SECURITY.md.

## Configuration and composition
Pydantic Settings provides typed configuration. Profiles/layers resolve centrally, with non-empty environment variables as the ultimate override. Composition code selects SPI implementations once. See CONFIGURATION.md.

## Observability
Application telemetry uses OTEL SDK/API -> OTEL Collector -> Prometheus (metrics), Loki (logs), Jaeger (traces). Grafana is the expected visualization layer. Agent observability correlates with OTEL through trace/run/domain identifiers. See OBSERVABILITY.md.

## PR 2 update — executable foundation

PR 2 delivered the Python project/tooling foundation and the foundational contracts named above:

- Python 3.14 project managed by `uv` (`pyproject.toml`, committed `uv.lock`, `src/` layout), gated by Ruff, strict mypy, pytest (strict async), coverage >= 85%, `build.sh`, pre-commit, and pull-request CI (`./build.sh --qa`, `./build.sh --sec`).
- Foundational interface modules: `darkula.app.llm` (`LlmClient`), `darkula.app.data_stream` (`DataStream`), `darkula.app.object_store` (`ObjectStore`), `darkula.app.persistence` (`UnitOfWork`/`DarkulaSpi`), `darkula.app.agent_observability` (`AgentObservability`), plus `darkula.domain.identifiers` and the `darkula.config.settings` skeleton and `darkula.telemetry.decorators` contract.

All concrete adapters (Redpanda, PostgreSQL, S3/R2, LangChain, LangSmith/Langfuse, crawler) remain future work behind these interfaces.

# PR 3 update — configuration, observability, and deterministic fakes

PR 3 made the in-process foundational runtime usable and deterministically testable without any real infrastructure:

- **Configuration** (`darkula/config/loader.py` + shipped TOML under `config/`): exact precedence `defaults < base < profile < local < non-empty environment`; env applied last via Pydantic source reordering; empty env is a no-op; deep merge with wholesale scalar/list replacement; fail-closed bounded errors; secret-like diagnostic redaction. Shipped profiles `development` (default), `test`, `production` (loads; `production` selections fail fast at composition until PR 5/8).
- **Deterministic fakes** (real interfaces, no parallel architecture): `darkula/testing/fake_llm.py` (`FakeLlmClient` implementing the untouched `_generate_structured` hook with scripted FIFO/factory/default outcomes, exact call recording, fail-closed unscripted calls) and `darkula/testing/fake_data_stream.py` (`FakeDataStream` with per-lane monotonic positions, per-consumer explicit ack, polling that never acknowledges, scripted failures, and a replay seam).
- **ObjectStore implementations** (`darkula/infrastructure/object_store/`): `InMemoryObjectStore` (streaming, instance-isolated, SHA-256 representation hash) and `LocalFileObjectStore` (streaming temp-file + atomic replace, bounded read chunks, root confinement with traversal/symlink-escape rejection, frozen missing semantics).
- **Observability** (`darkula/telemetry/`): `traced`/`timed`/`counted` now emit OTEL via the SDK (`opentelemetry-sdk` pinned), with seconds `.duration` histograms, bounded outcome attributes, no argument/result capture, cancellation-propagation, and no-op-safe unconfigured behavior. New support modules `attributes.py`, `tracing.py`, `metrics.py`, `setup.py`; telemetry tests use in-memory SDK components only.
- **Composition** (`darkula/composition.py`): narrow central composition of only delivered implementations; selecting unavailable production drivers (Redpanda, S3/R2, LangSmith/Langfuse, provider LLM) raises `UnavailableDriverError` — never a silent fake substitution.

PR 3 adds no real infrastructure, no `--intg`, no OTLP exporters, and no vendor dependencies beyond `opentelemetry-sdk`.

## Intentionally undecided in PR 1
Exact Python package layout; PostgreSQL schema/stored-function strategy; stream topic names and serialization; crawler/browser/Tor technologies; sandbox technology; object-key layout; exact extraction ontology; scheduling implementation; deployment topology; detailed secret backend; exact provider selection; and multi-tenancy.
