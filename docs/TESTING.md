# Darkula Testing Strategy

## Commands

The deterministic local quality gate:

```text
uv sync --locked
./build.sh --qa       ruff format check, ruff lint, strict mypy, unit tests, coverage >= 85%
./build.sh --intg     Darkula-owned Podman PostgreSQL (35432:5432) + Redpanda (39092:9092),
                      migrations, real-infrastructure integration tests
./build.sh --sec      bandit source scan + pip-audit dependency audit
uv run pre-commit run --all-files   optional local dev gate
```

Async tests use pytest-asyncio **strict mode** (`@pytest.mark.asyncio`);
async fixtures use `pytest_asyncio.fixture`. The unit suite (`--qa`) runs
fully offline and deterministically (integration tests carry the
`integration` marker and are deselected by default). Coverage measures the
`darkula` package with a hard `fail_under = 85` gate in CI and locally.

## Infrastructure integration (PR 4/PR 5)

`./build.sh --intg` runs the real-infrastructure integration suite:

1. verifies Podman availability;
2. starts/provisions **only** Darkula-owned resources:
   `scripts/darkula_postgres.sh start` (container `darkula-postgres` with
   `darkula.owned=true`, pinned `docker.io/library/postgres:18.2`, volume
   `darkula-postgres-data` created with its own `darkula.owned=true` label,
   exact host mapping `35432` → container `5432`) and
   `scripts/darkula_redpanda.sh start` (container `darkula-redpanda` with
   `darkula.owned=true`/`darkula.service=redpanda`, pinned
   `docker.io/redpandadata/redpanda:v24.3.8`, exact host mapping
   `39092` → container Kafka port `9092`, advertised host-reachable
   `127.0.0.1:39092`);
3. waits for `pg_isready` and `rpk cluster info`; applies migrations
   (`scripts/darkula_migrate.py`, ledger `darkula_schema_migrations`);
4. runs `uv run pytest tests/integration -m integration --no-cov -q`;
5. cleans up exactly the Darkula-owned resources while preserving the
   original exit status (Redpanda first, then PostgreSQL).

PR 7 extends the same `--intg` flow with crawler infrastructure:
`scripts/darkula_crawler.sh start` builds the pinned crawler runtime image
(`localhost/darkula-crawler-runtime:1.63.0`, cached when the tag is
unchanged) and the stdlib-only Fake World image
(`localhost/darkula-fakeworld-intg:1`), creates the Darkula-owned internal
network `darkula-intg` (internal subnet, `darkula.owned=true` label, no
host port), and runs the Fake World HTTP service container
(`darkula-fake-world-intg`, port 8080 inside the network, **no** host
port). `darkula_crawler.sh clean` removes exactly the Darkula-owned crawler
resources; it never touches other applications' resources.

Both lifecycle tools fail fast if their host port (`35432` or `39092`) is
occupied by a non-Darkula listener, if a resource bears a Darkula name
without Darkula labels, or if Podman cannot run; they never prune, never
touch foreign resources, and never choose another port. Developers wanting
a persistent local instance may run the `start|stop|clean` subcommands
manually; `./build.sh --intg` always tears its own resources down.

### PostgreSQL SQL boundary guard (PR 5B)

Production Python contains no database-behavior/data-access SQL. A positive
static guard (`tests/unit/infrastructure/persistence/postgresql/test_sql_boundary.py`)
scans the whole production PostgreSQL persistence package and allows only
fixed, parameterized invocations of approved versioned stored functions:

```text
SELECT * FROM <name>_v<positive integer>(<zero or more %s placeholders>)
```

Whitespace/newlines may vary; everything else is rejected (SQL1–SQL13):
direct table `SELECT`s, `INSERT`/`UPDATE`/`DELETE`, DDL/`TRUNCATE`, CTEs,
transaction SQL, non-versioned function names, embedded literal arguments,
f-string SQL, concatenated/`.format()`/`%`-formatted SQL, and multiple
statements. SQL used by test-only code outside the production package is
unaffected (SQL14). The guard inspects the actual production package rather
than a fixed file list, so new repository modules are covered automatically.

### Podman lifecycle ownership (PR 5B)

