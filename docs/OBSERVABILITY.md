# Darkula Observability

## PR 2 status — decorator contract only

PR 2 delivered the decorator-name/signature contract in `darkula/telemetry/decorators.py`: `traced`, `timed`, and `counted` for stabilized operation boundaries (sync and async). In PR 2 these are **contract-only no-ops**: they validate static names/attributes at decoration time and preserve call behavior, exceptions, cancellation, and function metadata exactly, but emit no telemetry.

## PR 5 status — DataStream/outbox/consumer instrumentation delivered

PR 5 instruments the reliable-messaging boundaries with the existing
decorator conventions and direct OTEL constructs for dynamic measurements:

- **datastream.publish / datastream.poll / datastream.acknowledge** — one
  span per operation plus counters (`darkula.datastream.<op>.count`,
  `.failed`) and seconds histograms (`<op>.duration`); poll also counts
  returned records (`darkula.datastream.poll.records`);
- **outbox.claim / outbox.publish / outbox.mark_published** — spans,
  operation counters/durations, `darkula.outbox.claimed` /
  `darkula.outbox.published` row counters, and
  `darkula.outbox.publish.failed`;
- **consumer.process** — one span per poll/process batch,
  `darkula.consumer.process.count` / `.duration`, and
  `darkula.consumer.process.duplicate` for deduplicated redeliveries;
- every dynamic metric/span attribute is limited to the bounded logical
  names `darkula.stream` and `darkula.consumer` — never message_id,
  correlation_id, causation_id, routing-key values, URIs, payloads, or
  secrets; no operation captures arguments or results.

### Trace propagation through transport metadata
The Redpanda adapter injects the current W3C context (`traceparent` +
`tracestate` only, via `TraceContextTextMapPropagator`) into broker
transport headers on publish — never into the message payload — and
extracts it on poll so the consumer-side `datastream.poll` span is parented
to the upstream trace. Extraction is best-effort and never fails delivery.
OTEL baggage is deliberately NOT propagated: arbitrary baggage must not
become a covert channel (see `Sandbox propagation`).

## PR 3 status — OTEL SDK emission delivered

PR 3 upgraded the three decorators to emit real OpenTelemetry while keeping every PR 2 preservation guarantee, added the support modules
`telemetry/attributes.py`, `telemetry/tracing.py`, `telemetry/metrics.py`, and `telemetry/setup.py`, and pinned `opentelemetry-sdk` (API + SDK). PR 16 added an optional OTLP/HTTP exporter path (see below); the default remains local-only with no network.

Delivered decorator behavior:

- `traced` — exactly one span per invocation (sync/async), marked `ERROR` on an ordinary exception (which propagates unchanged); cancellation propagates and is never an ordinary error; only validated static attributes are attached; arguments/results are **never** captured.
- `timed` — one `time.perf_counter()` seconds histogram point tagged `darkula.outcome = success|error`; cancellation records no point; the metric name must use the canonical `<operation>.duration` suffix and the seconds unit; the same exception propagates after its error point is recorded.
- `counted` — increments exactly once **at operation entry**, so success, failure, and cancellation each count as one invocation.
- Static attributes are bounded (key <= 200 chars; value <= 256 chars), control-free, and reject secret-like names/values at decoration time, so prompts/tokens/credentials/unbounded content can never reach telemetry through the decorators.
- Disabled/unconfigured OTEL falls back to the API no-op proxy: all decorators remain behavior-preserving.

Tracer/meter resolution goes through the injectable module seams `get_tracer` / `get_histogram` / `get_counter`; deterministic tests bind in-memory SDK providers through those seams (`tests/unit/conftest.py`, `tests/support/otel.py`) and never touch global OTel state or any external service. `configure_telemetry` composes local `TracerProvider`/`MeterProvider` for the configured `service.name` without exporters, network, or background threads.

PR 15 delivered the LangSmith adapter behind the existing SPI. PR 16 delivered real OTLP trace/metric export, a local Collector, and local Jaeger/Prometheus backends (see the PR 16 section). The Langfuse agent-observability backend remains explicitly unavailable.

## PR 8 status — normalization and artifact telemetry

PR 8 instruments the content boundary with the existing decorators and
bounded direct counters:

- **content.normalize** — one span per normalization plus
  `darkula.content.normalize.count` and `content.normalize.duration`
  (outcome success/error), emitted by
  `DeterministicContentNormalizer.normalize`;
- **artifact.store** — one span per storage operation plus
  `darkula.artifact.store.count` / `artifact.store.duration`, and dynamic
  low-cardinality counters `darkula.artifact.stored` (new physical object),
  `darkula.artifact.deduplicated` (verified reuse), and
  `darkula.artifact.bytes` (bytes stored on a new object).

