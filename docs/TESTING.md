# Darkula Testing Strategy

## Principles
Testing must be deterministic by default, exercise real application/domain code, and reuse realistic Fake World scenarios. Fake at external/variable boundaries rather than replacing the behavior under test.

## Levels
### Unit
Fast tests without PostgreSQL, Redpanda, OTEL backend, browser/Tor, or live LLM. Cover state transitions, policies, message mapping, normalization helpers, deterministic extractors, geographic data handling, configuration precedence, and similar logic.

### Component/scenario integration
Run real Darkula orchestration against Fake World data. Prefer FakeLlmClient, FakeDataStream, InMemoryObjectStore/local test store, and test persistence as appropriate. Exercise the real ReconAgent/SourceAnalyst implementation while faking its LlmClient for deterministic agent paths.

### Infrastructure integration
Exercise real adapters: PostgreSQL, Redpanda DataStream, ObjectStore implementations, OTEL Collector/export, and crawler sandbox/controller as introduced. Verify retries, duplicate delivery/idempotency, outbox publication, trace propagation, and clean shutdown.

### End-to-end Fake World
Exercise collection -> normalization -> extraction -> source assessment through production-like infrastructure without contacting real criminal resources.

## Fake implementations
FakeLlmClient returns deterministic structured responses and records invocations for assertions. FakeDataStream supports deterministic produce/consume behavior and duplicate/retry scenarios. InMemoryObjectStore supports artifact tests. Agent fakes may exist for tests above an agent boundary but should not replace real agents in primary pipeline integration tests.

## Reliability tests
Assume at-least-once stream delivery. Test duplicate messages, consumer crash before/after durable commit, outbox retry, poison/non-retryable messages, cancellation, and replay. Persisted processing must be idempotent.

## Configuration tests
Explicitly verify: default < base < profile < local override < non-empty environment variable. Unset and empty environment variables must not override lower-precedence values.

## Observability tests
Use OTEL test/in-memory exporters/readers where appropriate. Assert critical spans/metrics and safe error recording. Infrastructure tests verify Collector export. Never require sensitive payload capture for test success.

## Security tests
Test sandbox input/output constraints, path/origin budgets, malicious/malformed content, prompt-injection-like text, oversized artifacts, unsafe filenames, redirects, session expiry, and credential/log redaction.

## Relationship to evaluations
Integration tests answer whether engineering contracts execute correctly. Evals answer whether model-backed reasoning/extraction is good enough. Both should reuse the same scenario definitions and Fake World truth where possible; do not maintain unrelated test and eval universes.