`scripts/darkula_postgres.sh` never treats an exact Darkula-looking name as
proof of ownership. Newly created `darkula-postgres-data` volumes receive
`darkula.owned=true` at creation; `start` positively verifies the exact
container and exact volume metadata before reuse/provisioning; `clean`
preflights every existing Darkula-named resource before any destructive
action; and any unlabeled, wrongly labeled, or unverifiable resource fails
closed — no auto-adoption, no relabeling, no deletion.

Deterministic unit tests (`tests/unit/scripts/test_darkula_postgres_script.py`)
run the real script against a fake `podman`/`ss` earlier on `PATH` and cover
the P1–P12 lifecycle matrix (create labeled volume, reuse owned volume, refuse
unlabeled/wrongly labeled volume, preflight `clean`, safe no-op `clean`,
foreign-port refusal, foreign-resource non-interference, fail-closed inspect
failures, idempotent repeated start/clean) without a live Podman daemon. Real
`--intg` additionally asserts both ownership labels and the `35432:5432`
mapping from exact-resource Podman metadata. The same pattern covers Redpanda
(`tests/unit/scripts/test_darkula_redpanda_script.py`, I1–I10) and the real
`--intg` run asserts the Redpanda ownership label and the `39092:9092` mapping.

### Real messaging vertical slices (PR 5)

- V1 (`tests/integration/datastream/test_redpanda_datastream.py`): real
  Redpanda `start -> publish -> poll -> verify -> acknowledge -> poll again`,
  plus independent consumer groups and full message-semantics round-trip;
- V2 (`tests/integration/datastream/test_outbox_publisher.py`): real
  PostgreSQL outbox -> OutboxPublisher -> real Redpanda -> production poll;
  asserts the same `message_id` and that `published_at` is only set after
  publication;
- V3 (`tests/integration/datastream/test_durable_consumer.py`): real
  PostgreSQL + Redpanda; same logical message delivered twice (different
  positions) produces exactly one durable effect for one consumer identity;
- V4 (`tests/integration/persistence/test_podman_ownership.py`): real Podman
  asserts `darkula-postgres` and `darkula-postgres-data` carry
  `darkula.owned=true` and `35432:5432` (PR 5B regression);
- V5 (`tests/integration/datastream/test_podman_ownership.py`): real Podman
  asserts `darkula-redpanda` carries `darkula.owned=true` and `39092:9092`.

### PostgreSQL persistence tests

Production persistence tests (`tests/integration/persistence/`) exercise
the real path:

```text
PostgresDarkulaSpi -> PostgresUnitOfWork -> repository -> stored function -> PostgreSQL
```

Test-only SQL is permitted for setup/cleanup, fixture maintenance,
verification/assertions, fault injection, and independent DB-state
inspection; it must not become a second production persistence path.
Integration fixtures reset table state between tests (advisory-locked
`TRUNCATE`) so order never matters. The migration matrix checks that a
fresh/empty database provisions tables, functions, and constraints
correctly, and that the server is reached on host port `35432` while its
container port remains `5432`. The canonical vertical slice walks a
candidate through create → transition → recon, then a distinct Source
through create → endpoint → assessment, across real transactions.

## Principles
Testing must be deterministic by default, exercise real application/domain
code, and reuse realistic Fake World scenarios. Fake at external/variable
boundaries rather than replacing the behavior under test.

## Levels
### Unit
Fast tests without PostgreSQL, Redpanda, OTEL backend, browser/Tor, or live
LLM. Cover state transitions, policies, message mapping, normalization
helpers, deterministic extractors, geographic data handling, configuration
precedence, and similar logic.

### Component/scenario integration
Run real Darkula orchestration against Fake World data. Prefer
FakeLlmClient, FakeDataStream, InMemoryObjectStore/local test store, and
test persistence as appropriate. Exercise the real ReconAgent/SourceAnalyst
implementation while faking its LlmClient for deterministic agent paths.

### Infrastructure integration
Exercise real adapters: PostgreSQL, Redpanda DataStream, ObjectStore
implementations, OTEL Collector/export, and crawler sandbox/controller as
introduced (PR 4 onward).

