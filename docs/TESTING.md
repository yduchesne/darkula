# Darkula Testing Strategy

## Commands

The deterministic local quality gate:

```text
uv sync --locked
./build.sh --qa       ruff format check, ruff lint, strict mypy, unit tests, coverage >= 85%
./build.sh --intg     Darkula-owned Podman PostgreSQL at 35432:5432, migrations, real-PostgreSQL integration tests
./build.sh --sec      bandit source scan + pip-audit dependency audit
uv run pre-commit run --all-files   optional local dev gate
```

Async tests use pytest-asyncio **strict mode** (`@pytest.mark.asyncio`);
async fixtures use `pytest_asyncio.fixture`. The unit suite (`--qa`) runs
fully offline and deterministically (integration tests carry the
`integration` marker and are deselected by default). Coverage measures the
`darkula` package with a hard `fail_under = 85` gate in CI and locally.

## PostgreSQL integration (PR 4)

`./build.sh --intg` runs the real-PostgreSQL integration suite:

1. verifies Podman availability;
2. starts/provisions **only** Darkula-owned resources
   (`scripts/darkula_postgres.sh start`: container `darkula-postgres` with
   `darkula.owned=true`, pinned `docker.io/library/postgres:18.2`, volume
   `darkula-postgres-data` created with its own `darkula.owned=true` label,
   exact host mapping `35432` → container `5432`);
3. waits for `pg_isready`; applies migrations
   (`scripts/darkula_migrate.py`, ledger `darkula_schema_migrations`);
4. runs `uv run pytest tests/integration -m integration --no-cov -q`;
5. cleans up exactly the Darkula-owned resources while preserving the
   original exit status (`scripts/darkula_postgres.sh clean`).

The lifecycle tool fails fast if host port `35432` is occupied by a
non-Darkula listener, if a resource bears a Darkula name without Darkula
labels, or if Podman cannot run; it never prunes, never touches foreign
resources, and never chooses another port. Developers wanting a persistent
local instance may run `scripts/darkula_postgres.sh start|stop|clean`
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
mapping from exact-resource Podman metadata.

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
crash before/after durable commit, outbox retry, poison/non-retryable
messages, cancellation, and replay. Persisted processing must be
idempotent. PR 3's `FakeDataStream` replay seam supports these tests
before the real broker arrives.

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
