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

**Crawler** is the Darkula application-facing capability for bounded source inspection and collection. Its implementation is split across the trust boundary: a trusted `CrawlerController` validates and orchestrates work, while a minimal `CrawlerRuntime` performs hostile network interaction and rendering inside a disposable sandbox. The application-facing Crawler contract remains `crawl(CrawlRequest) -> CrawlResult`; callers do not control the sandbox or browser directly.

**Sandbox** is a first-class Darkula SPI independent of crawler semantics. It executes an allow-listed workload subject to explicit resource, network, filesystem, credential, time, input, and output constraints. The initial production/local adapter is expected to be `PodmanSandbox`; tests above this boundary may use `FakeSandbox`. The sandbox contract must not become a generic remote-shell API.

**Content Extraction** extracts entities, observables/IOCs, geography, topics/classifications, and relationships. Deterministic and model-backed extractors may coexist. Extraction is not source analysis.

**SourceAnalyst** consumes source metadata, collection history, normalized/extracted facts, and prior assessments to create source-level assessments. It does not browse directly.

## Reconnaissance, search, and controlled browsing

Reconnaissance separates discovery from hostile-source inspection. A ReconAgent may be implemented as a LangChain DeepAgent, but it remains in the trusted Darkula process and depends on Darkula-owned capabilities rather than receiving direct browser control.

```text
                         TRUSTED DARKULA

                           Coordinator
                               |
                           ReconAgent
                          (DeepAgent)
                          /        \
                         /          \
               WebSearchProvider   Crawler SPI
                  discovery            |
                                 CrawlerController
                                       |
                                   Sandbox SPI
                                       |
                         ===== SECURITY BOUNDARY =====
                                       |
                                 CrawlerRuntime
                                       |
                              Playwright/Chromium
                                       |
                                  HTTP / Tor
                                       |
                         ===== EXTERNAL WORLD =====
                                       |
                           real source / Fake World
```

The two ReconAgent capabilities have different purposes:

- **WebSearchProvider** discovers candidate sources, URLs, references, and public-web context through a search provider. Search results are observations, not authorization to crawl.
- **Crawler** inspects a specific source under an explicit bounded `CrawlRequest`. The ReconAgent may iteratively request inspection and reason over returned observations, but Darkula validates and authorizes each crawl. The agent is never given a raw Playwright/browser tool.

The expected reconnaissance loop is therefore `ReconAgent -> bounded crawl request -> CrawlerController -> Sandbox -> CrawlerRuntime -> bounded observation -> ReconAgent`. The Coordinator retains final lifecycle and work-authorization authority.

This agentic reconnaissance path is distinct from recurring collection. Once a candidate has become a managed Source with a CollectionPolicy, `SourceCollectionService` should normally drive deterministic bounded collection rather than require an agent to choose each navigation step.

## Crawler and sandbox execution model

The crawler intentionally straddles the trust boundary:

```text
TRUSTED                                      SANDBOXED
CrawlerController                           CrawlerRuntime
  validate CrawlRequest        -------->      execute navigation
  enforce application policy                 HTTP/browser/Tor
  construct sandbox policy                   hostile parsing/rendering
  start/terminate execution     <--------      bounded outputs/artifacts
  validate returned result
```

The sandbox runtime contains only the dependencies required for hostile-source interaction: the Darkula crawler runtime package, HTTP/browser support, Playwright/Chromium when required, Tor connectivity support when required, parsing/rendering dependencies, and minimal serialization/IPC. It does not contain agents, LLM clients, repositories, DataStream clients, ObjectStore clients, or general Darkula application credentials.

Sandbox policy is generic and capability-based rather than Darkula-service-specific. Every capability is denied unless explicitly granted for the execution. Policy is decomposed into orthogonal network, filesystem, resource, credential, runtime, and output constraints. Darkula services such as PostgreSQL, Redpanda, and ObjectStore are examples of resources that a crawler workload normally cannot reach; they are not special cases embedded in the Sandbox abstraction.

A sandbox execution receives only an allow-listed workload, its bounded input, explicit capability/policy data, and narrowly scoped source authentication material when required. It returns bounded observations/artifact handles and execution metadata. Application infrastructure access remains on the trusted side.

For network access, policy describes permitted destinations/protocols/ports, DNS behavior, proxies such as Tor, and applicable connection/traffic bounds. Everything not granted is denied. A Fake World crawler execution may therefore reach only its authorized Fake World endpoint; a future Tor crawl may be permitted to reach only a controlled Tor egress mechanism. A workload such as an offline document parser may receive no network capability at all.