Attributes are limited to the static, bounded decorator attributes and the
``darkula.outcome`` dimension. Telemetry never carries a URI, object key,
content hash, title, source name, credential, or content body -- no
high-cardinality or secret-bearing identifier (section 19 of the PR 8 plan).

## Operational telemetry
Darkula standardizes client-side operational telemetry on OpenTelemetry:
```text
Darkula -> OTEL SDK/API -> OTEL Collector -> Prometheus
                                      \-> Loki
                                      \-> Jaeger
```
Grafana is the expected local visualization layer. Application code must not depend directly on Prometheus, Loki, or Jaeger client APIs.

## Instrumentation convention
Prefer Darkula-owned OTEL-backed decorators around meaningful method/function boundaries:
- traced operation
- duration/timer
- invocation counter

Use direct OTEL constructs when measurements arise inside an operation: page/byte counts, retries, queue/consumer observations, intermediate events, dynamic attributes, or current-span events. Decorators standardize repetitive OTEL usage; they are not an abstraction intended to make OTEL replaceable.

## Correlation
Propagate appropriate identifiers across asynchronous boundaries: trace/span context, source_id, candidate_id, collection_run_id, crawl_request_id, content_id, agent_run_id, llm_invocation_id, and correlation/causation IDs as applicable. DataStream adapters own trace-context propagation in broker transport metadata (PR 5 delivers injection + extraction); the domain payload never contains trace headers.

## Agent/LLM observability
AI-specific observability uses a Darkula-owned AgentObservability interface with adapters such as LangSmith and Langfuse. Vendor types/concepts must not leak into agents or LlmClient. LlmClient is a central instrumentation point for model/provider, latency, usage, retries, outcome, and structured-output validation.

## Sensitive data
Collected dark-web material may include credentials, PII, stolen data, and adversarial prompt content. Raw prompts/responses/content are not assumed safe for SaaS telemetry. Capture policy must support NEVER/REDACTED/FULL-like semantics with conservative defaults; secrets and credentials require mandatory redaction. Structured logs must avoid unrestricted content and authentication material.

## Crawler/DataStream telemetry
Track crawl request/success/failure, pages/bytes, duration, timeouts, status classes, redirects, rate limiting, sandbox termination, navigation/normalization failures, duplicates/content changes. Track stream produce/consume/processing duration, failures/retries, serialization failures, lag/depth where available, and dead-letter/non-retryable outcomes.

## Sandbox propagation
Only minimal approved trace context crosses into disposable sandboxes; arbitrary OTEL baggage must not become a covert channel for sensitive application state.

## PR 9 status — collection telemetry delivered

PR 9 instruments the managed-source collection lifecycle with direct OTEL
counters/histograms/spans (no decorator attributes beyond the bounded
vocabulary):

- **Spans** — `collection.schedule_due`, `collection.execute`,
  `collection.crawl` (per endpoint), `collection.ingest` (per observation),
  `collection.finalize`, `collection.worker.poll`. No ID/URI/payload
  attributes; only the frozen low-cardinality set.
- **Counters** —
  `darkula.collection.run.created`, `.started`, `.succeeded`, `.failed`,
  `.cancelled`, `.reclaimed`, `darkula.collection.pages`,
  `darkula.collection.observations`, `darkula.collection.content.created`,
  `darkula.collection.content.deduplicated`,
  `darkula.collection.worker.command.invalid`,
  `darkula.collection.worker.run.unknown`, and
  `darkula.collection.worker.process.count`.
- **Histogram** — `darkula.collection.run.duration` (seconds).
- **Attributes** — `darkula.outcome` in {`success`, `error`} only;
  failure-code categories are low cardinality and bounded.

Prohibited attributes everywhere (mirroring PR 8): run/source/policy IDs,
endpoint URIs, source names, content hashes, object keys, titles, and
credentials. Persisted failure summaries are additionally validated
secret-free by the domain model before finalization.

## PR 10 status — reconnaissance telemetry delivered

PR 10 instruments the bounded recon workflow with direct OTEL
counters/histograms/spans plus provider-neutral `AgentObservability`
scopes per model attempt:

- **Spans** — `recon.workflow` (whole execution) and `recon.crawl` (one
  authorized inspection). Bounded attributes only (`darkula.outcome`,
  disposition, failure category); never candidate IDs, URLs, prompts, or
  content.
- **Counters** —
  `darkula.recon.workflow.started`, `darkula.recon.workflow.completed`
  (with `darkula.recon.disposition` in {`QUALIFY`, `NEEDS_MORE_RECON`,
  `REJECT`}), `darkula.recon.failures` (with
  `darkula.recon.failure_category` from the bounded typed-error vocabulary),
  `darkula.recon.inspections`, `darkula.recon.invalid_requests`,
  `darkula.recon.model.attempts`, `darkula.recon.llm_failures` (with
  `darkula.recon.llm_error` in the `LlmErrorCode` vocabulary), and
  `darkula.recon.structured_output_repairs`.
