# Darkula v0.1 Production-Readiness Register

This document distinguishes what PR 16 **verified on a local, single-node
system** from acceptable v0.1 limitations and the work that must happen before
any production deployment. It is a gap register, not a production-readiness
claim.

Status vocabulary:

- `VERIFIED_LOCAL` — exercised end-to-end on the local Podman stack.
- `LIMITED` — delivered but with a known local/single-node limitation.
- `NOT_DELIVERED` — deliberately out of v0.1 scope.
- `REQUIRES_OPERATOR_DESIGN` — the operator must choose/engineer it.
- `DEFERRED` — planned after v0.1.

## Verified v0.1 behavior (local, single node)

| Area | v0.1 evidence | Current gap | Required before production | Status |
| --- | --- | --- | --- | --- |
| Configuration/composition | Typed Pydantic settings, layered profiles, central composition root; `./build.sh --qa`/`--sec` | Single local profile set | Environment-specific profiles + secret injection reviewed | VERIFIED_LOCAL |
| PostgreSQL persistence | Versioned stored functions; short UoW; SQL-free application/domain; migrations `0001`–`0008` hash-guarded | Single node; no replication | HA/backup/PITR, connection pooling for scale, migration rollout policy | LIMITED |
| DataStream/outbox/consumer | Real Redpanda; at-least-once; durable `processed_message` idempotency; transactional outbox | No exactly-once claim; single broker | Broker durability/replication, topic retention, consumer-group ops | LIMITED |
| Crawler/sandbox | Real PodmanSandbox + Playwright/Chromium against Fake World; default-deny network/fs/runtime | Fake World is the only destination; no real Tor/clear-net egress | Egress policy, Tor/real-source handling, image supply chain | VERIFIED_LOCAL |
| Normalization/ObjectStore | Deterministic normalization; local filesystem ObjectStore; content-addressed dedup with provenance | Local filesystem only | Durable object storage (S3/R2), lifecycle/retention, encryption | LIMITED |
| Extraction/analysis | Deterministic + semantic + geographic + relationship extraction; SourceAnalysis/SourceAnalyst; all durable/immutable/versioned | Model resolver boundaries are fakes in CI | Live provider controls, cost/rate limits, eval gates | VERIFIED_LOCAL |
| Evaluation | Evaluator-side `darkula.evaluation`; deterministic metrics; opt-in runner | Live-model eval is manual | Recorded baselines/gates for live models | LIMITED |
| OTEL export | Darkula -> OTLP/HTTP -> Collector -> Jaeger traces + Prometheus metrics; sentinel-absence checks | Local collector; no auth/TLS; short retention | Managed Collector/backends, auth/TLS, retention, alerting | VERIFIED_LOCAL |
| LangSmith observability | Content-safe fail-open adapter; fail-fast config | External SaaS/privacy review pending | Data-processing agreement, region/retention review | LIMITED |
| Integration lifecycle | Trap-based cleanup; ownership-verified start/stop/clean; explicit ports | Single-node podman | Orchestrated deployment/supervision | VERIFIED_LOCAL |

## Operational concerns requiring operator design

| Area | v0.1 state | Required before production | Status |
| --- | --- | --- | --- |
| Secrets management | Local `.env`/env overrides; LangSmith/LLM keys redacted | Vault/secret manager, rotation, least privilege | REQUIRES_OPERATOR_DESIGN |
| Database backups/HA | Single PostgreSQL container | Backups, PITR, failover, tested restore | REQUIRES_OPERATOR_DESIGN |
| Redpanda durability | Single broker, default topics | Replication factor, retention, disaster recovery | REQUIRES_OPERATOR_DESIGN |
| ObjectStore retention | Local root, no lifecycle policy | Durable store, retention/legal-hold, encryption | REQUIRES_OPERATOR_DESIGN |
| Telemetry auth/TLS/durability | Local offline Collector | TLS, auth, durable backends, sampling policy | REQUIRES_OPERATOR_DESIGN |
| Alerting/SLOs | None | SLOs, alerts, dashboards, on-call | NOT_DELIVERED |
| Deployment supervision | `build.sh` + Podman scripts | Orchestrator, health probes, zero-downtime rollout | REQUIRES_OPERATOR_DESIGN |
| Scaling/backpressure | Single worker/node | Horizontal workers, broker partitions, bounded concurrency | REQUIRES_OPERATOR_DESIGN |
| Disaster recovery | None | RPO/RTO, restore drills, region strategy | REQUIRES_OPERATOR_DESIGN |
| Upgrades | Manual migrations | Versioned rollout/rollback policy | REQUIRES_OPERATOR_DESIGN |
| Image/vulnerability policy | Pinned images + `pip-audit`/`bandit` | Image signing/scanning, base-image patching cadence | REQUIRES_OPERATOR_DESIGN |

## v0.1 limitations (accepted)

| Area | Limitation | Status |
| --- | --- | --- |
| Tor / real dark-web collection | Not delivered; Fake World only | NOT_DELIVERED |
| Authentication / multi-tenancy | Single-tenant local runtime | DEFERRED |
| Hostile file/archive isolation | Only safe synthetic attachments; no untrusted archive/exploit sandbox | DEFERRED |
| Live geographic resolver | No live geocoder in v0.1; fake resolver only | DEFERRED |
| Legal retention/deletion | No retention/deletion workflow | REQUIRES_OPERATOR_DESIGN |
| Reports/UI/API product surface | None | DEFERRED |
| Loki/log-aggregation | Current observability is traces + metrics; no second logging architecture was invented | DEFERRED |
| Global entity resolution / graph / threat synthesis | None | DEFERRED |
| Exactly-once semantics | At-least-once only | NOT_DELIVERED |

## Local/single-node assumptions

- All services (PostgreSQL, Redpanda, Collector, Jaeger, Prometheus, crawler
  sandbox network, Fake World) run on one Podman host.
- Host ports use the Darkula prefix-3 convention (standard container ports;
  `${prefix}${container_port}` host ports, documented per service).
- The integration observability stack is **offline** and not exposed beyond
  the host. It must never be treated as a production telemetry deployment.
- CI provisions and removes only Darkula-owned resources; rollout is
  trap-cleanup based and preserves the primary exit status.

## Telemetry trust and data restrictions

Telemetry is a bounded, content-free operational signal, not a data channel.
It never carries source content, prompts/responses, credentials, cookies or
sessions, endpoint URIs, object keys, content hashes, high-cardinality IDs as
labels, or Fake World hidden truth. Exporter/backend failure is fail-open and
never alters domain behavior, retried as a business operation, or used to
justify a duplicate write. See `docs/OBSERVABILITY.md` and `docs/SECURITY.md`.
