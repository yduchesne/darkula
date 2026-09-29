# Darkula Observability

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
Propagate appropriate identifiers across asynchronous boundaries: trace/span context, source_id, candidate_id, collection_run_id, crawl_request_id, content_id, agent_run_id, llm_invocation_id, and correlation/causation IDs as applicable. DataStream adapters own trace-context propagation in message metadata.

## Agent/LLM observability
AI-specific observability uses a Darkula-owned AgentObservability interface with adapters such as LangSmith and Langfuse. Vendor types/concepts must not leak into agents or LlmClient. LlmClient is a central instrumentation point for model/provider, latency, usage, retries, outcome, and structured-output validation.

## Sensitive data
Collected dark-web material may include credentials, PII, stolen data, and adversarial prompt content. Raw prompts/responses/content are not assumed safe for SaaS telemetry. Capture policy must support NEVER/REDACTED/FULL-like semantics with conservative defaults; secrets and credentials require mandatory redaction. Structured logs must avoid unrestricted content and authentication material.

## Crawler/DataStream telemetry
Track crawl request/success/failure, pages/bytes, duration, timeouts, status classes, redirects, rate limiting, sandbox termination, navigation/normalization failures, duplicates/content changes. Track stream produce/consume/processing duration, failures/retries, serialization failures, lag/depth where available, and dead-letter/non-retryable outcomes.

## Sandbox propagation
Only minimal approved trace context crosses into disposable sandboxes; arbitrary OTEL baggage must not become a covert channel for sensitive application state.
