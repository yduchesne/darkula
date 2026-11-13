# Darkula Testing Strategy

## v0.2 planned acceptance and test boundaries

The completed v0.1 integration suite uses controlled Fake World sources. PR 17's discovery/provenance tests and PR 19's candidate-admission/qualification tests are delivered (see [PR 17 testing status](#pr-17-testing-status--discovery-foundation) and [PR 19 testing status](#pr-19-testing-status--candidate-admission-and-policy-routed-qualification)); the remaining requirements below are **planned** for [v0.2](ROADMAP_V02.md).

- **Discovery contract/persistence:** deterministic registry fixtures; malformed/unresolvable locators; provider evidence and provenance retention; replay/race idempotency; stable candidate identity without URI-equals-Source assumptions.
- **Source reconciliation:** duplicates from separate feeds, changed provider records, aliases/mirrors, malicious claims, rejected candidates, and failed imports. No automatic source merge or promotion.
- **HTTPS/Tor sandbox:** transport-specific network allowlists, DNS rebinding, private/link-local/metadata denial, redirect and cross-origin rejection, Tor proxy failure/no clearnet fallback, bounded requests/pages/time/output, clean cancellation, no privileged fallback.
- **Recursive/semantic discovery:** external links captured as data rather than navigated; exact source evidence; name-only references remain unresolved; adverse prompt injection cannot authorize browsing.
- **Qualification routing (PRs 17/19):** complete policy-allowlisted fixture yields `REGISTRY_POLICY`, `QUALIFIED` candidate event and durable qualification evidence with **zero** `ReconAgent`/`LlmClient` calls and **no** `ReconAssessment` rows; unknown providers, unsupported record classes, incomplete/conflicting metadata, spoofed mirrors and unsafe locators route to bounded authorized `AGENT_RECON`, pending or validation failure as policy dictates. Agent route links a genuine existing ReconAssessment. Separate endpoint reachability/probe from qualification; ensure no implicit collection authorization, promotion or credentials.
- **Qualification persistence and compatibility:** same-result retries/races create no duplicate authoritative decision; method/policy-version/evidence refs round-trip from PostgreSQL; decision/status/event changes are atomic; consumers work for both qualified candidates with and without ReconAssessments; historical v0.1 candidate assessments remain valid; disabled/changed provider trust rules take effect prospectively.
- **Source access readiness:** `UNKNOWN` / `PUBLIC` / `CREDENTIALS_PENDING` / `READY` / `AUTH_FAILED` / `ACCESS_REVOKED` persisted separately from existing candidate/source status; source discovered behind login is retained (not REJECT); public-scope qualification can succeed pending protected content; correct reason/provenance and candidate-to-Source transfer; no authenticated work scheduled/claimed while blocked.
- **Manual operator provisioning and authentication (PR 26):** synthetic login with operator-provisioned credential references; merely setting a reference remains pending until positive validation; expired/invalid credentials persist `AUTH_FAILED` without infinite retries; `ACCESS_REVOKED` cannot reauthorize itself; re-provision/revalidation resumes only permitted collection; credentials not leaked to another Source/endpoint, artifacts, models, or telemetry; human verification/approval is never automated.
- **MonitoringProfiles:** UUID and globally unique caller correlation IDs, generated correlation defaults, concurrent duplicate-create handling, versioned match provenance, two profiles matching one collected source without duplicated collection, UUID-only profile-scoped API semantics and explicit correlation lookup.
- **Full stack:** registry fixture -> SourceCandidate -> policy-routed `REGISTRY_POLICY` (no agent) **or** `AGENT_RECON` (actual ReconCoordinator) -> explicit Source promotion -> approved HTTPS/Tor collection -> existing extraction/SourceAnalyst -> profile result query; replay and outages retain one authoritative effect.
- **Execution rule:** local test servers, Fake World and synthetic intelligence remain mandatory CI substrates. Live public/hidden service smoke tests require separate explicit permission and opt-in and must not be essential to ordinary `./build.sh --qa`, `./build.sh --sec`, or `./build.sh --intg`.

Measure discovery yield/precision, novelty, duplicate candidate rate, mean qualifying latency and cost; establish baselines before specifying release-wide numeric gates. Keep v0.1's static security guards, immutable-migration guard, and single-transaction semantics unchanged.


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
(`localhost/darkula-crawler-runtime:1.63.0-pr20`, cached when the tag is
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
reuse the same scenario definitions and Fake World truth; `docs/EVALUATIONS.md`
owns the evaluation subsystem.

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

## PR 9 testing status — collection lifecycle

Deterministic unit matrix (runs in `--qa`, offline):

- `tests/unit/domain/test_collection.py` — CP1-CP9 policy bounds (interval/
  budget/duplicates/empty-auth/credential-reference/naive-UTC/revision
  immutability) and CR1-CR8 run state machine (RUNNING/timestamps, terminal
  completion, sanitized failures, frozen revision/snapshot).
- `tests/unit/app/test_collection_scheduler.py` — SCH1-SCH10: due eligibility
  with explicit `now`, occurrence idempotency (retry/concurrent), bounded
  batch, atomic run+outbox commit vs rollback, IDs-only wire shape.
- `tests/unit/app/test_collection_service.py` — EX1-EX14 + MAP1-MAP8:
  claim/crawl/ingest/finalize, terminal/unknown handling, withdrawn
  authorization (no crawl), deterministic request-id stability (MAP6/MAP7),
  frozen-snapshot semantics after RUNNING, cancellation persistence, failure
  sanitization, and the "no UoW open during crawler/ingest" probe.
- `tests/unit/app/test_collection_worker.py` — WK1-WK13 via FakeDataStream +
  in-memory persistence fakes: first-delivery claim/execute/ack, terminal
  duplicates, lease-gated concurrency, same-run reclaim, attempts exhausted,
  crash-before-terminal no-ack, unknown-run poison, cancellation no-premature
  ack, malformed payload poison, and message-ID (never offset) idempotency
  markers.
- `tests/unit/config/test_collection_settings.py` — CFG defaults/env
  override/empty-env/bounds/fail-closed unknown fields, plus composed
  service/scheduler/worker.
- `tests/unit/telemetry/test_collection_telemetry.py` — bounded labels; no
  IDs/URIs/hashes/credentials in collection attributes.
- `tests/unit/infrastructure/persistence/postgresql/test_collection_repository.py`
  — fixed stored-function invocations, outcome mapping, cancellation, and the
  positive SQL-boundary scan covers the new repository module.
- `tests/unit/test_deterministic_slices.py` — slice E: scheduler -> outbox ->
  FakeDataStream -> worker -> service -> fake crawler -> fake ingest.

Real infrastructure (`./build.sh --intg`):

- `tests/integration/collection/test_collection_persistence.py` — P1-P16:
  fresh-DB migration provisioning + ledger, policy/run round-trips, endpoint
  ownership, occurrence uniqueness, expected-state transitions,
  concurrent-start one-winner, expired/unexpired lease, attempts exhausted,
  concurrent due admission one occurrence, scheduler run+outbox atomicity
  and rollback, prior-source data survival.
- `tests/integration/collection/test_collection_messaging.py` — M1-M4 over
  real PostgreSQL + Redpanda: scheduler -> outbox -> OutboxPublisher ->
  real broker (IDs-only command); duplicate publication never re-executes;
  terminal-durable-before-ack with redelivery no-recrawl; crashed-worker
  RUNNING recovery on the same run.
- `tests/integration/crawler/test_collection_vertical_slice.py` — the
  canonical vertical slice: real crawler -> real sandbox -> real browser ->
  Fake World HTTP -> real ingest -> ObjectStore + PostgreSQL -> SUCCEEDED +
  ack, plus post-success redelivery and the next-interval new run.

## PR 10 testing status — bounded reconnaissance

Deterministic unit matrices (runs in `--qa`, offline; only the LLM and
persistence/crawler boundaries are faked — the ReconAgent, ReconCoordinator,
and protocol are production code):

- `tests/unit/app/test_recon.py` — the RA/EV/IA/RC/RB/TS matrices over the
  real Coordinator + ReconAgent with `FakeLlmClient` (scripted FIFO), the
  in-memory candidate persistence fake, and a scripted crawler stub:
  - RA (agent protocol): valid INSPECT/COMPLETE, confidence/ref-count/JSON
    bounds, unknown action/disposition/payload mismatch, extra unknown
    fields (budgets/credentials/tools) fail closed, prompt-injection source
    text stays data;
  - EV (evidence): stable trusted references, deterministic ordering,
    deterministic truncation at the registry, aggregate context caps,
    known/unknown reference validation, truth tokens never in system
    prompts, credential-like content absent from observability metadata;
  - IA (inspection authorization): entrypoint anchor, same-origin relative/
    absolute, cross-origin/scheme/port/userinfo/unsupported-scheme
    rejection before any crawler call, settings-only budgets, deterministic
    request identity, un-crawlable entrypoints;
  - RC (Coordinator): startable statuses, already-under-recon/terminal
    replay no-work, disposition->status/event mapping, final-conflict
    assessment rollback, not-found, operational LLM/crawl failures never
    analytical REJECT, cancellation with no fabricated state/event,
    concurrent start one winner;
  - RB (budgets): exact max turns/inspections legal, max+1 stopped,
    evidence/context caps, crawl budgets <= settings;
  - TS (telemetry): bounded labels (no candidate ID/URL/content/password),
    workflow/attempt/inspection/invalid-request/failure-category metrics.
- `tests/unit/app/test_recon_agent.py` — AG1-AG10: one call per decision,
  INSPECT-then-COMPLETE, typed LLM errors, bounded repair only,
  repair-exhausted, cancellation unchanged, observability scope per model
  attempt, stable prompt-version metadata, injection source text confined to
  the user prompt, unknown/over-cap references fail closed.
- `tests/unit/app/test_recon_security_guards.py` — TS6/TS7/TS10/TS8 static
  guards: recon modules never import playwright/podman/PostgreSQL adapters/
  ObjectStore/DataStream/Fake World/provider SDKs; the OpenAI adapter is the
  only `openai` importer in `src`; the crawler runtime never imports
  LLM/agent boundaries.
- `tests/unit/config/test_recon_settings.py` — ReconSettings bounds/env
  precedence/fail-closed and LlmProviderSettings secret/no-retry contract.
- `tests/unit/infrastructure/llm/test_openai_adapter.py` — LA1-LA10 over the
  real adapter with the external SDK transport stubbed: exact Pydantic
  model, TIMEOUT/PROVIDER_FAILURE/INVALID_STRUCTURED_OUTPUT/
  CONFIGURATION_ERROR mapping, cancellation unchanged, secrets absent from
  public errors, `max_retries=0`, missing config fail-fast, offline
  construction.
- `tests/unit/test_composition.py` — OPENAI driver composes the provider
  adapter; missing model/api-key fails fast; the runtime exposes
  `recon_agent`/`recon_coordinator`.

Real infrastructure (`./build.sh --intg`):

- `tests/integration/recon/persistence/test_recon_persistence.py` — RP1-RP12
  over real PostgreSQL: atomic start observed from a second transaction
  during external work, assessment+transition atomic commit for every
  disposition, final-conflict rollback (no orphan assessment), append-only
  multi-attempt history in deterministic order, concurrent start one winner,
  no production Python SQL in recon modules, migrations 0001-0004
  byte-identical.
- `tests/integration/recon/slice/test_recon_vertical_slice.py` — the
  canonical slice (section 15): real PostgreSQL candidate -> real
  ReconCoordinator -> real ReconAgent -> FakeLlmClient -> real
  CrawlerController -> real PodmanSandbox -> disposable CrawlerRuntime ->
  real Playwright/Chromium -> BlackGate HTTP -> bounded observations -> real
  PostgreSQL final transition. Slices: qualify with two scripted
  inspections (exactly one STARTED + one QUALIFIED event, one assessment,
  evidence refs validated, no truth/password leakage into prompts or
  persisted rows, no Source/CollectionPolicy created); cross-origin
  rejection (no crawler execution, no fabricated REJECT); needs-more then a
  second execution appending an independent assessment; reject with terminal
  replay performing no LLM/crawl; concurrent start with one winner and no
  duplicate STARTED.

The FakeLlmClient remains the deterministic automated-test boundary:
real-workflow behavior (Coordinator/ReconAgent/persistence/crawler/sandbox)
is never faked, and CI never requires a paid/live model API.

## PR 11 testing status — deterministic extraction

- `tests/unit/domain/test_extraction_domain.py` — domain matrix DM1-DM10:
  span validation, bounded/control-free extractor identity, result validation,
  duplicate manifest rejection, invalid entity counts, overlong values, and
  the finite PR 11 entity/hash-subtype vocabulary.
- `tests/unit/app/test_extractors.py` — semantic matrices IP1-IP8, URL1-URL10,
  EM1-EM7, DN1-DN10, HS1-HS7. Every match is asserted to equal the exact
  canonical-text slice `text[start:end]`; Unicode/IDNA v1 behavior is
  documented as ASCII-only.
- `tests/unit/app/test_extraction.py` — aggregation AG1-AG10 (deterministic
  order, distinct spans preserved, exact-duplicate dedup, contract failure on
  span/raw mismatch, exact-cap legal vs cap+1 typed failure without
  truncation, same-input determinism, inert prompt injection, empty text,
  deterministic manifest) and service/ObjectStore ES1-ES14 using an isolated
  in-memory SPI (`tests/support/extraction_fakes.py`) plus the real
  `InMemoryObjectStore`.
- `tests/unit/app/test_extraction_security_guards.py` — TS6-TS8: static
  imports never reach LLM/provider/network/browser/sandbox/DataStream/Fake
  World, no SQL in application/domain extraction, and telemetry carries only
  static bounded names.
- `tests/unit/config/test_extraction_settings.py` — the only PR 11 setting
  (`max_entities_per_content`) defaults/validates; extractor/profile
  versions are not runtime configuration.
- `tests/unit/test_composition.py` — central composition exposes exactly one
  `DeterministicExtractionService` built from persistence + ObjectStore + the
  fixed profile, with no LLM/crawler/DataStream injection.
- `tests/integration/extraction/persistence/test_extraction_persistence.py`
  — real-PostgreSQL RP1-RP12: atomic result+entity commit, semantic-key
  uniqueness, coexisting new profile versions, identical bytes/distinct
  observations, exact-occurrence conflict, same value/two spans, rollback on
  a fault after result creation, bounded FK errors, insert-order-independent
  listing, and a real concurrent semantic-key race with exactly one winner.
- `tests/integration/extraction/slice/test_extraction_service_slices.py` —
  failure/concurrency slices against real PostgreSQL + ObjectStore: missing
  object and hash mismatch fail typed before extraction with zero
  persistence; two concurrent same-content extractions resolve to exactly one
  durable result.
- `tests/integration/crawler/test_extraction_vertical_slice.py` — the
  canonical slice: real `CrawlerController` -> real `PodmanSandbox` ->
  Playwright/Chromium -> BlackGate HTTP -> real PR 8 `ContentIngestService` ->
  ObjectStore canonical representation -> real PostgreSQL `NormalizedContent`/
  `ContentArtifact` -> real `DeterministicExtractionService` -> real
  `ExtractionResult`/`ExtractedEntity`. It asserts ObjectStore (not preview)
  is the source, every raw value equals its exact slice, v1 normalization,
  persisted manifest/extractor versions, deterministic ordering, duplicate
  spans preserved, replay returning the same durable result with no duplicate
  rows, and no `SourceAssessment`/relationship behavior.

The canonical slice uses an additive PUBLIC Fake World thread
(`thr-collector-samples`) with fully synthetic, reserved/non-routable
observables; the extraction service and extractors are real, never faked, and
no LLM/live network is involved.

## PR 12 testing status — semantic extraction and geographic resolution

- `tests/unit/domain/test_geography_domain.py` — geographic domain matrix:
  resolver identity/request bounds, RESOLVED/AMBIGUOUS/UNRESOLVED invariants,
  confidence bounds, coordinate bounds, country-code shape, and
  provider-neutral result validation.
- `tests/unit/app/test_semantic_extraction.py` — the SD/GR/SE matrices:
  strict response-model bounds (SD1-SD13, including no `OTHER`, no
  relationship field, finite confidence), exact grounding (GR1-GR15: one/zero/
  ambiguous occurrences, context disambiguation, case/whitespace rejection,
  Unicode code-point spans, duplicate/spans handling, cap), and the service
  (SE1-SE22) using the in-memory SPI plus real `InMemoryObjectStore` and
  `FakeLlmClient`: ObjectStore is the source, missing/oversized/hash/UTF-8
  failures never call the model, existing-result reuse, model failures,
  invalid output, cancellation during read and model, rollback, replay,
  deterministic/semantic coexistence, prompt untrusted-data framing, and
  Fake World truth absence.
- `tests/unit/app/test_geography.py` — the GS matrix: bounded resolver
  context, LOCATION-only enforcement, not-found, span-integrity failure,
  missing/hash-mismatch canonical bytes, RESOLVED/AMBIGUOUS/UNRESOLVED
  persistence, resolver reuse and new-version coexistence, resolver failure,
  cancellation, context-at-boundaries, Unicode context, and wrong-type
  resolver contract failure.
- `tests/unit/app/test_extraction_security_guards.py` — TS12 static guards:
  no provider/geocoder SDK, no ReconAgent/DeepAgent, no crawler/sandbox, no
  Fake World truth, no SQL, and content-free telemetry names for the semantic
  and geography modules.
- `tests/unit/config/test_extraction_settings.py` /
  `tests/unit/test_composition.py` — semantic/geography settings defaults and
  bounds, semantic composition with the existing `LlmClient`, geography
  disabled by default, explicit-fake composition, and fail-fast when geography
  is enabled with no resolver.
- `tests/integration/extraction/persistence/test_semantic_geography_persistence.py`
  — the RP12 matrix against real PostgreSQL: atomic semantic result+entity
  commit, deterministic/semantic coexistence, semantic-key conflicts and
  coexisting versions, confidence round-trip, PR 11 NULL confidence, FK
  errors, rollback, resolved/ambiguous/unresolved round-trips, resolver-version
  coexistence, and real concurrent semantic/geographic races yielding exactly
  one durable row.
- `tests/integration/extraction/slice/test_semantic_geography_slices.py` —
  real PostgreSQL + ObjectStore slices: missing object/hash mismatch/model
  failure/resolver failure with no persistence, concurrent semantic extraction
  and concurrent geographic resolution each yielding one durable row, and the
  Washington State / Washington, D.C. / deliberately ambiguous `Washington`
  cases proving the same token is not hard-coded to one canonical place.
- `tests/integration/crawler/test_semantic_extraction_vertical_slice.py` —
  the canonical slice: real `CrawlerController` -> real `PodmanSandbox` ->
  Playwright/Chromium -> BlackGate HTTP -> real `ContentIngestService` ->
  ObjectStore -> real PostgreSQL -> real `SemanticExtractionService` ->
  `FakeLlmClient` -> real grounding -> real `ExtractionResult`/`ExtractedEntity`
  (coexisting with deterministic extraction) -> real
  `GeographicResolutionService` -> `FakeGeographicResolver` -> real
  `GeographicResolution`. Only the external world/model/resolver are faked.

The canonical slice proves exact source spans, persisted extraction
confidence, unchanged LOCATION occurrence after resolution, bounded resolver
context, replay without repeat model/resolver calls, no truth leakage, and no
`SourceAssessment` creation. `tests/integration/persistence/test_migrations.py`
asserts migration 0006 plus the retained PR 11 v1 stored functions.

## PR 13 testing status — content-derived relationship assertions

- `tests/unit/domain/test_relationship_domain.py` — the RD1-RD14 domain
  matrix: result/assertion validation, UTC timestamps, manifest uniqueness,
  support-span/text-length equality, support/confidence bounds, prohibited
  self-edges, finite predicate round-trip, arbitrary-predicate rejection, and
  distinct identity classes.
- `tests/unit/app/test_relationship_extraction.py` — the EC/RP/RG/RS matrices
  using the in-memory SPI, real `InMemoryObjectStore`, and `FakeLlmClient`:
  deterministic invocation-local endpoint refs, same-value/different-span and
  foreign-content exclusion, catalog capacity, strict structured-output bounds
  (extra fields, unknown predicate, bad refs/confidence/support/list bounds),
  exact support grounding (unique/absent/contextualized/ambiguous, endpoint
  containment, unknown refs, dedup, reverse direction, Unicode spans, hostile
  prose-as-data, exact cap vs over-cap), and the service (replay without
  ObjectStore/model, missing content/artifact/object, hash/UTF-8 failures,
  zero/one endpoint durable zero results, partial rejection, all-rejected,
  provider/invalid-output failures, cancellation, atomic rollback, winner
  reload, untrusted-data prompt, endpoint catalog vs truth, and preview never
  as source).
- `tests/unit/app/test_relationship_security_guards.py` — static guards:
  provider/network/browser/sandbox/DataStream/recon/Fake-World-truth import
  guards, no SQL in application/domain, stored-function-only repository, static
  content-free telemetry names, byte-identical migrations 0001-0006 via pinned
  SHA-256, and no graph/global-entity/SourceAnalyst classes.
- `tests/unit/config/test_extraction_settings.py` — the `relationship_extraction`
  settings defaults and bounds.
- `tests/integration/extraction/persistence/test_relationship_persistence.py`
  — the RDB1-RDB20 matrix against real PostgreSQL: result/assertion
  round-trips, semantic-key conflict, coexisting versions, unknown
  result/endpoint and wrong-content integrity errors, result/content mismatch,
  exact-duplicate conflict, distinct support spans, same-semantics/different
  occurrences, deterministic listing by result/content, rollback, and a real
  concurrent semantic-key race with exactly one winner.
- `tests/integration/extraction/slice/test_relationship_slices.py` — real
  PostgreSQL + ObjectStore slices: exact endpoint-occurrence ids and support
  spans, unknown refs become a durable zero result, missing object/hash
  mismatch/provider failure/cancellation persist nothing, concurrent
  same-profile convergence, and the cross-content provenance proof (same bytes
  and semantics in two observations keep distinct content/result/assertion/
  endpoint-occurrence ids).
- `tests/integration/crawler/test_relationship_vertical_slice.py` — the
  canonical real-browser slice: real `CrawlerController` -> `PodmanSandbox` ->
  Chromium/Playwright -> BlackGate HTTP -> production mapping/normalization ->
  real ObjectStore -> real PostgreSQL `NormalizedContent`/`ContentArtifact` ->
  real `SemanticExtractionService` -> `FakeLlmClient` -> real persisted
  `ExtractedEntity` occurrences -> real `RelationshipExtractionService` ->
  `FakeLlmClient` (bounded endpoint catalog) -> trusted ref/support grounding
  -> real PostgreSQL `RelationshipExtractionResult`/`ExtractedRelationship`.
  It asserts the real crawler path, no truth leak, endpoints persisted before
  relationship extraction, bounded refs-only prompt, exact canonical support,
  `relationship-assertions/v1` result, persisted confidence, unchanged
  entities/geography, PR 11/12 coexistence, replay without a second model call,
  and no `SourceAssessment`/global-graph object.

`tests/integration/persistence/test_migrations.py` asserts migration 0007 plus
the retained PR 11/12 stored functions and the relationship constraints.

## PR 14 testing status — policy-bounded source analysis

- `tests/unit/domain/test_source_assessment_profile.py` — SA matrix: bounded
  `profile_name`/`profile_version`, immutability, legacy default, confidence
  bounds/NaN/Inf, secret-like characteristics rejection.
- `tests/unit/app/test_source_analyst.py` — SP (strict extra-forbid response,
  model-supplied identity/window rejection, bounded characteristics/refs) and
  AN matrices (one structured `LlmClient` call, UNTRUSTED DATA prompt,
  hostile evidence confined to user data, stable operation/prompt version,
  bounded provider/output errors, cancellation propagation).
- `tests/unit/app/test_source_analysis.py` — AP (request window validation and
  max-window), AC (source/window ownership, per-source isolation,
  deterministic ordering, exact-capacity/overflow truncation, bounded
  summaries, no implicit extraction, no-evidence), EG (trusted durable ref
  grounding, unknown-ref failure, dedup/order, occurrence refs), and SS
  matrices (replay short-circuit, trusted identity/window/profile/time,
  model/output/grounding failures persist nothing, cancellation, concurrent
  loser reload, history unchanged, window/profile coexistence).
- `tests/unit/app/test_source_analysis_security_guards.py` — static guards:
  no provider/browser/sandbox/DataStream/recon/Fake-World-truth imports; the
  analyst has no persistence/collection capability; no SQL in application
  modules; stored-function-only PostgreSQL repositories; migrations 0001-0007
  byte-identical SHA-256; no global graph/truth classes; no `SourceStatus`
  mutation; static content-free telemetry names.
- `tests/unit/config/test_source_analysis_settings.py` — bounded
  `SourceAnalysisSettings` plus non-empty environment override.
- `tests/unit/test_composition.py` — the source-analysis service is composed
  only when enabled and reuses the single composed `LlmClient`.
- `tests/integration/source_analysis/test_source_analysis_slice.py` — real
  PostgreSQL slice: real `Source`/`CollectionRun`/`NormalizedContent`/
  `ExtractedEntity`/`GeographicResolution`/`ExtractedRelationship` -> real
  `SourceAnalysisContextBuilder` -> real `SourceAnalyst` -> `FakeLlmClient` ->
  trusted grounding -> real persisted `SourceAssessment`. Asserts source/window
  filtering, no cross-source leakage, all evidence kinds, durable refs,
  unknown-ref/provider-failure/cancellation persist nothing, no-evidence fails
  closed, replay without a second model call, and concurrent same-key
  convergence on one durable row.
- `tests/integration/crawler/test_source_analysis_vertical_slice.py` — the
  canonical real-browser slice: BlackGate Fake World -> real HTTP ->
  Playwright/Chromium -> disposable `PodmanSandbox`/`CrawlerRuntime` -> real
  `CrawlerController` -> deterministic normalization -> real ObjectStore ->
  real PostgreSQL `NormalizedContent` -> managed Source-linked collection
  history -> real semantic/geography/relationship extraction -> real
  `SourceAnalysisContextBuilder` -> real `SourceAnalyst` -> `FakeLlmClient` ->
  trusted grounding -> real persisted `SourceAssessment`. Asserts the crawler
  path, `truth_leaks(...) == []`, persisted facts feed analysis, bounded
  evidence catalog, hostile source text absent from the system prompt, correct
  source/window/profile, durable refs, unchanged `Source`/endpoints, replayed
  analysis without a second model call, and no fabricated assessment for an
  evidence-free window.

The unit suite runs the real `SourceAnalyst` with `FakeLlmClient`; the model is
the only faked nondeterministic boundary. Live-model SourceAnalyst evaluation
remains PR 15.

## PR 15 testing status — expanded Fake World and evaluations

Fake World expansion tests (`tests/unit/testing/fake_world/`):

- `test_cross_source.py` covers the `darkula-cross-source` v1 matrix
  (FW15-1..FW15-18): model construction and typed cross-reference failures,
  deterministic rendering, gated marketplace listings, updated/sold/removed
  listing states, leak index/detail and teaser/published/changed/removed
  state, ShadowTalk forum compatibility, cross-source actor/organization
  truth, hidden-truth non-rendering, the observable false claim, behavior
  manifest coverage, fixed UTC timestamps, and mirror references;
- BlackGate v1 golden pages/behavior IDs remain unchanged (existing tests).

Evaluation tests (`tests/unit/evaluation/`) are fully deterministic and never
call a live model or the network:

- `test_domain.py` (ED15) — bounded identity/model/result/manifest
  validation, deterministic serialization, partial-run distinguishability;
- `test_metrics.py` (EM15) — exact counts, empty-set semantics, duplicates,
  grounding, rubric coverage, repeat determinism;
- `test_evaluators.py` (RE15/SE15/GE15/RL15/SA15) — recon, semantic,
  geography, relationship, and SourceAnalyst scoring including
  hidden-truth-leakage detection and unsupported-conclusion detection;
- `test_cases.py` (ED15-8..10) — explicit registry order and typed
  unknown-case failure;
- `test_runner.py` (ER15) — deterministic ordering, bounded
  CASE/MODEL/EVALUATOR/SETUP errors, cancellation with a partial manifest,
  live-requested-with-fake failure, safe artifacts, explicit gating, and
  distinct run IDs with stable case IDs;
- `test_cli.py` — explicit opt-in CLI listing/replay, disabled config
  failure, live+fake failure, and exit codes.

Observability tests
(`tests/unit/infrastructure/observability/test_langsmith.py`, AO15) exercise
the adapter with a fake/stub SDK boundary: safe metadata only, prompt/output
unrepresentability, fail-open start/end/patch/flush, unchanged application
exceptions and cancellation, and a static guard that the LangSmith SDK is
imported only inside the infrastructure adapter.

`./build.sh --eval` is the only entry point that runs the evaluation CLI. It
is never invoked by `--qa`, `--sec`, or `--intg`; ordinary CI never requires
live-model or SaaS credentials. Live-model acceptance runs are manual and
documented; lack of credentials does not block deterministic coverage.

## PR 16 testing status — v0.1 full-stack hardening

### Telemetry unit matrix (`tests/unit/telemetry/test_otlp.py`, OT16)

- disabled -> no providers/export/network;
- local-only -> no exporters/readers;
- OTLP -> one `BatchSpanProcessor` and one periodic metric reader composed;
- invalid/missing OTLP endpoint and invalid timeout/interval fail closed;
- non-empty env overrides win; empty env has no effect;
- bounded fail-open `force_flush`/`shutdown`, including exporter errors;
- SDK/exporter imports stay confined to `darkula/telemetry/`;
- telemetry settings expose no secret-like fields.

### Real OTLP export integration (`tests/integration/observability/test_otel_export.py`, OI16/TP16A)

Runs against the real Darkula-owned Collector/Jaeger/Prometheus provisioned by
`./build.sh --intg`: emits a real span and counter, flushes, then queries
Jaeger (trace present) and Prometheus (metric present) with bounded polling
and asserts known sensitive sentinels are absent from the exported data.

The same file also proves **real Redpanda W3C trace propagation** (TP16A): a
probe message is published under a known upstream span through the real
`RedpandaDataStream` (which injects `traceparent` into broker transport
headers), consumed through the real adapter (which extracts the context), and
the resulting `test.redpanda_trace.root -> datastream.publish ->
datastream.poll` chain is verified through the Jaeger HTTP API: same trace ID
and explicit parent/ancestor span IDs, not merely span existence. A
no-active-context negative control message is published to a distinct stream
and asserted absent from the root trace, and sensitive sentinels are asserted
absent from the trace payload. Jaeger queries use bounded polling.

### v0.1 canonical full-stack slice (`tests/integration/crawler/test_v01_end_to_end.py`, E2E16A)

One documented canonical path begins with the real asynchronous collection
path and continues through every delivered downstream stage to a persisted
`SourceAssessment`:

```text
CollectionScheduler -> transactional message_outbox -> OutboxPublisher
-> real Redpanda -> CollectionWorker -> SourceCollectionService
-> CrawlerController -> PodmanSandbox -> CrawlerRuntime -> Chromium
-> Fake World HTTP -> ContentIngestService -> local ObjectStore -> PostgreSQL
-> DeterministicExtractionService
-> SemanticExtractionService (only FakeLlmClient)
-> GeographicResolutionService (only FakeGeographicResolver)
-> RelationshipExtractionService (only FakeLlmClient)
-> SourceAnalysisContextBuilder -> SourceAnalyst (only FakeLlmClient)
-> SourceAnalysisService -> persisted SourceAssessment
```

Only the external world (Fake World) and the non-deterministic model/resolver
providers are faked; no stage is replaced by a fake service and no downstream
row is test-seeded. The slice collects the public AccessBay catalogue (a
cross-source virtual host on the same Fake World container) and asserts:

- deterministic extraction persists exact-grounded occurrences and replays
  to the same result without duplication;
- semantic extraction (via the fake LLM) produces the grounded `ORGANIZATION`
  and `LOCATION` occurrences used downstream, with replay making no second
  model call;
- `GeographicResolutionService` resolves the real semantic `LOCATION`
  occurrence and replays without a second resolver call;
- `RelationshipExtractionService` persists an exact-grounded assertion whose
  endpoints are persisted occurrences of the same content, with replay
  making no second model call;
- the bounded `SourceAnalysisContextBuilder` exposes all five evidence kinds
  (`CONTENT_OBSERVATION`, `ENTITY_OCCURRENCE`, `GEOGRAPHIC_RESOLUTION`,
  `RELATIONSHIP_ASSERTION`, `COLLECTION_RUN`);
- `SourceAnalysisService` durably persists a grounded `SourceAssessment`, and
  replay returns the same assessment with `created is False` and no second
  analyst LLM call;
- hidden truth/password sentinels are absent from collected content, prompts,
  and persistence; all evidence belongs to the requested source/window; and
  analysis never mutates `Source`/endpoint lifecycle.

A second test (NightLeak/BlackGate virtual host) proves cross-source
content-locality (no hidden truth, no global identity merge).

### Combined failure -> recovery -> convergence (FR16A)

`test_v01_end_to_end.py` also drives one real system-boundary failure and
recovery: the **first `DataStream.publish` fails before broker acceptance**
(a transport-boundary test seam that wraps the real `RedpandaDataStream`; no
production fault switch and no production code change). The transactional
outbox row stays retryable; the existing `OutboxPublisher` retry path
republishes after the claim lease expires; the real `CollectionWorker` then
converges to exactly one `SUCCEEDED` run and one content set, and a duplicate
delivery is a no-op. This proves at-least-once semantics converge on exactly
one authoritative final effect under existing production retry rules.

### Replay/failure/security

Replay/idempotency and failure/recovery are also exercised across the
existing PR 5/8/9/11–14 integration slices (outbox publish retry, Redpanda
redelivery and `processed_message` no-op, content-addressed dedup with
provenance, semantic key replay without a second model call) and the sandbox
security suites (`tests/integration/crawler/test_network_isolation.py`,
`tests/unit/app/test_*_security_guards.py`). The pinned migration-hash guard
covers `0001`–`0008`.

### Integration lifecycle

`./build.sh --intg` installs a trap **before** the first provisioning step, so
a partial provision still removes only positively-identified Darkula-owned
resources and preserves the original non-zero exit status. The observability
script has deterministic lifecycle tests
(`tests/unit/scripts/test_darkula_observability_script.py`) covering ownership
labels, pinned images, explicit ports, foreign-resource refusal, occupied-port
refusal, and safe/idempotent cleanup.

## PR 17 testing status — discovery foundation

PR 17's tests are deterministic and offline. No network, DNS, Tor, crawler,
LLM, or Fake World scenario is required: the external provider boundary is a
pure `DiscoveryInput` value and is faked, never the persistence or domain
behavior under test. Migrations `0001`-`0008` remain byte-identical (the
frozen SHA-256 guard in
`tests/unit/app/test_source_analysis_security_guards.py` still covers them);
`0009_discovery_foundation.sql` is additive only.

### Unit matrix — domain (`tests/unit/domain/test_discovery.py`)

| ID | Assertion |
| --- | --- |
| U01 | Valid HTTP(S) locator normalizes scheme/host case, drops the default port, empty path -> `/` |
| U02 | Non-default port and path/query are preserved byte-for-byte; normalization is idempotent |
| U03 | Syntactically valid `.onion` host accepted with no network access |
| U04 | Unsupported schemes, userinfo, fragments, whitespace/control characters rejected |
| U05 | Encoded dot/slash, invalid IPv4/IPv6/port, trailing-dot host, protocol-relative and bare host rejected without repair |
| U06 | NAME_ONLY persists `normalized_locator=NULL` and `UNRESOLVED_NAME` |
| U07 | A NAME_ONLY reference that is an absolute URL is rejected (classification mismatch) |
| U08 | Aware non-UTC timestamps normalize to canonical UTC |
| U09 | Naive datetimes are rejected |
| U10 | Blank/oversized fields and secret-like values are rejected without echoing the value |
| U11 | The semantic digest is a stable hard-coded length-prefixed SHA-256 |
| U12 | Distinct record versions/providers produce distinct semantic keys |
| U13 | Same semantic tuple with a changed payload (`observed_at`) is bound for conflict, not update (also asserted end-to-end in U19) |
| U14 | `requires_auth=True` stays an untrusted boolean claim; no READY/access state is produced |
| U15 | `REGISTRY_POLICY` decisions reject a `ReconAssessment` reference (never fabricated) |
| U16 | `AGENT_RECON` decisions require a real `ReconAssessmentId` |
| U17 | Empty or duplicate evidence observation IDs are rejected |
| U18 | Outcome/status mismatch, decision-key mismatch, and event-type mismatch are rejected |

### Unit matrix — application/persistence (`tests/unit/app/test_source_discovery.py`, `tests/unit/infrastructure/persistence/postgresql/test_discovery_repositories.py`)

| ID | Assertion |
| --- | --- |
| U19 | `SourceDiscoveryService.ingest` persists observations only: no candidate/source/endpoint/policy/run/event/access rows, exact replay returns the original identity/acceptance time, same-key payload change conflicts, provider/record-version separation holds |
| U20 | `asyncio.CancelledError` during ingest propagates and rolls the unit of work back with nothing persisted |
| U21 | Pending access evidence requires an existing candidate and an observation that claims authentication; replay returns the same immutable evidence; candidate status is unchanged |
| U22 | Qualification replay returns the original decision with no second event; stale expected state conflicts with zero effects; missing evidence/recon provenance is rejected |
| U23 | PostgreSQL repository adapters marshal parameters and map rows exactly, surface controlled outcomes as typed bounded errors, and propagate cancellation |

### Real-PostgreSQL vertical slice (`tests/integration/persistence/test_discovery_repository.py`)

Runs through the real `SourceDiscoveryService -> PostgresDarkulaSpi ->
PostgresUnitOfWork -> versioned stored function -> PostgreSQL` path; direct
SQL is used only for seeding/verification.

| ID | Scenario |
| --- | --- |
| I01 | `0009` applies after frozen `0001`-`0008`; new tables/functions/constraints exist (`test_migrations.py`) |
| I02 | Valid locator round-trips with the correct locator, UTC timestamps, and semantic key |
| I03 | Ingesting the same payload twice yields one row and returns the original identity/time |
| I04 | Concurrent identical ingests converge on one row without deadlock |
| I05 | Concurrent same-key conflicting payloads produce one winner and one `ConflictError` with no overwrite |
| I06 | The same URL from two providers yields two distinct immutable observations |
| I07 | A new provider record version yields a second immutable observation and retains the original |
| I08 | A name-only observation persists a null locator with no candidate/source/policy/access effects |
| I09 | A `requires_auth=True` claim without a candidate writes no access evidence |
| I10 | Pending evidence for an existing candidate is orthogonal: one `CREDENTIALS_PENDING` record, candidate status unchanged |
| I11 | Pending evidence replay creates no second row and no status mutation |
| I12 | Rollback after insert leaves no observation |
| I13 | A valid `REGISTRY_POLICY` decision persists one decision and exactly one existing candidate event/status transition with no `ReconAssessment` |
| I14 | Exact qualification replay appends no second event/decision |
| I15 | A competing/stale expected state loses with `ConflictError` and no partial rows |
| I16 | `AGENT_RECON` with a nonexistent or mismatched recon assessment is rejected atomically |
| I17 | `AGENT_RECON` with a genuine seeded assessment links decision + event + transition atomically |
| I18 | Missing candidate/evidence surfaces a typed bounded error that echoes no values or table names |
| I19 | Existing PR 10 recon, PR 16 collection, crawler, extraction, and source-analysis suites remain unchanged and pass |

### PR 17-F qualification integrity regressions

These regressions pin the qualification persistence boundary (F1 atomicity,
F2 exact replay, F3 cited registry provenance). In-memory parity tests live in
`tests/unit/app/test_source_discovery.py`; the adapter event-identity check
lives in `tests/unit/infrastructure/persistence/postgresql/test_discovery_repositories.py`;
the same scenarios run against real PostgreSQL in
`tests/integration/persistence/test_discovery_repository.py`.

Negative tests catch the typed error and then **commit the unit of work**
before asserting readback, so a conflict path is proven side-effect-free
independently of caller rollback timing.

| ID | Layer | Assertion |
| --- | --- | --- |
| F1 | PostgreSQL | A forced duplicate `qualification_decision` primary key (distinct semantic key/event, second INSERT) rolls back the candidate status update and the event insert even though the caller commits after catching `ConflictError` |
| B-U01 | adapter | `event.event_id != decision.event_id` raises `ValueError` before any SQL is executed |
| B-U02 | in-memory | Exact replay with a regenerated `decision_id` returns the original decision and appends no event |
| B-U03 | in-memory | Same key with a changed event identity conflicts; state unchanged |
| B-U04 | in-memory | Same key with a changed `evaluated_at` conflicts; state unchanged |
| B-U05 | in-memory | Same key with a changed `event.occurred_at` conflicts; state unchanged |
| B-U06 | in-memory | Same key with changed `context`, `reason`, or `provenance` conflicts; state unchanged |
| B-U07 | in-memory | JSON object key order alone is an exact replay |
| B-U08 | in-memory | Same-key replay before commit returns the staged decision without a duplicate event (committed and pending uniqueness parity) |
| C-U01–C-U07 | in-memory | Cited registry evidence must match provider/record with `REGISTRY` detection; uncited matches, wrong provider/record, and non-registry detections are rejected; mixed evidence with one match and `AGENT_RECON` remain accepted |
| B-I01 | PostgreSQL | Exact replay with a regenerated `decision_id` returns the original stored ID/time and appends no event |
| B-I02–B-I04 | PostgreSQL | Changed event identity, `evaluated_at`, or event `occurred_at` conflicts with no decision/event change |
| B-I05 | PostgreSQL | Changed `context`, `reason`, or `provenance` conflicts with no change; `JSONB` key ordering alone replays (B-I06) |
| C-I01–C-I07 | PostgreSQL | Cited `REGISTRY` provider/record provenance is required and enforced before mutation; a matching but uncited observation is insufficient; `AGENT_RECON` is unaffected |

### Schema and safety notes

`0009_discovery_foundation.sql` adds three tables
(`discovery_observation`, `access_evidence`, `qualification_decision`) and
new `_v1` stored functions only. Replay identity is the five-column semantic
tuple plus a UNIQUE `semantic_key`; access evidence uses two partial unique
indexes because PostgreSQL treats NULLs as distinct; decision evidence is a
bounded `uuid[]` (1-64) with FK-style existence verification inside the
stored function. All production SQL lives in versioned stored functions —
the SQL boundary guard
(`tests/unit/infrastructure/persistence/postgresql/test_sql_boundary.py`)
covers the new adapter modules.

## PR 18 testing status — bounded registry polling

PR 18 keeps the external boundary fake and everything behind it real: the
deterministic `FakeSourceRegistry` (Fake World transport) feeds the **real**
RansomLook parser/adapter, `RegistryPoller`, `SourceDiscoveryService`,
`PostgresDarkulaSpi`, and PostgreSQL stored functions. No test contacts
RansomLook, Tor, the Internet, the crawler, or an LLM.

### Unit matrix — adapter/transport (`tests/unit/infrastructure/discovery/`)

| ID | Boundary | Expected assertion |
| --- | --- | --- |
| U01 | valid `GET /api/groups` list | accepted in original order; bytewise sorting/keyset selection is the poller's job |
| U02 | duplicate/mixed/empty/oversize/object index, malformed JSON, duplicate keys, `NaN`, invalid UTF-8, depth > 16 | typed `RegistrySchemaError`; no cursor progress |
| U03 | `[group, posts]` detail | only safe group/location fields become `DiscoveryInput`; posts never mapped |
| U04 | injected `screen`/`source`/`header`/`init_script`/`browser`/`meta`/posts | none appears in claims, versions, or errors |
| U05 | private group/location | skipped and counted; no corresponding claim |
| U06 | `available: false` | no access evidence, readiness, or qualification change |
| U07 | valid absolute `.onion`/HTTPS | `LOCATOR` with PR 17 normalization; target is never fetched |
| U08 | bare `.onion`/host name | `NAME_ONLY`; no scheme inference |
| U09 | unsupported scheme/credentials/encoded separators/control | per-record skip; no raw value echoed |
| U10 | missing `slug` and `fqdn` | `MISSING_REFERENCE` skip |
| U11 | both `slug` and `fqdn` | slug wins; `fqdn` is not retained |
| U12 | aware `updated` | UTC normalization plus a version revision |
| U13 | missing/naive/invalid `updated` | poll-observation-time fallback; stable digest |
| U14 | reordered JSON keys | identical record ID/version |
| U15 | changed safe field | new immutable version; earlier observation retained |
| U16 | contradictory same-key payload | existing `ConflictError`; checkpoint unchanged |
| U17 | two contradictory same group+slug records in one detail | `RegistryRecordConflict`; no advance |
| U18 | two records with the same normalized locator | distinct provider records/observations |
| U33 | entry name path injection (`/`, `?`, `#`, scheme, control) | rejected before transport; fixed host only |
| U38 | hostile provider values | exceptions/logs contain no URL, secret, body, or identifier |
| U28/U29 | `429` numeric/malicious `Retry-After` | `0..3600` honored, otherwise fixed `900`; no unbounded sleep |
| U30 | `503` then success/failure | exactly one retry; failure defers |
| U31 | `401`/`403`/`3xx`/`404` | typed failures; zero redirects/auth fallback |
| U32 | oversized response / deep JSON | bounded failure before parsing/advance |

### Unit matrix — poller/checkpoints (`tests/unit/app/`, `tests/unit/domain/`)

| ID | Boundary | Expected assertion |
| --- | --- | --- |
| U19 | no checkpoint | at most `min(max_entries, 10)` entries; checkpoint at last completed entry |
| U20 | cursor at end | CAS reset to `NULL`, no same-run wrap |
| U21 | new entry earlier than cursor | not lost; captured after the next pass reset |
| U22 | one invalid record in a valid detail | skip counted; accepted siblings persist |
| U23 | failure after a committed record | cursor unchanged; retry replays exactly |
| U24 | clock advanced, no provider timestamp | stored `observed_at`/ID/`ingested_at` reused, no conflict |
| U25/U26 | concurrent first writer, same/changed payload | exactly one reread; converged replay or bounded conflict |
| U27 | checkpoint CAS loser | `CHECKPOINT_RACE`; no cursor overwrite |
| U28/U29 | index/detail `429` | cooldown persisted; a run inside it performs no HTTP |
| U34 | cancellation | propagates; no advance/defer and no UoW across HTTP |
| U35/U36 | configuration/CLI | defaults off, bounds enforced, unknown keys rejected, `--live` triple gate |
| U37 | Fake World registry | exactly four synthetic entries, deterministic call log, GET only |
| U39/U40 | checkpoint staging/CAS | generation `0 -> 1`, monotonic CAS, cursor/cooldown invariants, rollback discarded |

### Real-PostgreSQL vertical slices (`tests/integration/persistence/test_registry_polling.py`)

Canonical path: Fake World transport -> real RansomLook parser/adapter ->
`RegistryPoller` -> real `SourceDiscoveryService` -> real
`PostgresDarkulaSpi` -> `discovery_observation_insert_v1` /
`registry_poll_checkpoint_*_v1` -> database readback.

| ID | Real-DB path | Assertion |
| --- | --- | --- |
| I01 | migrate `0010` | table/functions exist; ledger head is `0010_registry_polling.sql` |
| I02 | absent checkpoint | generation `0`, cursor `NULL` |
| I03/I04 | first advance + stale CAS | generation `1`; stale CAS raises without change |
| I05/I06 | defer then cooldown / rollback | cursor preserved, generation increments, cooldown survives a new SPI; rollback discarded |
| I07 | one fake registry group | group+location observations persist; every forbidden table stays empty |
| I08 | repeated pass after reset | counts, IDs, `observed_at`, `ingested_at` unchanged |
| I09 | changed safe field | new version coexists; earlier observation unchanged |
| I10 | invalid + valid location | only valid persists; skip counted; checkpoint advances |
| I11 | mid-group failure | cursor unchanged; next run replays then progresses |
| I12 | `429` after index | cooldown persisted, cursor unchanged, no HTTP inside cooldown |
| I13 | `503` then success | exactly two attempts for the failing request; one completed checkpoint |
| I14 | malformed/private group | no unwanted observations; malformed keeps checkpoint, private excluded and advances |
| I15 | two concurrent pollers | exactly one `COMPLETED` and one `CHECKPOINT_RACE`; no duplicate observations |
| I16 | clock changes across runs | first-seen time retained; no same-key conflict |
| I17 | same normalized locator, distinct records | distinct evidence IDs; zero candidates |
| I18 | PR 17 exact replay after `0010` | PR 17 behavior unchanged; SQL boundary guard passes |
| I19 | SPI restart | persisted cursor resumes at the next selected entry |
| I20 | external calls | only the fake transport is used; no Tor/crawler/LLM/network dependency |

`schema and safety` note: `0010_registry_polling.sql` is additive; migrations
`0001`–`0009` remain byte-identical. The checkpoint stores only a bounded
provider ID, exact entry name, generation, and timestamps — no raw registry
payload, locator, secret, candidate, or source identity.

### PR 18-F registry safety/error-classification fixup

The transport is now HTTPX asynchronous streaming with one **elapsed-time
deadline per physical attempt** covering connect, status/headers, raw body
streaming, and close. This is deliberately stronger than an HTTPX socket
idle timeout: a provider trickling bytes can still exceed the total attempt
budget. Timeout and caller cancellation cancel the socket read and close the
connection; there is no `asyncio.to_thread`/`urllib` worker left running.
Non-200 response bodies are never consumed, and transport/connect/read/
protocol failures map to a bounded `RegistryTransportError` (the adapter owns
the single retry). All new tests are offline: HTTPX `MockTransport` for
response-shape cases, one loopback `asyncio.start_server` trickle test for the
real deadline/cancellation, and Fake World transport for adapter/poller/
PostgreSQL slices.

| ID | File | Coverage |
| --- | --- | --- |
| T01 | `tests/unit/infrastructure/discovery/test_http.py` | fixed origin, validated path, exact timeout/byte caps to the private async helper |
| T02 | same | schemed/authority/query/fragment/backslash/dot/long/control paths rejected before I/O |
| T03 | same | GET-only shape, `Accept-Encoding: identity`, no body/auth/cookie; hostile `HTTP(S)_PROXY` cannot redirect the request |
| T04 | same | 200 raw bytes exactly at the cap are returned undecoded |
| T05 | same | invalid/negative/over-cap `Content-Length` and actual stream overage fail as schema |
| T06 | same | gzip/other content encodings are rejected without decoding |
| T07 | same | `429`/`3xx`/`5xx` metadata is returned with `body == b""`; error-body streams are never read |
| T08 | same | connect/read/timeout/remote-protocol HTTPX failures map to bounded transport failure |
| T09 | same | real loopback chunked trickle exceeds the elapsed deadline, connection closes, wall clock < 1.5 s |
| T10 | same | caller cancellation propagates unchanged and closes the loopback connection |
| T11 | `tests/unit/infrastructure/discovery/test_ransomlook.py` | group `private` absent/false/true/null/string/number/list/object type matrix |
| T12 | same | location malformed flags skip one `INVALID_SCHEMA`; `private=true` skips one `PRIVATE`; valid siblings remain |
| T13 | same | `.`/`..` entries cause zero detail HTTP, one `INVALID_REFERENCE` skip, no raw `ValueError` |
| T14 | same | `Retry-After` ASCII/1-4-digit/0-3600 matrix; Unicode digits, HTTP dates, and 10k-digit values never raise |
| T15 | same | 503-then-200 issues exactly two GETs and waits `max(0.5, interval)` (2.0 in production-shaped test) |
| T16 | same | transport exception twice issues two GETs and waits the nominal 0.5 s when interval is 0 |
| T17 | same | cancellation during the retry sleep propagates and issues no second request |
| T18 | `tests/unit/app/test_registry_polling.py` | contradictory provider duplicate -> `PERMANENT_ERROR`, cursor/generation unchanged, no cooldown |
| T19 | same | partial committed sibling + conflict -> corrected rerun replays and advances only after success |
| T20 | `tests/unit/scripts/test_registry_poll.py` | permanent provider outcome exits 2; immutable replay `ConflictError` still exits 4 |
| T21 | app tests | transport failures keep `TEMPORARY_ERROR` + 900 s defer and exact replay |
| T22 | app tests | invalid/huge/Unicode `Retry-After` on detail yields 900 s cooldown and no exception |
| T23 | app tests | caller cancellation during detail work causes no CAS advance and no durable defer |
| T24 | app tests | CAS race and end-of-pass cursor reset semantics unchanged |
| T25 | script tests | triple live-enable gate and existing settings bounds unchanged |

Real-PostgreSQL regressions (Fake World transport, real parser/poller/service/
`PostgresDarkulaSpi`):

| ID | Scenario |
| --- | --- |
| I21 | contradictory detail -> `PERMANENT_ERROR`; no cursor advance; committed observations immutable; forbidden tables empty |
| I22 | corrected detail -> exact replay of committed observations, no duplicates, checkpoint advances after entry completion |
| I23 | malformed group privacy flag -> `PERMANENT_ERROR` and no cursor/observations; corrected detail then completes |
| I24 | valid + malformed-private + true-private locations -> only valid ingests; two skips; checkpoint advances |

A `404` detail remains temporary; `401`/`403`, any `300..399` (including
`304`), unexpected statuses, malformed index/detail, and oversize/deep JSON
remain permanent; `429` uses the ASCII-bounded `Retry-After` or 900 s
fallback. No test performs a live RansomLook/Tor/Internet request.

### PR 18-F2 malformed-input boundary corrective

Every untrusted registry string/header used for sorting, path construction,
digest identity, or numeric conversion is validated at the existing trust
boundary so malformed data produces a bounded typed outcome rather than an
unclassified Python exception. This adds no schema, dependency, network, or
admission change.

| ID | File | Coverage |
| --- | --- | --- |
| F2-U01/U02 | `tests/unit/domain/test_registry_poll_checkpoint.py` | valid non-ASCII cursors unchanged; lone high/low UTF-16 surrogates raise fixed-message `ValueError` with no value echo |
| F2-U03 | same | a checkpoint cannot be constructed with a surrogate cursor |
| F2-U04 | `tests/unit/infrastructure/discovery/test_ransomlook.py` | escaped surrogate in the JSON index -> sanitized `RegistrySchemaError`, not `UnicodeEncodeError` |
| F2-U05 | same | surrogate group-name mismatch stays the existing fixed schema error; no identity synthesis |
| F2-U06 | same | direct surrogate `for_entry` -> zero HTTP, one `INVALID_REFERENCE` skip |
| F2-U07 | same | surrogate location skipped while valid sibling/group claims persist; no surrogate in claims |
| F2-U08 | same | surrogate `slug` falls back to a safe `fqdn`; no surrogate retained or hashed |
| F2-U09 | same | valid non-ASCII NAME_ONLY reference produces stable identity/version across runs |
| F2-U10/U11 | `tests/unit/app/test_registry_polling.py` | surrogate index from a provider-neutral stub or real adapter -> `PERMANENT_ERROR` with zero detail requests, observations, or checkpoint changes |
| F2-U12..U15 | `tests/unit/infrastructure/discovery/test_http.py` | `Content-Length` width/grammar bounded before `int()`; overlong/Unicode/sign/blank -> sanitized schema error; 16 zeroes valid, numeric overage rejected |
| F2-U16 | same | overlong declared length rejected before any stream read; stream closed |
| F2-U17 | same | mock stream yields one chunk then raises `httpx.ReadError` -> `RegistryTransportError('registry request failed')`, sanitized message, `aclose()` called |
| F2-U18/U19 | same / existing modules | existing trickle-deadline, cancellation, 429/503, privacy, duplicate-conflict, replay and ordering suites unchanged |
| F2-I01 | `tests/integration/persistence/test_registry_polling.py` | real parser/poller/service/PostgreSQL: surrogate index -> permanent/no writes/no checkpoint; corrected index -> completed cursor/generation 1; forbidden tables empty |
| F2-I02 | same | real DB: surrogate location skipped, group+valid location persisted (2 rows), checkpoint advances; cursor reset replay preserves identities/timestamps |

Surrogates are written as escaped Python literals and escaped JSON bytes; no
invalid UTF-8 is embedded in source files. Mid-stream failure uses HTTPX
`MockTransport`, not the live provider. Header parsing is tested directly and
through the async transport; no live RansomLook/Tor/Internet request occurs.

## PR 19 testing status — candidate admission and policy-routed qualification

PR 19 keeps the external boundaries fake and everything behind them real.
Unit tests use the composed in-memory admission fake
(`tests/support/admission_fakes.py`, which reuses the existing PR 10/17 fakes
and their commit/rollback helpers) and the production
`CandidateAdmissionService`, `CandidateQualificationService` and
`ReconCoordinator`. Integration tests use the real `PostgresDarkulaSpi`,
migration `0011_candidate_admission.sql`, PR 17 ingest, PR 19 stored
functions and the real `ReconCoordinator`; only the registry HTTP transport
and LLM/crawler boundaries are faked. No test contacts RansomLook, Tor, the
Internet, or a live provider.

### Unit matrix — domain and application

| ID | Scenario | Assertion |
| --- | --- | --- |
| U19-01 | name-only observation | ledger `UNRESOLVED_NAME`; no candidate/event |
| U19-02 | first valid locator | one `DISCOVERED` candidate + event + slot + ledger; observation unchanged |
| U19-03 | exact replay | original candidate/outcome returned; no rewrite |
| U19-04 | new observation, same locator | same candidate; second ledger link; no second `DISCOVERED` |
| U19-05 | two raw spellings, same normalized locator | one slot and candidate |
| U19-06 | different locators, same provider origin | two candidates; read-only alias suggestion; no merge |
| U19-07 | exact managed endpoint | `KNOWN_ENDPOINT` evidence only; no candidate/source write |
| U19-08 | 33 exact endpoint matches | `REVIEW_REQUIRED/EXCESS_ENDPOINT_MATCHES` |
| U19-09 | one exact legacy candidate | reuse UUID; no second event |
| U19-10 | two exact legacy candidates | `REVIEW_REQUIRED`; no slot |
| U19-11 | promoted legacy candidate | `REVIEW_REQUIRED`; no reactivation |
| U19-12 | malformed persisted classification | typed `AdmissionInvariantError`; nothing written |
| U19-13 | injected digest collision | fatal; original slot unchanged |
| U19-14 | cancellation before commit | no candidate/slot/ledger |
| U19-15 | repeated admission race | winner reused; one candidate |
| U19-16/17 | auth claim and replay | one `CREDENTIALS_PENDING` evidence; original ID/timestamp retained |
| U19-18 | auth claim + managed endpoint | no candidate; claim retained |
| U19-19 | batch limits 1 and 50 | exact bounded processing; no reprocessing |
| U19-20 | injected batch failure | prior entry survives; failed entry uncommitted |
| U19-21 | >16 possible aliases | `too_many=True`; no partial list |
| U19-22 | trust disabled | pending route; one pending event; no decision |
| U19-23 | trusted complete record | `REGISTRY_POLICY`/`QUALIFIED`; one decision/event; zero recon |
| U19-24 | trusted but name-only | never qualifies; no candidate |
| U19-25 | unknown provider/record class | pending; no decision |
| U19-26 | conflicting provenance | ambiguous/pending; no decision |
| U19-27 | multiple versions of one record | latest observation cited; history retained |
| U19-28 | source-kind mismatch | pending; no agent |
| U19-29 | disable after success | historic decision kept; no new decisions |
| U19-30 | pending then enabled | qualifies once; no duplicate pending event |
| U19-31/32 | concurrent/exact decision replay | one winner; original decision/event retained |
| U19-33 | terminal/busy candidate | no decision, event, LLM, or crawler |
| U19-34/35 | unauthorized/auth-pending dispatch | no coordinator call; not `REJECTED` |
| U19-36..38 | authorized FakeLLM/FakeCrawler recon | real assessment + `AGENT_RECON` decision + one terminal event per disposition |
| U19-39 | operational recon failure | PR 10 recovery; no fabricated decision |
| U19-40 | final decision conflict | assessment/decision/event rolled back |
| U19-41 | legacy `reconnoiter` | unchanged PR 10 event/status/assessment semantics |
| U19-42 | config fail-closed | default trust off; invalid batch limits/unknown keys rejected |
| U19-43 | telemetry/log fields | bounded outcome/reason/count/policy only |
| U19-44 | immutable observation | bytewise-equal fields incl. null `candidate_id` |
| U19-45 | forbidden side effects | no Source/Endpoint/Policy/run/outbox |

Host-test modules: `tests/unit/domain/test_candidate_admission_domain.py`,
`tests/unit/app/test_candidate_admission.py`,
`tests/unit/app/test_candidate_qualification.py`, and the PR 19 classes
appended to `tests/unit/app/test_recon.py`.

### Real-PostgreSQL vertical slices (`tests/integration/persistence/test_candidate_admission.py`)

| ID | Coverage |
| --- | --- |
| I19-01 | migration head `0011`; admission tables and six `_v1` functions present; `0009`/`0010` still applied |
| I19-02 | Fake Source Registry -> real adapter -> ingest -> admission; group NAME_ONLY unresolved, valid location creates one candidate/event; observations immutable |
| I19-03 | replay + new version same locator -> one candidate, two ledger links, one `DISCOVERED` event |
| I19-04 | two independent SPIs admit concurrently -> exactly one candidate/event, both observations linked |
| I19-05 | seeded managed endpoint -> `KNOWN_ENDPOINT`; no candidate/source/endpoint/collection rows |
| I19-06 | duplicate legacy candidates -> `REVIEW_REQUIRED`; neither mutated; no slot |
| I19-07 | auth claim -> candidate + `CREDENTIALS_PENDING`; unit-of-work rollback removes all admission effects |
| I19-08 | trust off -> pending; enable -> one `REGISTRY_POLICY` decision + one `QUALIFIED` event; zero recon |
| I19-09 | conflicting provider provenance -> no `QUALIFIED` decision, no merge/source |
| I19-10 | two concurrent qualifying SPIs -> one decision/event; typed bounded loser |
| I19-11 | real `ReconCoordinator` + FakeLlm/FakeCrawler -> one assessment, one `AGENT_RECON` decision, one terminal event |
| I19-12 | mid-run status conflict -> assessment/decision/status/event rolled back together |
| I19-13 | cancellation before commit -> no admission data |
| I19-14 | restart SPI -> slot/ledger/decisions survive; replay deterministic |
| I19-15 | audit -> no managed Source/endpoint/policy/run/outbox/credential |
| I19-16 | PR 18 registry-only ingestion still creates zero candidates/ledger rows |

Real-PostgreSQL tests run under `./build.sh --intg` and reset only Darkula
tables. Multi-connection race proofs use two independent
`PostgresDarkulaSpi` instances, not a fake `ConflictError`.

## PR 19-F1 testing status — trusted provenance, endpoint guard, replay, bounded access

This narrow fixup keeps every trust boundary fake and the production logic
real. No test contacts RansomLook, Tor, or the Internet. The positive
approved-pipeline test instantiates the **actual**
`RansomLookDiscoveryAdapter` + actual `StdlibRegistryHttpTransport` + actual
`RegistryPoller`, and replaces only the private
`darkula.infrastructure.discovery.http._async_get_url` execution seam with a
deterministic synthetic-JSON function that asserts the pinned
`https://www.ransomlook.io:443/` origin. This preserves exact production class
identity while eliminating network I/O.

### Unit matrix (U19F-01 .. U19F-36)

| ID | File | Assertion |
| --- | --- | --- |
| U19F-01 | `tests/unit/app/test_registry_polling.py` | generic ingest stores the observation and writes **no** marker |
| U19F-02 | same | duck-typed adapter advertising `ransomlook` cannot mark |
| U19F-03 | same | `RansomLookDiscoveryAdapter(FakeSourceRegistry)` is unverified |
| U19F-04 | same | exact adapter + fixed-origin transport marks and qualifies with no live request |
| U19F-05/U19F-06 | same | adapter subclass / transport subclass cannot mark |
| U19F-07..11 | `tests/unit/app/test_candidate_admission.py` | marker unknown-observation, invalid-provenance, replay timestamp, atomic rollback, cancellation |
| U19F-12..27 | `tests/unit/app/test_candidate_qualification.py` | unverified/mixed provenance, trust-off, post-admission endpoint, missing slot, near-match, pending auth, malformed inputs, over-64 claims, replay/current status, impossible-state invariant, race replay |
| U19F-28..30, U19F-33..36 | same | bounded access probe (0/1/64/512), `_load`/dispatch never call the history list, disabled-after-history, one endpoint probe, limit validation, boolean return |
| U19F-31/U19F-32 | `tests/unit/app/test_recon.py` | context check uses the boolean probe; legacy no-context recon reads no access evidence |

### Real-PostgreSQL vertical slices (I19F-01 .. I19F-16)

`tests/integration/persistence/test_candidate_admission.py` adds:
migration/schema contract; forged generic provenance; fake-adapter
non-attestation; full approved-transport slice (patched HTTPX execution ->
real adapter/poller/service/marker function -> admission -> PR 17 atomic
decision); retroactive verification on exact replay; marker rollback;
post-admission exact endpoint guard; endpoint exactness; missing-slot
invariant; bounded access probe with 0/1/64/512 synthetic rows; legacy recon
preserved; qualified replay after `PROMOTED`; concurrent marked qualification
(two independent SPIs); credential-pending dispatch guard; restart
durability; cancellation/marker immutability. Existing I19-08/I19-10 now seed
the marker precondition while keeping their qualification/concurrency
assertions.

### Local performance benchmark

`tests/performance/bench_pr19_fixup.py` is a standalone program (never
pytest-collected). It seeds synthetic candidates with 0/1/64/512
`CREDENTIALS_PENDING` rows, 0/1/33 exact endpoints, and 1/65 verified IDs
inside one transaction, compares
`access_evidence_list_for_candidate_v1` with
`access_evidence_has_pending_v1` (40 warmup + 200 samples, P50/P95),
measures `candidate_current_endpoint_match_v1` and
`trusted_registry_candidate_ids_v1`, records `EXPLAIN (ANALYZE, BUFFERS,
TIMING OFF, SUMMARY ON)` plans (including the inner `EXISTS` plan proving the
partial index is used), rolls the transaction back, verifies the tables are
empty from a fresh connection, and writes `.benchmarks/pr19f1-local.json`
(untracked). There is no fixed absolute latency target; the only fixed stop is
a P50 regression above 2x the old full-history read at N=512, which is
repeated once before any stop decision.

```bash
./build.sh --qa
./build.sh --sec
./build.sh --intg
./scripts/darkula_postgres.sh start
./scripts/darkula_postgres.sh migrate --apply
mkdir -p .benchmarks
uv run python tests/performance/bench_pr19_fixup.py \
  --warmup 40 --samples 200 --output .benchmarks/pr19f1-local.json
./scripts/darkula_postgres.sh clean
```

Known limits: the marker is application-attested (no cryptographic upstream
signature); the current endpoint guard is a committed-state read and does not
serialize a concurrently committing `SourceEndpoint` insert; the live
RansomLook response contract is not validated in CI.

## PR 19-F2 testing status — stable historical registry qualification replay

Migration `0013_registry_qualification_replay.sql` adds one read-only SQL
function, `qualification_registry_v1_decisions_for_candidate_v1`, returning at
most two `REGISTRY_POLICY` / `candidate-qualification` / `v1` decisions for one
candidate in `(evaluated_at, decision_id)` order. Replay resolves by
candidate and frozen policy identity, never by the newest admission-linked
observation.

### Unit matrix (U19F2-01 .. U19F2-20)

`tests/unit/app/test_candidate_qualification.py` adds
`TestRegistryReplayAfterNewObservations`:

| ID | Assertion |
| --- | --- |
| U19F2-01 | qualify from v1, admit a later v2 same locator, replay exact original decision ID with `QUALIFIED` and no second decision/event |
| U19F2-02 | same after a legal `QUALIFIED -> PROMOTED` transition; replay reports `PROMOTED` |
| U19F2-03 | later unverified observation does not block or requalify historical replay |
| U19F2-04 | later conflicting record/origin claim does not block historical replay |
| U19F2-05 | 65 later admitted versions (66 claims total) still replay without a claim bound |
| U19F2-06 | same-instant later observation does not change replay selection |
| U19F2-07 | trust disabled after qualification: historical replay still returned; unrelated candidate stays default-deny |
| U19F2-08 | later auth-claim observation and pending evidence do not block historical replay |
| U19F2-09 | `AGENT_RECON`-only terminal candidate is not misreported as registry replay |
| U19F2-10 | `DISCOVERED` candidate with no decision keeps the existing trust-off pending behavior |
| U19F2-11 | prospective verified qualification still creates exactly one new decision |
| U19F2-12 | two historical registry v1 decisions fail closed with `AdmissionInvariantError` |
| U19F2-13 | single historical decision with incompatible current status fails closed |
| U19F2-14 | single historical decision with a non-`QUALIFIED` outcome fails closed |
| U19F2-15 | wrong candidate/policy identity fails closed without leaking provider values |
| U19F2-16 | other-policy and `AGENT_RECON` decisions are not treated as registry replay |
| U19F2-17 | controlled conflict retry finds the concurrent winner and replays it |
| U19F2-18 | conflict retry with two registry v1 decisions fails closed without a new write |
| U19F2-19 | historical replay calls exactly one bounded lookup and never `claims_for_candidate`/`list_for_candidate` |
| U19F2-20 | fake lookup returns 0/1/2/2 rows in deterministic order for 0/1/2/3 matching decisions with interleaved `AGENT_RECON` rows |

### Real-PostgreSQL matrix (I19F2-01 .. I19F2-08)

`tests/integration/persistence/test_candidate_admission.py` adds
`TestRegistryQualificationReplayF2`:

| ID | Coverage |
| --- | --- |
| I19F2-01 | real qualification then later v2 admission: exact original decision ID, `QUALIFIED`, `replayed=True`, one decision/event, two ledger rows |
| I19F2-02 | promote, admit v2, replay: original ID, `PROMOTED`, no duplicate `QUALIFIED` event |
| I19F2-03 | later generic/unverified observation: historical replay works, no marker for the new observation |
| I19F2-04 | 66 total admission-linked claims: replay without a claim bound; one decision/event |
| I19F2-05 | trust disabled later: historical replay retained; fresh candidate still default-deny |
| I19F2-06 | test-only second valid registry v1 decision: production service raises `AdmissionInvariantError`, zero writes |
| I19F2-07 | genuine `AGENT_RECON` terminal candidate: no registry replay, existing terminal result, original decision untouched |
| I19F2-08 | SQL function returns exactly two ordered rows; migration head `0013`; `0011`/`0012` functions and tables still present; no new table |

The PR 19-F1 standalone benchmark's only change is its migration-head
precondition (`0013_registry_qualification_replay.sql`); fixture sizes,
sampling, metrics, and safety checks are unchanged.


## PR 20 testing status — controlled public HTTPS crawling

**Default CI is fully offline.** The unit suite and `./build.sh --intg` make
zero public DNS/TCP/HTTP requests. The live path is proven with a real
rootless-Podman topology plus a **test-only fail-closed gateway image** whose
resolver/dialer always deny; production configuration never selects it.

Commands:

```text
uv run pytest tests/unit/crawler/test_public_https_authorization.py
uv run pytest tests/unit/crawler/test_https_egress_policy.py tests/unit/crawler/test_https_gateway.py
uv run pytest tests/unit/crawler/test_live_browser_policy.py
uv run pytest tests/unit/crawler/test_https_smoke_cli.py tests/unit/crawler/test_public_https_security_guards.py
./build.sh --intg   # includes tests/integration/crawler/test_public_https_isolation.py
```

Coverage IDs:

- **U20-01..U20-48** (unit): legacy v1 byte/golden and isolated policy
  preservation; triple live authorization; exact host/path/URL validation;
  hard live ceilings; v2 protocol strictness and controller identity/version
  checks; gateway parser/DNS/IP classification, numeric-IP pinning, rebind,
  timeouts, budgets, cancellation and internal-only bind; browser route/
  WebSocket/method/redirect/request-budget policy; Podman argv/topology/
  cleanup and the CLI double gate; static import/TLS guards.
- **I20-01..I20-15** (real Podman, offline): existing Fake World suite remains
  green (I20-01/I20-15); missing/foreign outer network refusal; fresh
  ephemeral `/24` + pinned gateway/crawler addresses; gateway listener
  reachability; denial of direct public/private/metadata/DNS paths; raw
  CONNECT wrong-target/wrong-port refusal; no ambient proxy/default route;
  concurrent executions use distinct networks; denied gateway yields no
  `completed`; cancellation and forced failure leave zero crawler, gateway or
  ephemeral-network resources; cleanup refuses unverified resources.
- **L20-01..L20-05** (manual, operator-approved, never CI): see
  [test_public_https_smoke.md](../tests/manual/test_public_https_smoke.md)
  and [HTTPS_EGRESS_OPERATIONS.md](HTTPS_EGRESS_OPERATIONS.md). A genuinely
  public successful crawl is not claimed by the offline suite.

The integration fixture provisions the exact `darkula-live-egress` outer
network (verifying ownership/non-internal semantics, removing it only when it
created it) and builds/removes its own test gateway image. It never calls the
operator provisioning script and never touches Fake World's `darkula-intg`.


### PR 20-F1 cleanup/telemetry regression coverage

**Offline only; no public request.** Added on top of U20/I20:

- **U20F1-01..32** in `tests/unit/infrastructure/sandbox/test_podman_sandbox.py`:
  exact `container|network exists` 0/1/125 semantics; fixed 10-second cleanup
  deadlines; bounded two-attempt container/network removal; nonzero `stop`/`rm`
  handling; ownership revalidation before mutation; unknown presence after a
  removal is failure; containment (network retained if a container remains);
  single terminal execution metric for success/failure/cancellation/unexpected
  error; truthful `cleanup_succeeded`; legacy exception-after-launch path; and
  the unchanged owned/unowned protections.
- **I20F1-01..05** in `tests/integration/crawler/test_public_https_isolation.py`:
  a real owned internal network with an attached test container refuses bounded
  removal and stays in place; injected persistent `rm` nonzero remains bounded
  with the real container intact; the offline live topology still verifies
  cleanup; unverified resources are untouched; and the legacy v1 Fake World
  crawl still completes with verified cleanup.
- **L20-01..05 remain NOT RUN** pending operator approval and controlled
  redirect/TLS fixtures. Offline QA success does not claim a real public crawl.
