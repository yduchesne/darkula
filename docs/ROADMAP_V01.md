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

### Status — delivered

PR 3 was implemented from `.plans/DARKULA_PR_3_DETAILED_PLAN.md` (commit baseline: `09d856ab4e30ed02b543791b8c788cbeaecfc208`). Delivered: full layered TOML settings resolution in `config/` + `darkula/config/loader.py` with env-last precedence and redacted diagnostics; `darkula/testing/` FakeLlmClient + FakeDataStream; `darkula/infrastructure/object_store/` InMemoryObjectStore + LocalFileObjectStore (root-confined, streaming); OTEL SDK-backed `traced`/`timed`/`counted` with `attributes`/`tracing`/`metrics`/`setup` support modules and in-memory telemetry tests; and the narrow `darkula/composition.py` that fails fast on unavailable production drivers. No real infrastructure, no `--intg`, no OTLP exporters/Collectors.

## PR 4 — Source domain and persistence foundation
Implement SourceCandidate, event history, Source, SourceEndpoint, reconnaissance/assessment domain records and initial PostgreSQL persistence SPI/adapter. Detailed schema and migration plan must be designed from fresh main.

### Status — delivered

PR 4 was implemented from `~/Downloads/DARKULA_PR_4_DETAILED_PLAN.md` (commit baseline: `028f012aa0d799e8de930c147fd4489f939075a6`). Delivered: executable source-domain model (`domain/source.py`) with frozen enums/identity/time/JSON validation; aggregate-oriented repository contracts exposed through the existing `UnitOfWork`; versioned PostgreSQL schema + `*_v1` stored functions in `migrations/0001_initial.sql` (production SQL lives only in DB artifacts; repository Python carries only stored-function invocations); async PostgreSQL `PostgresDarkulaSpi`/`PostgresUnitOfWork`/repositories/mapping/errors with bounded, no-leak errors and cancellation-safe transactions; `PersistenceError` subtypes; database settings/composition; Darkula-owned Podman PostgreSQL pinned at `docker.io/library/postgres:18.2` published exactly `35432:5432`; `./build.sh --intg` + integration CI job; migrations tooling; unit + real-PostgreSQL test matrices (domain D*, UoW U*, mapping/errors M*, migrations P*, candidate C*, source S*, transactions T*, vertical slice). No Redpanda/outbox/crawler/collection/agent/extraction, no ORM.

## PR 5 — DataStream/Redpanda and reliable messaging
Implement producer/consumer DataStream semantics, Redpanda adapter, message envelope/versioning/correlation, idempotency foundations, transactional outbox, retries/failure semantics, and OTEL propagation/metrics. Use ATI as an implementation reference after inspecting its fresh main.

### Status — delivered

PR 5 was implemented from `~/Downloads/DARKULA_PR_5_DETAILED_PLAN.md` (commit baseline: `f058e3a...`). Delivered: `RedpandaDataStream` implementing the existing `DataStream` (aiokafka behind the boundary); deterministic versioned wire codec (envelope v1, canonical UTF-8 JSON, strict bounded rejection); publish routing keys + W3C trace headers, per-lane high-water acknowledgements, poll-never-acks, malformed-record bounded failure; transactional outbox + durable processed-message tables and stored functions in `migrations/0002_message_outbox.sql` (outbox append/claim/mark-published, atomic check+record idempotency, explicit lease/attempt retry state); `OutboxRepository`/`ProcessedMessageRepository` through the existing UnitOfWork; bounded `OutboxPublisher` (claim -> commit -> publish outside the transaction -> mark -> commit, partial acceptance fails closed); `ReliableConsumer` primitive (effect + marker + outgoing outbox atomic; ack only after commit; cancellation safe); typed Redpanda settings/composition (production driver, never silent fake); OTEL spans/counters/durations for datastream/outbox/consumer with bounded labels and header-based trace propagation; Darkula-owned Podman Redpanda at `docker.io/redpandadata/redpanda:v24.3.8` published exactly `39092:9092` with the same fail-closed ownership lifecycle as PostgreSQL; `./build.sh --intg` provisions PostgreSQL + Redpanda; real vertical slices V1–V5 including the PR 5B real-Podman PostgreSQL container/volume ownership regression; unit matrices DS1–DS16, O1–O12, C1–C10, I1–I10, and telemetry emission. No crawler/Fake World/collection/recon/extraction/agent work was introduced.