- **Histogram** — `darkula.recon.workflow.duration` (seconds).
- **AgentObservability** — one scope per `LlmClient` call with
  `AgentOperationMetadata(operation_name="recon.assess",
  agent_name="recon_agent", prompt_version="recon-v1",
  model_profile="recon")`. Prompts, model output, source content,
  credentials, and hidden reasoning are never representable in metadata.

Prohibited attributes everywhere (mirroring PR 8/PR 9): candidate/event/
assessment IDs, candidate entrypoints/URLs, page text, evidence
references/excerpts, testimony, prompts, model output, and credentials.
Spans and metric attributes are asserted content-free and
high-cardinality-free by the unit TS matrix.

## PR 11 status — deterministic extraction telemetry delivered

PR 11 instruments the extraction boundary with the existing decorators plus
one bounded entity counter:

- **Span** — `extraction.extract` (one per extraction execution). No dynamic
  attributes.
- **Counters** — `darkula.extraction.executions` (one per call, incremented at
  entry so success/failure/cancellation each count once) and
  `darkula.extraction.entities` (number of persisted occurrences).
- **Histogram** — `darkula.extraction.duration` (seconds, tagged only
  `darkula.outcome` in {`success`, `error`}).

The fast path that reuses an existing durable result still counts one
execution but performs no ObjectStore read and no extractor work; extractor
name/version, profile name/version, and entity-type vocabulary are bounded and
developer-controlled, so they are the only permitted dimensions.

Prohibited (mirroring PR 8-10): normalized-content/result/entity IDs as
metric labels, URIs, ObjectKeys, content hashes, titles/text, raw or
normalized entity values, and credentials. No argument or return value is ever
captured by the decorators; extraction performs no LLM/agent-observability
call in PR 11.

## PR 12 status — semantic extraction and geographic resolution telemetry

Semantic extraction is instrumented with the existing decorators plus bounded
counters:

- **Span** — `semantic_extraction.extract`.
- **Counters** — `darkula.semantic_extraction.executions` (one per call),
  `darkula.semantic_extraction.entities` (accepted occurrences), and
  `darkula.semantic_extraction.rejected_candidates` (ungrounded/ambiguous).
- **Histogram** — `darkula.semantic_extraction.duration` (seconds, outcome
  dimension only).

Geographic resolution:

- **Span** — `geographic_resolution.resolve`.
- **Counters** — `darkula.geographic_resolution.executions` and
  `darkula.geographic_resolution.status` (bounded attribute
  `RESOLVED`/`AMBIGUOUS`/`UNRESOLVED`).
- **Histogram** — `darkula.geographic_resolution.duration` (seconds).

Prohibited (mirroring PR 8-11): source text, raw/normalized entity values,
location mentions, canonical names, coordinates, prompts, model output,
provider payloads, IDs as metric labels, object keys, hashes, and credentials.
No argument or return value is captured by the decorators; the only dynamic
dimension is the bounded resolution status.

## PR 13 status — relationship-assertion extraction telemetry

Relationship extraction is instrumented with the existing decorators plus
bounded counters:

- **Span** — `relationship_extraction.extract`.
- **Counters** — `darkula.relationship_extraction.executions` (one per call),
  `darkula.relationship_extraction.assertions` (accepted assertions), and
  `darkula.relationship_extraction.rejected_candidates` (unknown ref, missing/
  ambiguous support, endpoint-excluding support, or self-edge).
- **Histogram** — `darkula.relationship_extraction.duration` (seconds).

Prohibited (mirroring PR 11/12): source text, support text, endpoint values,
refs, ids, predicates as labels, prompts, model output, object keys, hashes,
and credentials. No argument or return value is captured by the decorators;
the only attributes are static developer-controlled span/metric names.

## PR 14 status — source-analysis telemetry

Source analysis is instrumented with the existing decorators plus bounded
counters:

- **Span** — `source_analysis.analyze`.
- **Counters** — `darkula.source_analysis.executions` (one per call),
  `darkula.source_analysis.created` (newly persisted assessments),
  `darkula.source_analysis.replays` (semantic replay short-circuits), and
  `darkula.source_analysis.evidence_items` (bounded evidence items presented).
- **Histogram** — `darkula.source_analysis.duration` (seconds).

Prohibited: source/assessment/content/entity/relationship ids, URI/object key/
content hash values, evidence refs, evidence summaries, prompts, model output,
characteristics, scores, credentials, and raw errors. No argument or return
value is captured by the decorators; the only attributes are static
developer-controlled span/metric names.

## PR 15 status — LangSmith agent-observability adapter and evaluation correlation