Filesystem policy similarly grants only explicit inputs and bounded ephemeral writable storage over a read-only runtime, without host filesystem or container-engine access. Runtime policy requires an unprivileged execution posture with unnecessary Linux capabilities removed, no-new-privileges, and restricted device/namespace exposure where supported. Resource and output policies bound CPU, memory, processes, disk/tmp, open files where practical, execution time, artifact count, per-artifact size, aggregate output, and permitted output forms.

The Sandbox SPI owns disposable execution lifecycle: create, execute, collect bounded outputs, terminate/cancel, and destroy. Caller cancellation must terminate the workload and clean up rather than leave an orphaned browser/container. Concrete adapters may use Podman initially and may later use Docker, Kubernetes Jobs, gVisor, Firecracker, or a remote sandbox service without changing crawler semantics.

Crawler navigation policy and sandbox network policy are defense-in-depth controls. `CrawlRequest` defines what navigation Darkula authorized; the sandbox independently constrains what the workload can reach.

## PR 7 delivered mechanics

**One fresh container per execution (no reuse).** `PodmanSandbox.execute()` creates exactly one disposable container per execution with a derived `darkula-crawler-<execution-id>-<nonce>` name, runs the allow-listed workload over stdin with no shell, transfers bounded output over stdout, and removes the container on every terminal path (completion, workload failure, sandbox timeout, caller cancellation, resource-limit kill). Containers carry `darkula.owned=true` plus service/execution labels; preflight inspection and removal happen only when exact metadata confirms ownership (an unowned same-named resource is never adopted, relabeled, or removed). No broad Podman cleanup is used anywhere in Darkula tooling.

**Controller/runtime protocol.** `CrawlerController` and `CrawlerRuntime` communicate with a versioned, bounded JSON protocol (`runtime_protocol.v1`): the controller encodes a `RuntimeInput` (start URL, single authorized origin, optional synthetic credentials, page/request/depth/time budgets, excerpt bound) and the runtime encodes a `RuntimeOutput` (pages with depth/status/title/bounded excerpt, discovered same-origin links, request count, redirect count, status reason). The controller clamps caller budgets against trusted settings maxima, derives the least-capability sandbox policy from the request, validates every decoded field before it becomes a `CrawlResult`, and never lets hostile raw bodies enter telemetry.

**Network mechanism.** The sandbox joins a Darkula-owned isolated internal network (`darkula-intg`, internal subnet, Darkula-prefixed host ports elsewhere untouched). The internal bridge provides no outbound route beyond the bridge: containers on it can be reached by name by other members, but it has no egress to the host, other Podman networks, or the wider Internet. Enforcement is therefore below workload logic and below origin checks in the runtime: PostgreSQL/Redpanda/an unauthorized sibling (all on other networks), the internal gateway/host services, and link-local cloud metadata (`169.254.169.254`) are all unreachable. The runtime additionally only ever navigates the single controller-authorized origin, and discovered links outside that origin are dropped — defense in depth on top of topology.