## PR 6 — Fake World foundation
Implement scenario/truth model and first realistic forum source with deterministic fixtures/rendering sufficient for crawler and recon tests.

### Status — delivered

PR 6 was implemented from `~/Downloads/DARKULA_PR_6_DETAILED_PLAN.md` (commit baseline: fresh main including GitHub PR #12). Delivered: `darkula/testing/fake_world/` (identifiers, model, truth, rendering, traceability, registry, scenarios/blackgate_v1) with canonical `blackgate-core` v1; immutable scenario/identity/version models with cross-reference validation at construction (FW1–FW10); independent `FakeWorldTruth` (actors/aliases/organizations/locations/relationships/events, hidden-alias and private-sale truth-only facts) (G1–G5); realistic BlackGate forum archetype (4 boards, aliases/reputation, paginated thread, registration/login gating with deterministic request-count session expiry, quotes/edits/deletions/reposts, multilingual content, one malformed legacy page, hostile prompt-injection-like text as plain content, safe synthetic attachments plus an inert unsafe-looking filename, redirects, 429 rate limiting, intermittent 503-then-200); deterministic transport-light `FakeWorldRenderer` (no network/filesystem/wall clock, fresh runtime state per test, source-level failures as responses) (R1–R24); Washington State hospital seed + separate Washington, D.C. reference for later geographic ambiguity; `FW-BG-*` behavior traceability manifest validated at construction (T1–T6); all-navigation-vertical-slice (`VSLICE`); AST/fresh-interpreter guards enforcing that production packages never import Fake World truth; docs updated (FAKE_WORLD/TESTING/EVALUATIONS/AGENTS). No crawler/sandbox, collection, recon agent, extraction/geocoding, LLM, evaluation framework, PostgreSQL/Redpanda dependency, or live criminal-resource access was introduced.

## PR 7 — Crawler contract and sandbox foundation

Delivered (PR 7, branch `dev/secure-crawler`): the application-facing
Crawler contract (`CrawlRequest -> CrawlResult`, origin-based authorization,
budgeted traversal, bounded observations), trusted `CrawlerController`
(least-capability policy derivation with an exact one-destination network
allowlist, budget clamping against settings maxima, exit-reason mapping,
invalid-output rejection, cancellation propagation, OTEL counters/timers),
a versioned bounded controller-runtime JSON protocol (v1), a first-class
`Sandbox` SPI with `PodmanSandbox` (fresh disposable one-container-per-
execution, ownership-label fail-closed lifecycle, no shell, bounded stdin/
stdout, timeout/cancellation cleanup, hardened rootless flags, pids/memory/
CPU ceilings, read-only rootfs + bounded tmpfs), a minimal sandboxed
`CrawlerRuntime` (deterministic BFS over sorted same-origin links, budgets,
one bounded 503-retry, 429/logout/fragment handling, single login with
post-login re-observation of bounced targets, bounded download samples for
attachments), and real-browser Fake World integration (internal network
`darkula-intg` with no egress and a single authorized destination
enforced below workload logic; Fake World HTTP service with session/rate/
failure semantics matching the PR 6 scenario renderer). Delivered tests:
unit contract/protocol/controller/engine/sandbox/podman/http-adapter
matrices plus the canonical real-Podman integration slice (real
controller → real sandbox → real container → real runtime → real
Playwright/Chromium → real HTTP → BlackGate), lifecycle/ownership/
resource-bound negatives, and network-isolation negatives (PostgreSQL,
Redpanda, unauthorized sibling, gateway/host, loopback, cloud metadata all
unreachable; positive control included). No unrestricted production
illicit-source crawling is enabled; ReconAgent/DeepAgent remains outside the
sandbox and is PR 10 scope.

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