Darkula infrastructure tests must use the prefix-`3` host-port namespace for every published service while retaining standard ports inside containers. PostgreSQL therefore publishes `35432:5432`. Test setup, discovery, teardown, and cleanup must select only explicitly Darkula-owned Podman resources and remain safe while unrelated local Podman stacks are running. A collision with a foreign resource or required host port must fail clearly rather than reconfigure, stop, or remove that resource or silently choose a different port.

### PostgreSQL persistence tests
Production persistence tests must exercise the real repository-to-stored-function path. Python test code may use direct SQL for test-only concerns such as database setup/cleanup, fixture maintenance, verification/assertions, fault injection, and inspecting database state independently of the production repository API. Keep such SQL in test/support code and do not use the exception to duplicate or replace production persistence behavior.

### End-to-end Fake World
Exercise collection -> normalization -> extraction -> source assessment
through production-like infrastructure without contacting real criminal
resources.

## PR 3 canonical fakes (reusable, offline)

- `darkula.testing.fake_llm.FakeLlmClient` — implements the real
  `LlmClient` hook (`_generate_structured`), never bypasses the public
  `generate_structured` validation. Scripted FIFO responses/exceptions,
  factory, fixed default, expected-model assertion, exact call recording;
  outcome precedence `queue -> factory -> default -> assertion failure`;
  unscripted calls fail closed; scripted `CancelledError` propagates.
- `darkula.testing.fake_data_stream.FakeDataStream` — deterministic
  publish/poll/ack with per-lane monotonic positions, per-consumer explicit
  acknowledgement (polling never acknowledges), stream isolation,
  scripted failures, and `reset_consumer()` as the smallest explicit
  replay/duplicate seam.
- `darkula.infrastructure.object_store.InMemoryObjectStore` —
  streaming put/get, frozen missing semantics, instance isolation,
  SHA-256 representation hash.
- `darkula.infrastructure.object_store.LocalFileObjectStore` — streaming
  temp-file + atomic-replace writes confined to an explicit root; traversal
  and symlink-escape rejection; bounded read chunks; no partial final
  objects on ordinary failure/cancellation; metadata kept in memory only
  (documented).
- Agent fakes may exist for tests above an agent boundary but should not
  replace real agents in primary pipeline integration tests.

## PR 3 deterministic component slices
`tests/unit/test_deterministic_slices.py` runs under normal `--qa`:

- A. `base -> profile -> local -> env -> Settings -> composition` (exact
  precedence, implementation types, fail-fast unsupported drivers, no
  network);
- B. `publish -> poll -> process -> explicit ack` (poll-before-ack, no
  implicit ack);
- C. local artifact `AsyncIterable -> put -> stat -> get -> delete` (root
  confinement and frozen missing semantics);
- D. telemetry around a fake operation (result unchanged; exact span, count,
  and duration; no prompt/payload/result capture).

## Observability tests
Use OTEL in-memory SDK components only: `tests/unit/conftest.py` wires the
decorator seams to an `InMemorySpanExporter` + `InMemoryMetricReader`, and
`tests/support/otel.py` reads metrics back. Tests never depend on a
Collector/Prometheus/Loki/Jaeger/Grafana and never set global OTel
providers (order-independent by design).

## Configuration tests
`tests/unit/config/test_loader.py` covers the C-matrix: every precedence
boundary, empty/unset environment no-op semantics, deep merge, unknown-key
and malformed-profile/TOML failures, secret-like diagnostic redaction,
determinism, and environment isolation. Tests clear ambient `DARKULA_*`
variables and use `monkeypatch` so they stay order-independent.

## Async concurrency tests
When production code introduces concurrent I/O, tests must verify the concurrency contract rather than only the final result. Cover the configured concurrency bound, cancellation propagation, ordinary failures, and any ordering or rate/resource constraints that apply. Where `asyncio.gather()` is used, tests should make it possible to detect accidental serialization as well as unbounded task fan-out. Keep concurrency tests deterministic; do not depend on timing races or live external services.

## Reliability tests
Assume at-least-once stream delivery. Test duplicate messages, consumer
crash before/after durable commit, outbox retry (lease expiry, publish
failure, mark failure, crash-after-publish-before-mark), partial broker
acceptance, poison/non-retryable messages, cancellation, and replay.
Persisted processing must be idempotent by stable `message_id`, never by
position.

