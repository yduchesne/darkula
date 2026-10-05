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
`telemetry/attributes.py`, `telemetry/tracing.py`, `telemetry/metrics.py`, and `telemetry/setup.py`, and pinned `opentelemetry-sdk` (API + SDK only; **no** OTLP exporters, Collectors, or vendor backends).

Delivered decorator behavior:

- `traced` — exactly one span per invocation (sync/async), marked `ERROR` on an ordinary exception (which propagates unchanged); cancellation propagates and is never an ordinary error; only validated static attributes are attached; arguments/results are **never** captured.
- `timed` — one `time.perf_counter()` seconds histogram point tagged `darkula.outcome = success|error`; cancellation records no point; the metric name must use the canonical `<operation>.duration` suffix and the seconds unit; the same exception propagates after its error point is recorded.
- `counted` — increments exactly once **at operation entry**, so success, failure, and cancellation each count as one invocation.
- Static attributes are bounded (key <= 200 chars; value <= 256 chars), control-free, and reject secret-like names/values at decoration time, so prompts/tokens/credentials/unbounded content can never reach telemetry through the decorators.
- Disabled/unconfigured OTEL falls back to the API no-op proxy: all decorators remain behavior-preserving.

Tracer/meter resolution goes through the injectable module seams `get_tracer` / `get_histogram` / `get_counter`; deterministic tests bind in-memory SDK providers through those seams (`tests/unit/conftest.py`, `tests/support/otel.py`) and never touch global OTel state or any external service. `configure_telemetry` composes local `TracerProvider`/`MeterProvider` for the configured `service.name` without exporters, network, or background threads.

Explicitly deferred: OTLP exporters/Collector deployment, Prometheus/Loki/Jaeger/Grafana wiring, and vendor agent observability (LangSmith/Langfuse) remain future PRs.

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
