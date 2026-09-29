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
- Treat all collected content as untrusted even after normalization.

## Engineering rules
- Keep domain/application layers independent of Kafka/Redpanda, S3/R2, LangChain, LangSmith/Langfuse, Prometheus, Loki, Jaeger, and crawler implementation details.
- Prefer small typed contracts and explicit DTOs. Do not leak third-party framework types through Darkula interfaces.
- Fail closed at trust boundaries and fail fast for invalid startup configuration.
- Never log credentials, secrets, unrestricted collected content, or sensitive prompt payloads.
- New source/crawler behavior should be justified by a documented real-world archetype and represented in Fake World scenarios where practical.
- Keep docs synchronized with architectural changes.
- Do not prematurely freeze details explicitly marked TBD in the architecture documents.

## Testing
Unit tests are fast and deterministic. Scenario/integration tests reuse Fake World scenarios and normally fake at infrastructure/LLM boundaries, not by replacing the behavior under test. Live-model behavior belongs in evaluations. Add infrastructure integration coverage for PostgreSQL, Redpanda, ObjectStore adapters, OTEL export, and sandbox boundaries as those components arrive.

## ATI reuse
Agentic Threat Investigator (ATI) is an implementation reference for compatible patterns including configuration layering, DataStream, LlmClient/FakeLlmClient, OTEL conventions, and eval/test fixture reuse. Darkula must remain self-contained: inspect fresh ATI main before deliberately reusing a pattern; do not create an undocumented runtime dependency on ATI.