PR 5 unit matrices (offline, no broker/database):

- `tests/unit/infrastructure/data_stream/test_codec.py` — DS1–DS8, DS16:
  canonical round-trip, absent-optional determinism, UTC preservation,
  strict rejection, trace headers never in payload, data-free errors;
- `tests/unit/infrastructure/data_stream/test_redpanda.py` — DS9–DS16 plus
  lifecycle: contract validation, routing-key -> broker key, positions,
  never-acks polling, high-water acknowledgement, malformed-record bounded
  failure, consumer-group isolation, cancellation propagation, trace
  header injection/extraction, bounded provider errors (fake aiokafka
  clients);
- `tests/unit/infrastructure/persistence/postgresql/test_message_repositories.py`
  — O1/O2/O3/O5/O9/O12: outbox append/claim/mark and processed-message
  invocation + mapping + cancellation, covered by the SQL-boundary guard;
- `tests/unit/app/test_outbox_publisher.py` — O6/O7/O8/O10/O11:
  claim->publish->mark with commit boundaries, failure/cancellation leaves
  rows retryable, partial acceptance fails closed, broker I/O outside any
  open transaction;
- `tests/unit/app/test_consumer.py` — C1–C10: first delivery commits
  effect+marker, duplicates suppressed (same and different positions),
  independent consumers, handler failure/cancellation rollback without ack,
  commit-then-ack-failure redelivery safety;
- `tests/unit/scripts/test_darkula_redpanda_script.py` — I1–I10: fake-podman
  lifecycle (labeled provision, owned reuse, unlabeled/wrong label fail
  closed, foreign 39092 listener refusal, exact owned cleanup, foreign
  exact-name refusal, untouched unrelated resources, inspect failure,
  repeated clean no-op);
- `tests/unit/infrastructure/data_stream/test_telemetry.py` — bounded
  datastream/outbox/consumer counters, histograms, spans, and label
  vocabulary (no message_id/correlation_id/payload/routing-key values).

`FakeDataStream`'s `reset_consumer()` remains the replay/duplicate seam for
app-level slices. Real Redpanda vertical slices (V1–V3) replay and dedupe
against the live broker.

## Security tests
Test sandbox input/output constraints, path/origin budgets,
malicious/malformed content, prompt-injection-like text, oversized
artifacts, unsafe filenames, redirects, session expiry, and credential/log
redaction. PR 3 adds: LocalFileObjectStore traversal/symlink-escape
confinement, telemetry attribute secret-marker rejection, configuration
diagnostic redaction, and "no capture" telemetry assertions.

## Relationship to evaluations
Integration tests answer whether engineering contracts execute correctly.
Evals answer whether model-backed reasoning/extraction is good enough. Both
should reuse the same scenario definitions and Fake World truth where
possible; do not maintain unrelated test and eval universes.

## Fake World test infrastructure (PR 6)
The Fake World is an executable testing subsystem, not a fixture directory:
`src/darkula/testing/fake_world/` provides scenario/truth models, a
deterministic renderer, a scenario registry, and behavior traceability (see
`docs/FAKE_WORLD.md`).

- Canonical scenario: `darkula.testing.fake_world.get_scenario("blackgate-core", version=1)`.
  Definitions are immutable and shared; unknown IDs/versions fail
  deterministically with `UnknownScenarioError`.
- Fresh runtime state: create `FakeWorldRenderer()` per test (or reuse one
  and call `renderer.reset()`). Sessions, expiry counters, rate-limit views,
  and scripted failures never leak across renderers or tests.
- Truth access rules: `FakeWorldTruth` is for tests/evals only. Production
  packages must never import `darkula.testing.fake_world` (static guard in
  `tests/unit/testing/fake_world/test_architecture.py`). Rendering never
  exposes hidden truth; regression tests scan every rendered page/header for
  hidden identifiers.
- Golden-rendering policy: assert semantic fragments and stable link sets;
  freeze SHA-256 hashes for only a handful of canonical pages
  (`TestCanonicalGoldenPages`). Golden hashes are never regenerated
  automatically — changing canonical observable content requires an
  intentional version bump and deliberate golden update.