PR 15 implements the existing provider-neutral `AgentObservability` SPI for
LangSmith in `darkula.infrastructure.observability.langsmith`:

- only bounded, content-free `AgentOperationMetadata` is mapped to LangSmith
  metadata (`darkula.operation_name`, agent, model provider/name/profile,
  prompt version, and safely-representable source/collection identifiers);
- prompts, model outputs, collected content, credentials, and hidden reasoning
  are not representable through the SPI and are never sent;
- the backend is fail-open: start/post/end/patch/flush failures never fail,
  retry, or otherwise alter the observed application operation, and
  `BaseException` (including `asyncio.CancelledError` and `KeyboardInterrupt`)
  is never swallowed;
- LangSmith SDK types stay inside the infrastructure adapter; application and
  domain code only see `AgentObservability`.

Composition fails fast when the LangSmith backend is selected without a
project and API key (no silent no-op fallback). Langfuse remains explicitly
unavailable. Evaluation result correlation uses run/case/scenario/evaluator/
model identities and operation names only — never source bodies or truth
payloads. Agent observability is not the evaluation result store.

**OTEL stays authoritative for operational telemetry.** Agent/LLM
observability is a separate concern and does not replace OTEL.

## PR 16 status — OTLP export, Collector, and local backends

PR 16 closes the v0.1 telemetry gap by composing a real exporter path
through the existing `configure_telemetry`/`TelemetryRuntime` composition —
no rival telemetry abstraction, no vendor SDK in application/domain code.

```text
Darkula instrumentation (traced/timed/counted, OTEL API)
     |
     +-- OTLP/HTTP (spans) ----------------------------------+
     |                                                       |
     +-- OTLP/HTTP (metrics) --------------------------------+--> OTEL Collector
                                                             |       |-> Jaeger (traces)
                                                             |       |-> Prometheus (metrics)
```

### Settings and selection

`TelemetrySettings` (see `docs/CONFIGURATION.md`) selects:

- `enabled=false` — no-op (no providers, no export, no threads);
- `enabled=true, export=none` — the PR 3 local in-process providers;
- `enabled=true, export=otlp` — real OTLP/HTTP span exporter behind a
  `BatchSpanProcessor` plus a periodic OTLP/HTTP metric reader/exporter.

Only the OTLP/HTTP protocol is supported in v0.1: no arbitrary header map, no
vendor exporter selection, and no secret-bearing endpoint configuration.
Composition registers the providers as the process-global OTEL providers
**only** when OTLP export is selected, so the installed decorators emit to
the composed exporter; local-only composition keeps the PR 3 behavior.

### Lifecycle and failure behavior

- `TelemetryRuntime.force_flush(timeout_millis=...)` and `shutdown(...)` are
  bounded and fail-open: exporter/backend failure never raises into domain
  behavior. `tracer_provider.shutdown()` is called before the meter provider
  shutdown, and both are idempotent/safe.
- Exporter or backend outage cannot partially commit domain state, change a
  decision, cause duplicate work, or trigger a business-operation retry.

### Local infrastructure and ports

`scripts/darkula_observability.sh` owns three pinned, `darkula.owned=true`
containers plus the `darkula-observability` network:

| Resource | Image | Host port -> container |
| --- | --- | --- |
| `darkula-otel-collector` | `otel/opentelemetry-collector-contrib:0.115.1` | `34317:4317`, `34318:4318`, `31333:13133` |
| `darkula-jaeger` | `jaegertracing/all-in-one:1.62.0` | `31686:16686` |
| `darkula-prometheus` | `prom/prometheus:v2.55.1` | `39090:9090` |

`config/observability/otel-collector.yaml` receives OTLP and routes traces to
Jaeger (OTLP/gRPC) and metrics to a Prometheus scrape endpoint; there is no
debug exporter. `config/observability/prometheus.yml` scrapes the Collector.
Ports are explicit five-digit prefix-`3` choices (standard container port with
the Darkula `3` prefix); no generalized prefix rule is invented. Ownership is
positive (`darkula.owned=true`), images are pinned (never `latest`), foreign
resources/ports fail closed, and there is no prune.

### Integration proof

`tests/integration/observability/test_otel_export.py` (OI16) runs against the
real Collector/Jaeger/Prometheus: it emits a real span/counter, flushes, then
polls Jaeger (trace present) and Prometheus (metric present) with bounded
deadlines, and asserts that known sensitive sentinels are absent from the
exported data. `./build.sh --intg` provisions the stack and removes it in a
trap-cleanup that preserves the primary exit status.

### Remaining gaps

Loki/log aggregation was deliberately not added (no second logging
architecture for a single line in the plan); traces + metrics are the v0.1
mandatory proof. Alerting/SLOs, telemetry auth/TLS, durable backends, and
sampling policy remain production gaps (see `docs/PRODUCTION_READINESS.md`).