**Programmatic crawling behavior.** The runtime performs a deterministic bounded BFS: sorted discovery order, depth/pages/requests/time budgets, one 503-retry with bounded wait (Retry-After honored and capped), 429s and login/logout-paths handled as data, fragments/non-HTTP/out-of-origin links dropped, and the login form submitted at most once. A protected target that bounces to the login form is re-observed once after authentication succeeds so the intended content is actually collected (the bounce and the post-login retry are both recorded). Content-Disposition attachments do not render; the runtime records a bounded first-bytes sample observation instead (no unbounded buffering; the sandbox tmpfs caps the browser's download temp file, which is then discarded).

**Output boundary.** PR 7 transfers only small structured JSON reference payloads. Raw HTML/PDF/download bodies stay inside the disposable container; the controller/sandbox surface never exposes host filesystem paths. Artifacts (screenshots, raw bodies) are PR 8 ObjectStore work.

**Credentials.** The runtime image and sandbox contain no Darkula application credentials, no LLM/DB/DataStream/ObjectStore clients, and no general secrets. The only authentication material that may cross the boundary is the narrowly scoped source credential of the specific crawl (BlackGate synthetic login), and a crawl without credentials sets the credentials flag denied.

## Data plane
```text
PostgreSQL        DataStream                 ObjectStore
structured        async commands/events      raw/large artifacts
domain state      + durable references       screenshots/PDFs/etc.
    ^                  ^                          ^
    |                  |                          |
    +---------------- Darkula --------------------+
```
PostgreSQL is the structured system of record. DataStream is an application-owned producer/consumer abstraction; Redpanda is the delivered adapter (aiokafka behind the Darkula boundary). ObjectStore is an application-owned streaming artifact interface with local filesystem, S3, R2-compatible, and in-memory/test implementations.

### PostgreSQL access principle
Production Python contains no database-behavior/data-access SQL. The production
persistence path is:

```text
repository -> fixed parameterized stored-function invocation -> versioned function -> database behavior
```

Application and domain code never execute SQL and reach persistence only
through the persistence/repository and UnitOfWork abstractions. All database
behavior — table access, joins, predicates, mutations, DDL, and transactions —
is owned by versioned PostgreSQL stored functions defined in versioned
database/migration artifacts. PostgreSQL infrastructure repositories may
contain only fixed, parameterized invocations of approved versioned stored
functions (`SELECT * FROM <name>_v<positive integer>(...)`): function names are
static constants, and every value is bound as a separate Psycopg parameter.
Dynamic SQL — f-strings, `.format()`, concatenation, `%`-formatting,
identifier interpolation, embedded values, or multiple statements — is
rejected by the positive static QA guard.

This rule separates the production persistence contract from database
implementation details and centralizes SQL behavior in PostgreSQL. Direct SQL
from Python is permitted in test code when it is useful for database setup or
cleanup, fixture maintenance, verification/assertions, fault injection, or
independent inspection of persisted state. Tests of the production persistence
path must still exercise the production repositories/stored functions;
test-only SQL must not become a second implementation of production
persistence behavior.

Messages are small, versioned, correlated, and normally reference persisted state/artifacts. Raw HTML, PDFs, and screenshots belong in ObjectStore, never in the stream. Delivery is **at-least-once**; consumers are durable-idempotent by stable `message_id`. State+event atomicity comes from a transactional outbox: a business mutation and its outbox record commit in one PostgreSQL transaction, and a bounded outbox publisher moves rows to the broker only after commit. Polling never acknowledges; a position is acknowledged only after the consumer's durable processing committed. Broker positions are transport state, never identity. Commands express requested work (for example crawl/recon/extraction/analysis requested). Events express completed facts (for example crawl completed, content normalized/extracted, source assessed). Per-`message_type` topic/schema layout remains TBD and owned by the feature PRs.

## Async I/O concurrency
Darkula should overlap independent I/O waits when doing so improves throughput or latency. For bounded sets of independent I/O operations, prefer asynchronous concurrency and use `asyncio.gather()` where it is a natural fit. Concurrency must always be explicitly bounded, for example by bounded batches, a semaphore, or a bounded worker/task pool; do not create one unbounded task per discovered URL, artifact, provider call, database operation, or other potentially large input.

Concurrency is an optimization subject to correctness. Keep work serialized, or apply a stricter concurrency policy, when ordering, transaction boundaries, external rate limits, resource budgets, backpressure, or other invariants require it. Concurrent code must have explicit error and cancellation behavior and must not leave orphaned background tasks.

## SPIs and adapters
Foundational Darkula-owned interfaces include persistence (DarkulaSpi or equivalent), DataStream, ObjectStore, LlmClient, AgentObservability, and the planned Crawler, Sandbox, and WebSearchProvider boundaries. External framework/provider types must not leak through them.

LlmClient supports structured, schema-validated model interactions. FakeLlmClient is the deterministic test boundary; LangChainLlmClient is an intended production adapter.

AgentObservability abstracts AI-specific observability semantics so LangSmith, Langfuse, and future providers are replaceable. OpenTelemetry itself is the selected operational telemetry standard and is not hidden behind a generic observability SPI.

## Security boundary
Hostile network interaction and dangerous parsing/rendering occur inside the strongest practical sandbox boundary. ReconAgent, SourceAnalyst, Coordinator, LlmClient, persistence, messaging, artifact storage, and ordinary application code remain outside that boundary. Normalized content remains untrusted. Agents receive constrained observations/data, not arbitrary browser or sandbox control. See SECURITY.md.

## Local infrastructure isolation
Darkula's locally published service ports use application prefix `3` to avoid collisions with independently running application stacks. The prefix applies only to host-published ports: services retain their standard ports inside containers and on the Darkula Podman network. For example, PostgreSQL uses container port `5432` and is published on the host as `35432` (`35432:5432`).

Apply this convention consistently to every Darkula service that publishes a host port. Derive the host port from the service's standard container port and the Darkula prefix rather than selecting an arbitrary available port. Container-to-container communication continues to use the standard service port. Darkula Podman resources must also be unambiguously Darkula-owned/namespaced; exact Darkula-looking names alone do not establish ownership — every mutable/removable container or volume carries a `darkula.owned=true` label, applied at creation and positively verified from exact-resource metadata before reuse, mutation, or deletion. Local tooling must fail safely on collisions and on unlabeled/wrongly labeled Darkula-named resources and must never modify or remove unrelated resources.

## Configuration and composition
Pydantic Settings provides typed configuration. Profiles/layers resolve centrally, with non-empty environment variables as the ultimate override. Composition code selects SPI implementations once. See CONFIGURATION.md.

## Observability
Application telemetry uses OTEL SDK/API -> OTEL Collector -> Prometheus (metrics), Loki (logs), Jaeger (traces). Grafana is the expected visualization layer. Agent observability correlates with OTEL through trace/run/domain identifiers. See OBSERVABILITY.md.

## PR 2 update — executable foundation

PR 2 delivered the Python project/tooling foundation and the foundational contracts named above:

- Python 3.14 project managed by `uv` (`pyproject.toml`, committed `uv.lock`, `src/` layout), gated by Ruff, strict mypy, pytest (strict async), coverage >= 85%, `build.sh`, pre-commit, and pull-request CI (`./build.sh --qa`, `./build.sh --sec`).
- Foundational interface modules: `darkula.app.llm` (`LlmClient`), `darkula.app.data_stream` (`DataStream`), `darkula.app.object_store` (`ObjectStore`), `darkula.app.persistence` (`UnitOfWork`/`DarkulaSpi`), `darkula.app.agent_observability` (`AgentObservability`), plus `darkula.domain.identifiers` and the `darkula.config.settings` skeleton and `darkula.telemetry.decorators` contract.

PR 5 delivers the Redpanda DataStream adapter and the reliable-messaging foundation; PostgreSQL persistence shipped in PR 4; PR 8 delivers S3/R2-compatible ObjectStore storage and the normalization/content boundary. LangChain, LangSmith/Langfuse, and agent/crawler-collection TBD items remain future work behind these interfaces.

# PR 3 update — configuration, observability, and deterministic fakes

PR 3 made the in-process foundational runtime usable and deterministically testable without any real infrastructure:

- **Configuration** (`darkula/config/loader.py` + shipped TOML under `config/`): exact precedence `defaults < base < profile < local < non-empty environment`; env applied last via Pydantic source reordering; empty env is a no-op; deep merge with wholesale scalar/list replacement; fail-closed bounded errors; secret-like diagnostic redaction. Shipped profiles `development` (default), `test`, `production` (loads; `production` selections fail fast at composition until PR 5/8).
- **Deterministic fakes** (real interfaces, no parallel architecture): `darkula/testing/fake_llm.py` (`FakeLlmClient` implementing the untouched `_generate_structured` hook with scripted FIFO/factory/default outcomes, exact call recording, fail-closed unscripted calls) and `darkula/testing/fake_data_stream.py` (`FakeDataStream` with per-lane monotonic positions, per-consumer explicit ack, polling that never acknowledges, scripted failures, and a replay seam).
- **ObjectStore implementations** (`darkula/infrastructure/object_store/`): `InMemoryObjectStore` (streaming, instance-isolated, SHA-256 representation hash) and `LocalFileObjectStore` (streaming temp-file + atomic replace, bounded read chunks, root confinement with traversal/symlink-escape rejection, frozen missing semantics).
- **Observability** (`darkula/telemetry/`): `traced`/`timed`/`counted` now emit OTEL via the SDK (`opentelemetry-sdk` pinned), with seconds `.duration` histograms, bounded outcome attributes, no argument/result capture, cancellation-propagation, and no-op-safe unconfigured behavior. New support modules `attributes.py`, `tracing.py`, `metrics.py`, `setup.py`; telemetry tests use in-memory SDK components only.
- **Composition** (`darkula/composition.py`): narrow central composition of only delivered implementations; selecting unavailable production drivers (S3/R2, LangSmith/Langfuse, provider LLM) raises `UnavailableDriverError` — never a silent fake substitution. PR 5 made the Redpanda selection compose the real adapter (see the PR 5 update below).

PR 3 adds no real infrastructure, no `--intg`, no OTLP exporters, and no vendor dependencies beyond `opentelemetry-sdk`.

# PR 4 update — source domain and PostgreSQL persistence foundation

PR 4 delivered Darkula's first real structured-domain persistence path:

- **Executable source-domain model** (`darkula/domain/source.py`): `SourceCandidate`, `SourceCandidateEventHistory` (append-only), `Source` (a logical managed source, not a URL), `SourceEndpoint` (rotation does not change Source identity), `ReconAssessment` (immutable), and `SourceAssessment` (immutable, time-windowed), with the frozen lifecycle enums and one bounded `Confidence` representation. Times are timezone-aware UTC; structured JSON fields are validated documents that never embed credentials; history and assessment records are immutable in production.
- **Repository contracts through the existing UnitOfWork** (`darkula/app/repositories.py`): aggregate-oriented `SourceCandidateRepository` and `SourceRepository` exposed via new abstract `source_candidates`/`sources` properties on `UnitOfWork`. One UoW = one real transaction; explicit commit; uncommitted/exceptional/cancelled exit rolls back; commit failure never claims committed.
- **Versioned SQL artifacts** (`migrations/0001_initial.sql`): schema, constraints, deterministic indexes (candidate_id + occurred_at + tie-breaker, etc.) and the versioned `*_v1` stored functions that OWN every production persistence operation. Production repository Python contains **no database-behavior/data-access SQL**: the only SQL permitted is the fixed, parameterized stored-function invocation (the driver's function-call mechanism), and all table access, joins, predicates, mutations, and other database behavior live in versioned database artifacts. A positive static QA guard validates that the production PostgreSQL persistence package contains only fixed, parameterized invocations of versioned stored functions (PR 5B) — it is not a keyword denylist.
- **Async PostgreSQL adapter** (`darkula/infrastructure/persistence/postgresql/`): `PostgresDarkulaSpi` (owns the bounded psycopg `AsyncConnectionPool`, lazy `start()`/`close()`), `PostgresUnitOfWork` (one pooled connection + one explicit transaction, cancellation-safe rollback, bounded closed-use errors), the two repositories (stored-function invocation + centralized result mapping + bounded error projection), `mapping.py`, and `errors.py` (SQLSTATE-classified: unique → `ConflictError`, FK/check/not-null → `IntegrityError`, operational/pool → `PersistenceUnavailableError`; never echoes driver text; `asyncio.CancelledError` always propagates unchanged).
- **Bounded persistence errors** (`darkula/app/persistence.py`): `PersistenceError` gains `NotFoundError`, `ConflictError`, `IntegrityError`, `PersistenceUnavailableError`, and `MappingError`.
- **Configuration/composition** (`config/`, `darkula/config/settings.py`, `darkula/composition.py`): `DatabaseSettings` grows connection/pool fields with fail-fast validation; `compose()` grows the lazy `PostgresDarkulaSpi`; no production fallback to fake persistence.
- **Darkula-owned Podman PostgreSQL** (`scripts/darkula_postgres.sh`): provisions/starts/stops/cleans exactly `darkula-postgres` (pinned image `docker.io/library/postgres:18.2`, `darkula.owned=true` label) and `darkula-postgres-data` (created with its own `darkula.owned=true` label since PR 5B); publishes the exact mapping host `35432` → container `5432`. Start and clean positively verify every existing Darkula-named container/volume from exact-resource metadata before reuse or deletion and fail closed on unlabeled, wrongly labeled, or unverifiable resources — no auto-adoption, relabeling, broad discovery, or prune (PR 5B hardening).
- **Migration tooling** (`scripts/darkula_migrate.py`): deterministic lexical application of versioned `migrations/NNNN_*.sql`, with a `darkula_schema_migrations` ledger table; this is operator/CI tooling only.
- **`./build.sh --intg`** plus a dedicated integration CI job: provisions PostgreSQL, applies migrations, runs the real-PostgreSQL suite (`tests/integration`, marker `integration`), and cleans up while preserving the original exit status. `./build.sh --qa` remains fully offline, fast, and deterministic.
- **Real-PostgreSQL integration suite** (`tests/integration/`): migrations (P1–P6), candidate (C1–C10), source (S1–S8), transactions/cancellation (T1–T6), and the canonical vertical slice — all through `PostgresDarkulaSpi → PostgresUnitOfWork → repository → stored function → PostgreSQL`; test-only SQL is restricted to setup/reset/independent assertion.

PR 4 adds **no** DataStream/Redpanda, no transactional outbox, no crawler/collection/agent/extraction work, no scheduler, no ORM, no API/UI, and no multi-tenancy. Everything here is PR 4-scoped per the roadmap.
# PR 5 update — DataStream/Redpanda and reliable messaging

PR 5 delivered Darkula's production messaging path with PostgreSQL remaining authoritative:

- **Deterministic versioned wire codec** (`darkula/infrastructure/data_stream/codec.py`): canonical UTF-8 JSON envelope v1 (`envelope_version`, `message_id`, `message_type`, `schema_version`, `occurred_at`, `payload`, optional `correlation_id`/`causation_id`/`routing_key`; absent optionals are omitted). Deterministic encoding (sorted keys, fixed separators, ASCII escaping), strict decoding that rejects malformed JSON, missing fields, invalid identifiers/timestamps, unsupported versions, and unknown top-level fields; bounded `MessageCodecError` never echoes wire/payload/identifiers. No schema registry.
- **Production Redpanda adapter** (`darkula/infrastructure/data_stream/redpanda.py`): `RedpandaDataStream(DataStream)` over the pure-Python asyncio `aiokafka` client (pinned `aiokafka>=0.14,<0.15`). Publish encodes + sends with routing key as broker key and W3C `traceparent`/`tracestate` in transport headers (never the payload); acks=`all`, flush-required broker acceptance, `PublishResult(message_count)`; partial batch acceptance is possible and never promised all-or-nothing. Poll maps `consumer_id` -> Kafka consumer group, returns at most `max_records` decoded records in deterministic partition/offset order, never commits; malformed records fail the poll boundedly and are never skipped/acknowledged. Acknowledgment commits per-lane high-water marks (`max(offset)+1`, Kafka's real partition-epoch semantics — not sparse per-position acks; the reliable consumer preserves safety by processing in order and acking only after commit). Provider types/exceptions stay inside the module; errors map to the existing bounded `PublishError`/`PollError`/`AcknowledgeError`/`LifecycleError`.
- **Transactional PostgreSQL outbox + durable idempotency** (`migrations/0002_message_outbox.sql`): immutable `0002` adds `message_outbox` (unique `message_id`, delivery state `published_at`, explicit `attempt_count`/`last_attempt_at`/claim-lease fields; broker partition/offset never persisted) and `processed_message` (unique `(stream_name, consumer_id, message_id)`; never an offset), with stored functions `outbox_append_v1` (duplicate-safe), `outbox_claim_v1` (bounded, `FOR UPDATE SKIP LOCKED`, deterministic order `created_at, outbox_id`, explicit lease), `outbox_mark_published_v1` (owner-scoped by claim id), and `processed_message_record_v1` (atomic check+record). Production Python contains only fixed parameterized invocations; the SQL-boundary QA guard covers the new modules.
- **Repositories through the existing UnitOfWork** (`darkula/app/repositories.py` + PostgreSQL adapters): `OutboxRepository` (append/claim/mark_published) and `ProcessedMessageRepository` (record) exposed as new `outbox`/`processed_messages` properties on `UnitOfWork`/`PostgresUnitOfWork`; controlled conflicts map to existing typed persistence errors.
- **Bounded outbox publisher** (`darkula/app/outbox.py`): claim in a short UoW -> commit -> broker publish outside any transaction -> mark successes in a short UoW -> commit. Failures keep rows retryable (explicit lease + attempt counters; no hidden retry loop — the caller owns cadence). Partial broker acceptance fails closed with `OutboxPublishError` (nothing half-marked); a crash after publish before mark may republish (consumers dedupe by `message_id`).
- **Reliable consumer primitive** (`darkula/app/consumer.py`): `ReliableConsumer`/`ReliableMessageHandler` implement poll -> UoW (durable dedupe by `(stream, consumer, message_id)`; first-time -> handler effect + outgoing outbox + processed marker atomically; duplicates -> no effect/no outgoing) -> commit -> acknowledge. Ack never precedes commit; cancellation propagates with rollback; no unbounded fan-out; retry cadence is owned by the caller (no hidden retries).
- **Settings/composition** (`config/settings.py`, `config/`, `composition.py`): `DataStreamSettings` gains only the required typed Redpanda fields (`bootstrap_servers`, `client_id`, bounded `poll_timeout_ms`, bounded `max_poll_records`); no arbitrary Kafka dict, no committed secrets, env stays the ultimate override (empty env = no-op). `REDPANDA` selection now composes `RedpandaDataStream`; the fake is never silently substituted.
- **OTEL** (adapter/publisher/consumer): bounded counters/histograms/spans for `datastream.publish/poll/acknowledge`, `outbox.claim/publish/mark_published`, and `consumer.process` (+ duplicate count); attributes limited to bounded `darkula.stream`/`darkula.consumer` names — never message_id, correlation_id, routing-key values, URIs, or payloads. Trace context is injected into broker headers on publish and extracted on poll; best-effort, never fails delivery; no baggage propagation.
- **Darkula-owned Podman Redpanda** (`scripts/darkula_redpanda.sh`): pinned `docker.io/redpandadata/redpanda:v24.3.8`, container `darkula-redpanda` labeled `darkula.owned=true`/`darkula.service=redpanda`, exact host mapping `39092` -> container Kafka port `9092`, advertised host-reachable `127.0.0.1:39092`; same fail-closed ownership verification as PostgreSQL (exact-resource inspect, no adoption/relabel/prune, foreign-port listener refusal with no fallback port). `./build.sh --intg` provisions PostgreSQL + Redpanda, applies migrations, runs the suite, and cleans only verified owned resources.
- **Integration slices**: V1 real-DataStream publish/poll/ack/re-poll + independent groups; V2 real PostgreSQL outbox -> OutboxPublisher -> real Redpanda -> production poll with mark-published-only-after-publication asserted; V3 duplicate durable processing (one effect across redelivery at different positions); V4 PR-5B real-Podman PostgreSQL container/volume ownership + `35432:5432`; V5 real-Podman Redpanda ownership + `39092:9092`.
- **Unit matrices**: DS1–DS16 (codec/adapter, offline via fake aiokafka), O1–O12 (outbox repositories/publisher), C1–C10 (reliable consumer), I1–I10 (Redpanda lifecycle via fake podman), plus telemetry emission tests. Unit QA remains offline.

PR 5 adds **no** crawler/sandbox, Fake World, collection, ReconAgent/Coordinator workflow, extraction/SourceAnalyst, concrete business consumers, ObjectStore changes, schema registry, DLQ framework, topic-per-event proliferation, multi-tenancy, or production deployment topology. Everything here is PR 5-scoped per the roadmap.

## Intentionally undecided in PR 1

Exact Python package layout; detailed PostgreSQL schema (PR 4); stream topic names and serialization; crawler/browser/Tor technologies; sandbox technology; object-key layout; exact extraction ontology; scheduling implementation; deployment topology; detailed secret backend; exact provider selection; and multi-tenancy.

# PR 8 update — normalization and artifact storage

PR 8 delivered the deterministic trusted-side content-ingestion boundary that
converts bounded crawler observations into durable normalized content plus
ObjectStore-backed artifacts, with independent provenance.

```text
Crawler (bounded observations)
   -> ContentObservation (Darkula-owned input DTO; section 13 mapper)
   -> normalization boundary (deterministic, non-LLM, versioned "text-v1")
   -> ObjectStore bytes (content-addressed, deduplicated)
   -> PostgreSQL normalized metadata/provenance (short UoW)
```

- **Domain** (`src/darkula/domain/content.py`): `ArtifactCompleteness`,
  `ArtifactKind`, `ContentArtifact`, `NormalizedContent`, and
  `ContentObservation`, with frozen identity distinctions (content ID /
  artifact ID / `ObjectKey` / `ContentHash` / provenance).
- **Normalization** (`src/darkula/app/normalization.py`): one
  `ContentNormalizer` capability (`DeterministicContentNormalizer`) that is
  deterministic and non-LLM, canonicalizes text line endings/control chars,
  hashes exact stored bytes, and resolves the stored artifact representation.
- **Artifact storage** (`src/darkula/app/artifacts.py`): content-addressed
  `sha256/<2>/<64>` keys and hash-first deduplication that verifies existing
  objects (never trust-a-key silently) and preserves distinct artifact
  records per representation. Reuses the existing ObjectStore SPI; no second
  blob SPI.
- **Orchestration** (`src/darkula/app/content.py`): `ContentIngestService`
  performs ObjectStore I/O outside any PostgreSQL transaction, then persists
  artifact metadata + normalized provenance atomically in a short unit of
  work; dedup never merges provenance; retried observations are idempotent
  on the provenance identity.
- **Persistence** (`migrations/0003_content.sql`): `content_artifact` and
  `normalized_content` tables plus versioned stored functions; production
  Python persists only provider-neutral metadata (full text/bytes live in
  ObjectStore, with a small bounded text preview in PostgreSQL).
- **S3-compatible adapter** (`src/darkula/infrastructure/object_store/s3.py`):
  `S3CompatibleObjectStore` serves both `s3` and `r2` drivers through
  endpoint/credentials configuration, over `boto3` 1.43.x (Python 3.14) with
  blocking SDK calls isolated via bounded `asyncio.to_thread`, single-PUT
  streaming (PR 8 artifacts are bounded), and provider errors mapped to
  bounded Darkula errors.
- **Security**: the sandbox never receives an ObjectStore client or S3/R2
  credentials; keys derive only from the SHA-256 representation hash (never
  hostile URI/filename); samples/excerpts are `SAMPLE`, never complete
  originals; normalization stays untrusted-by-design.

PR 8 adds **no** collection orchestration (PR 9), no extraction/analysis
(PR 11-14), no multi-tenancy, no live provider crawling, and no schema
registry. It performs no rearchitecting of the PR 7 crawler.

# PR 9 update — managed-source collection lifecycle

PR 9 connects the PR 7 crawler and the PR 8 content-ingestion boundary into
the deterministic, policy-authorized collection lifecycle:

```text
CollectionPolicy -> CollectionScheduler -> outbox -> DataStream
    -> CollectionWorker -> SourceCollectionService -> Crawler (PR 7)
        -> ContentIngestService (PR 8) -> ObjectStore + PostgreSQL
```

## Dominant invariant — transaction boundaries (PR 9 1.6)

PostgreSQL owns policy/run state, DataStream carries small references only,
and **no PostgreSQL transaction spans broker, crawler, sandbox, HTTP, or
ObjectStore I/O**:

```text
short UoW:  authorize/create/claim -> commit
Crawler.crawl() outside any UoW
ContentIngestService.ingest() outside the caller UoW
short UoW:  finalize (terminal state, counters) -> commit
```

The transactional outbox makes run admission atomic: the scheduler creates
the QUEUED run **and** appends the `collection.execute` outbox row in one
transaction (PR 9 1.8); `OutboxPublisher` publishes later, outside any
transaction.

## Why not the ReliableConsumer handler? (PR 9 1.7)

The PR 5 `ReliableConsumer` invokes its handler inside one open unit of
work (processed marker + effect + outgoing outbox commit atomically). Long
collection work (crawler, HTTP, ObjectStore) must never run inside an open
transaction, so PR 9 deliberately adds a collection-specific worker that
mirrors the same ordering at a different granularity:

```text
DataStream.poll
  -> short durable admission/dedup (processed marker + run read) -> commit
  -> SourceCollectionService.execute (claim -> crawl/ingest -> finalize)
  -> durable terminal state committed by the service
  -> DataStream.acknowledge
```

ReliableConsumer itself is unchanged: it remains the generic primitive for
short handlers; `SourceCollectionService.execute` is the collection runner.

## Identity and authorization (PR 9 1.1-1.4)

`Source` is logical identity, `SourceEndpoint` is a locator; `CollectionPolicy`
is reusable authorization/configuration belonging to the Source; `CollectionRun`
is one historical execution of the occurrence `(policy_id, scheduled_for)`.
`CollectionRunId` is the authoritative work identity — never a URI and never a
broker position. The run permanently stores `policy_revision` + the exact
frozen execution snapshot that explains its authorization, so policy edits
never rewrite run history (1.10, 1.11): new execution requires an ACTIVE
Source, ACTIVE policy, and ACTIVE owned endpoints at admission; once RUNNING
the frozen snapshot governs.

## Scheduler and worker (PR 9 1.5, 1.9, 1.18)

- `CollectionScheduler.schedule_due(now, limit)` admits the earliest due
  active policy occurrence atomically (row lock `FOR UPDATE SKIP LOCKED` +
  `UNIQUE (policy_id, scheduled_for)`), advances the policy clock past
  `now`, and appends the IDs-only `collection.execute` v1 command
  (`collection_run_id`, `source_id`, `policy_id` — never URIs/content/
  credentials) to the outbox in the same transaction. Concurrent schedulers
  produce exactly one run and one command per occurrence.
- `SourceCollectionService.execute(run_id)` claims a QUEUED run into RUNNING
  with an execution lease, closes the transaction, crawls each frozen
  endpoint (deterministic `CrawlRequest` identity via run/endpoint/attempt
  namespace UUID), ingests through PR 8, and finalizes a terminal state in
  a new short transaction. Failure summaries are the bounded sanitized
  vocabulary; raw exception text is never persisted.
- `CollectionWorker.process_once` polls, performs the short durable
  admission, executes, and acknowledges only after durable terminal state.
  At-least-once semantics are preserved: terminal runs never recrawl;
  RUNNING runs with unexpired leases are never crawled concurrently; expired
  leases are reclaimed as the same run within `max_run_attempts`; crashed
  pre-terminal runs are never acknowledged.