- No network/browser requirement in PR 6: the renderer is transport-light
  (request/response objects, not HTTP). PR 7 will add a tiny test-only HTTP
  seam when the crawler requires real HTTP.
- Deterministic failure behaviors are part of the world: 401/403/404/405/
  429/503 and redirects are returned as responses; scenario/programming
  errors raise typed validation errors and are tested separately (FW5–FW7).

Unit matrices under `tests/unit/testing/fake_world/`: FW1–FW10 (core model),
R1–R24 (rendering), G1–G5 (geography/truth), T1–T6 (traceability), the
all-rendered-navigation vertical slice (`VSLICE`), and architecture/import
guards (FW9). All run inside the normal `--qa` gate; no Podman, PostgreSQL,
Redpanda, browser, or network is required.

## Crawler and sandbox (PR 7)

**FakeSandbox boundary.** `darkula/testing/fake_sandbox.py` implements the
`Sandbox` SPI for unit tests above the boundary: it records every request,
returns scripted results (FIFO), and can await forever so controller
cancellation paths are testable without Podman. `FakeLlmClient`-style
faking of the *infrastructure* is expected; the CrawlerController behavior
under test is never replaced.

Unit coverage (runs in `--qa`, no Podman/browser/network):

- `tests/unit/crawler/test_contracts.py` — origin parsing, request
  validation/budget bounds, result bounds (C-series);
- `tests/unit/crawler/test_runtime_protocol.py` — versioned v1 JSON codec:
  encode/decode round trips, control-character rejection, size/length
  bounds, malformed input failures (RP-series);
- `tests/unit/crawler/test_controller.py` — policy derivation
  (exact one-destination allowlist, least capability), budget clamping vs
  settings maxima, loopback/metadata origin rejection, exit-reason
  mapping, invalid-output handling, cancellation propagation, OTEL
  emission (J-series);
- `tests/unit/crawler/test_runtime_engine.py` — deterministic bounded BFS:
  sorted discovery, budgets, 503-retry-once, 429-as-data, logout skip,
  fragment/out-of-origin/non-HTTP link drop, login-once + post-login
  re-observation of a bounced target;
- `tests/unit/sandbox/` — Sandbox SPI contracts and validation;
- `tests/unit/infrastructure/sandbox/test_podman_sandbox.py` — argv
  building (no shell), output collection bounds, exit-code mapping,
  timeout/cancellation cleanup, ownership-preflight fail-closed;
- `tests/unit/testing/fake_world/test_http_adapter.py` — Fake World HTTP
  semantics H1–H12 (redirects, sessions, expiration, 429/503, malformed
  page, attachments, `&amp;` handling, cookie case-insensitivity, no truth
  on the wire).

**Real Podman/browser integration** (`tests/integration/crawler/`, marked
`integration`, run by `./build.sh --intg` after provisioning):

- `test_vertical_slice.py` — the canonical slice: real `CrawlerController`
  → real `PodmanSandbox` → fresh disposable container → real
  `CrawlerRuntime` → real Playwright/Chromium → real HTTP → BlackGate Fake
  World. Execution A (cold start at `/`) asserts the protected→login
  bounce, login POST→index with session retention, boards, threads and
  graceful session expiry; Execution B (start at the hospital thread
  `?page=1`) asserts
  post-login re-observation and pagination (`Page 2 of 2`) with the same
  session; both assert bounded attachment samples, 503-retry success, and
  that the truth-only tokens and auth password never appear in any
  observation. Also: one fresh disposable container per execution with
  zero containers left behind;
- `test_lifecycle.py` — sandbox timeout → `timed_out` + cleanup,
  cancellation → `CancelledError` + cleanup, unowned-same-name
  fail-closed (never adopted/removed), labeled-removal only, hardened
  host config (privileged=false, cap-drop, no-new-privileges, pids,
  memory, CPU, internal network, non-root user, no binds) plus a runtime
  probe for read-only rootfs/tmpfs, and enforced PID/memory ceilings;
