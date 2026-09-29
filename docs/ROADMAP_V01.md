# Darkula v0.1 Roadmap

This roadmap sequences the architecture established by PR 1. PR boundaries after PR 1 are planning targets and may be refined by detailed plans and fresh-main review.

## PR 1 — Architecture and engineering foundation
Create AGENTS.md and the v0.1 architecture/domain/testing/evaluation/observability/configuration/Fake World/security documentation. No production code or detailed persistence schema.

### Status — delivered

## PR 2 — Project/tooling and foundational contracts
Establish Python project/tooling/CI and core typed contracts/value objects. Introduce initial SPI definitions for LlmClient, DataStream, ObjectStore, persistence boundary, and agent observability without premature provider coupling. Establish OTEL helper/decorator conventions and configuration skeleton.

### Status — delivered

PR 2 was implemented from `.plans/DARKULA_PR_2_DETAILED_PLAN.md` (commit baseline: PR 1 documentation set). Delivered: Python 3.14/uv/hatchling project, Ruff/strict mypy/pytest/coverage/pre-commit/PR CI, `src/darkula` package with `domain/identifiers.py`, the `app/` contracts (`llm.py`, `data_stream.py`, `object_store.py`, `persistence.py`, `agent_observability.py`), `config/settings.py` skeleton, `telemetry/decorators.py` contract, the contract test matrix, and `docs/DETAILED_PR_PLAN_AUTHORING_GUIDE.md`. No infrastructure adapter (Redpanda, PostgreSQL, S3/R2, LangChain/LangSmith/Langfuse, crawler) was introduced.

## PR 3 — Configuration, observability, and test fakes
Implement Pydantic Settings layering/profile/empty-env semantics, composition foundation, FakeLlmClient, FakeDataStream, InMemory/LocalFile ObjectStore baseline, and OTEL testable instrumentation primitives.

## PR 4 — Source domain and persistence foundation
Implement SourceCandidate, event history, Source, SourceEndpoint, reconnaissance/assessment domain records and initial PostgreSQL persistence SPI/adapter. Detailed schema and migration plan must be designed from fresh main.

## PR 5 — DataStream/Redpanda and reliable messaging
Implement producer/consumer DataStream semantics, Redpanda adapter, message envelope/versioning/correlation, idempotency foundations, transactional outbox, retries/failure semantics, and OTEL propagation/metrics. Use ATI as an implementation reference after inspecting its fresh main.

## PR 6 — Fake World foundation
Implement scenario/truth model and first realistic forum source with deterministic fixtures/rendering sufficient for crawler and recon tests.

## PR 7 — Crawler contract and sandbox foundation
Implement CrawlRequest/CrawlResult, trusted worker/controller, sandbox contract, resource/egress constraints, and Fake World crawler integration. No unrestricted production dark-web crawling before security acceptance criteria are met.

## PR 8 — Normalization and artifact storage
Implement ContentArtifact/NormalizedContent, safe normalization boundary, ObjectStore artifact flow, hashes/deduplication with independent provenance, and S3-compatible adapter work as appropriate.

## PR 9 — Collection
Implement CollectionPolicy, CollectionRun, SourceCollectionService, asynchronous crawl work, durable state transitions, and collection telemetry.

## PR 10 — Recon workflow
Implement Coordinator reconnaissance flow and ReconAgent using LlmClient structured outputs, evidence references, FakeLlmClient scenarios, and agent observability.

## PR 11 — Deterministic content extraction
Implement ExtractionResult and deterministic observable/entity extractors with provenance/versioning.

## PR 12 — Semantic and geographic extraction
Implement model-backed semantic extraction, geographic mention/resolution with separate confidence/provenance, and realistic Fake World ambiguity scenarios.

## PR 13 — Extracted relationships
Implement content-derived relationships and provenance without promoting assertions to global truth.

## PR 14 — Source analysis
Implement SourceAnalyst and immutable/time-windowed SourceAssessment history, triggered by explicit analysis policy rather than necessarily per document.

## PR 15 — Expanded Fake World and evaluations
Add marketplace/leak/mirror archetypes, cross-source scenarios, Recon/extraction/SourceAnalyst evals, provider-neutral agent-observability integration, and experiment/result correlation.

## PR 16 — v0.1 end-to-end hardening
Exercise PostgreSQL + Redpanda + ObjectStore + sandbox + extraction + agents + OTEL stack end to end; test replay/idempotency/failures/security controls; document production-readiness gaps.

## Deferred beyond v0.1 unless required
Multi-tenancy; commercial/private collectors; ATI integration; full threat-investigation synthesis; detailed product UI; production-scale deployment architecture.

## Planning rule
Every implementation PR requires a detailed plan based on fresh main. Do not treat this roadmap as a substitute for inspecting current code and prior merged changes.