- `test_network_isolation.py` — positive control (Fake World HTTP
  reachable through the exact crawler flags) and negatives: PostgreSQL,
  Redpanda, an unauthorized sibling on another Podman network, the
  internal network gateway/host listener (39080), loopback, and
  link-local cloud metadata are all unreachable; no host ports are
  published on the isolation network.

The integration fixtures fail closed when the runtime image, network, or
Fake World container is missing or mislabeled, and the suite creates/removes
only Darkula-owned resources (label verified before removal). No test ever
needs live Tor, malicious sites, or a second transport architecture.

## PR 8 testing status — normalization and artifact storage

PR 8 adds deterministic content-boundary coverage:

- **Domain/normalization (N-series** in `tests/unit/app/test_normalization.py`
  and `tests/unit/domain/test_content.py`**)** — multilingual text preserved;
  CRLF/CR canonicalized to LF; disallowed C0/DEL controls stripped while
  `\n`/`\t` survive; blank/invalid URI rejected; oversized title/text fail
  typed (`ContentTooLargeError`); same input twice yields identical
  bytes/hash; a changed byte changes the hash; prompt-injection text is
  retained only as data (never interpreted, no geographic extraction);
  page excerpts are marked `SAMPLE` and never complete-original; secret-like
  metadata is rejected; cancellation propagates.
- **Artifact storage/dedup (A-series** in
  `tests/unit/app/test_artifacts.py`**)** — new hash = one put; existing
  compatible hash = no duplicate upload; same hash + new provenance = same
  physical key with distinct artifact records; same URI + changed bytes = new
  key/hash; deterministic key layout (`sha256/<2>/<64>`); existing key with
  wrong size/corrupt bytes = `ArtifactIntegrityError`; store unavailable and
  cancellation = bounded failures, never false success; unsafe filename/URI
  never influences the key.
- **Content-ingestion orchestration** (`tests/unit/app/test_content_ingest.py`)
  — dedup reuses the artifact record while preserving two provenance-bearing
  observations, and a retried provenance identity creates no duplicate
  observation.
- **S3-compatible adapter (S-series** in
  `tests/unit/infrastructure/object_store/test_s3.py`**)**: put/get/stat/
  delete round-trips, frozen missing semantics, provider-unavailable and
  5xx mapped to bounded `ObjectStoreUnavailableError`/`ObjectNotFoundError`
  (no raw provider text), independent Darkula SHA-256 (never ETag), endpoint
  override accepted, and lazy no-network construction. These are SDK-level
  fakes; live S3/R2-compatible infrastructure is deferred to PR 16 (no CI
  dependency on AWS/R2/cloud).
- **Composition/config (C-series)** — `local`/`in_memory` unchanged; `s3`/
  `r2` both compose `S3CompatibleObjectStore`; missing remote bucket fails
  fast; env credentials override file; empty env is a no-op; diagnostics
  redact credentials; no network call at composition; no silent fallback.
- **Static architecture guards** (`tests/unit/crawler/test_runtime_storage_free.py`)
  — the crawler-runtime image must never import the Darkula ObjectStore
  boundary or any S3/R2 SDK, and the sandbox surface exposes no storage
  credentials.
- **Real-PostgreSQL content persistence (P-series** in
  `tests/integration/persistence/test_content_repository.py`**)**: artifact
  create/get/find round-trips, observation create/get round-trips, two
  observations referencing one artifact, same-URI-at-two-times immutability,
  invalid artifact FK rejection, duplicate-provenance idempotency, structural
  metadata/sample/version round-trips, and the SQL-boundary guard covering the
  new repository.
- **Real-crawler normalization vertical slice** (`tests/integration/crawler/
  test_normalization_vertical_slice.py`) — real `CrawlerController` →
  Playwright/Chromium → Fake World → production mapper → production
  normalizer → real `LocalFileObjectStore` → real PostgreSQL; asserts bounded
  SAMPLE excerpts, stored SHA-256 matching exact bytes, artifact/
  observation reload, and no truth-token/auth-password leakage. A dedup slice
  proves one physical key with two provenance observations; a changed-content
  slice proves same URI + changed bytes = distinct hash/key; a failure slice
  proves ObjectStore failure records no structured success.
