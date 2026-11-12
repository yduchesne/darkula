# Darkula v0.2 Roadmap — Source Discovery and Real-World Monitoring

> **Status: PR 17 implemented; PRs 18–27 planned.** PR 17 (discovery domain, durable provenance, and the qualification persistence primitive) is delivered. The remaining roadmap identifiers below describe planned work, not shipped behavior. Each implementation PR requires a separate, fresh-`main` detailed plan under [Detailed PR Plan Authoring Guide](DETAILED_PR_PLAN_AUTHORING_GUIDE.md).

## Release objective

Advance Darkula from the completed v0.1, validated against the synthetic Fake World, to a reusable **open-source, account-free, non-multitenant** framework that can discover and qualify source candidates, securely collect approved real HTTPS and Tor sources, retain provenance, and expose profile-scoped intelligence programmatically.

**Success is a bounded, evidence-backed source-discovery-to-monitoring workflow, not comprehensive darknet coverage, a commercial service, or a production-HA deployment.**

## Baseline and release boundary

The [v0.1 roadmap](ROADMAP_V01.md) records PRs 1–16 as delivered. Existing components include:

- `SourceCandidate`, `Source`, `SourceEndpoint`, candidate lifecycle, `ReconAssessment`, and `SourceAssessment` with PostgreSQL persistence;
- `ReconCoordinator` + `ReconAgent` for bounded, same-origin candidate inspection;
- `CollectionPolicy`, collection scheduling/workers, and deterministic `SourceCollectionService`;
- `CrawlerController` + `PodmanSandbox` + `CrawlerRuntime` with authorization and isolation;
- Redpanda/DataStream, transactional outbox, ObjectStore, normalization, deterministic/semantic/geographic/relationship extraction, SourceAnalyst, OTEL, evaluation, and Fake World.

**The baseline is not live darknet collection:** current sandbox/browser verification uses Fake World, has no authorized general Internet/Tor egress, and does not have autonomous source discovery, MonitoringProfiles, or source-management API. See [Production Readiness](PRODUCTION_READINESS.md). Do not represent v0.1's local integration validation as production readiness.

## Non-negotiable architectural invariants

1. **OSS framework only.** No users, accounts, organizations, tenants, billing, or Cyberinth-specific product behavior.
2. **Separate source collection from monitoring interests.** A logical Source and its approved endpoints are collected once; zero or more MonitoringProfiles can match the same observations. Neither profiles nor caller correlations create duplicate crawlers or source partitions.
3. **Stable identities.** Candidate UUID, logical Source UUID, endpoint locator, external provider identity, discovery observation, MonitoringProfile UUID, correlation ID, content hash, and object key have distinct semantics. Do not equate a URL with a Source or an LLM profile/version with a MonitoringProfile.
4. **Discovery is not crawl authorization.** Structured feeds, extracted URLs, link references, and agent assertions are untrusted observations. Only deterministic, policy-authorized coordinators can admit candidates, inspect endpoints, promote sources, or schedule collection.
5. **Preserve the existing trust boundary.** The trusted controller enforces authorization; network/browser/hostile processing runs only in disposable, default-deny sandbox workloads. Agents receive bounded observations, not browsers, arbitrary tools, network policy, credentials, or persistence rights.
6. **Provenance and conservative identity reconciliation.** Retain discovery source, time, source record/document, observed locator, extraction method, and claimed relationships. Suspected mirrors are claims, not evidence sufficient to merge Sources automatically.
7. **Reuse v0.1 infrastructure.** PostgreSQL remains authoritative. Production persistence continues through versioned stored functions and repositories/UnitOfWork, never application SQL. DataStream + outbox remain at-least-once/idempotent; no DB transaction spans network, LLM, sandbox, or ObjectStore I/O.
8. **Safe, bounded execution.** Explicit per-provider rate/cost budgets, per-source egress policies, artifact limits, retries, cancellation, retention settings, and content-free telemetry. Never accept arbitrary URLs as immediate crawler instructions.
9. **Synthetic and opt-in integration testing.** CI never needs criminal-source credentials, real onion services, or untrusted data dumps. Live external-network tests use explicit operator opt-in and permitted destinations.
10. **Explicit, operator-provisioned access.** Darkula never registers source accounts, acquires invitations, or circumvents access controls. A source's authentication readiness is separate from candidate/source lifecycle; unavailable credentials never imply analytical rejection. Credential references belong to an approved Source/endpoint, not a MonitoringProfile.
11. **Policy-routed qualification.** A preapproved, well-formed registry observation can be qualified deterministically without an LLM, provided all provider/record/identity-evidence gates pass; otherwise route to the existing ReconCoordinator or retain the candidate pending. Always persist qualification method, trusted policy/version, supporting evidence and outcome. A `ReconAssessment` is only produced by actual agent reconnaissance, never fabricated for registry decisions.
12. **No silent architecture replacement.** Preserve existing candidate lifecycle, `ReconCoordinator`, collection scheduling, ObjectStore content-addressing, Fake World, and evaluation boundaries. Amend the roadmap if necessary rather than redesigning implicitly.

## Planned capability flow

1. Structured registries (starting with RansomLook) and, later, collected-content discovery emit normalized **DiscoveryObservations** with locator, method, provider/source reference, timestamps, and evidence.
2. **SourceDiscoveryService** validates and deduplicates observations, reconciles known candidates/endpoints, and persists new `SourceCandidate` records. An unresolved source name without a concrete locator remains an unresolved reference, not an authorized crawl candidate.
3. A trusted **CandidateQualificationPolicy** routes admitted candidates: `REGISTRY_POLICY` can deterministically qualify eligible structured records with complete evidence; insufficient, ambiguous or contradictory records use `AGENT_RECON` via the existing ReconCoordinator, or remain pending. Both paths create a durable qualification decision; only actual agent reconnaissance creates a `ReconAssessment`. Neither path automatically promotes a candidate.
4. Reconnaissance preserves a separate, persistent access-readiness assessment. Sources requiring login without an authorized provisioned credential move to `CREDENTIALS_PENDING` instead of being rejected for lack of access. An operator provisions external-source accounts and credentials manually; the candidate may be qualified using available evidence or remain pending further reconnaissance.
5. Explicit promotion/approval creates a managed `Source`, `SourceEndpoint`, and `CollectionPolicy` as separately authorized steps.
6. **Existing SourceCollectionService** performs deterministic, policy-bounded collection using HTTPS/Tor transport capability added to the sandbox.
7. Ingestion/normalization/extraction/SourceAnalyst persist source-owned history; an independent **profile-matching** path evaluates evidence against MonitoringProfiles.
8. Profile-scoped programmatic operations address profiles by `monitoring_profile_id`. A dedicated correlation lookup resolves globally unique `correlation_id` to that UUID.

```text
Structured feeds ----+                               Existing source lifecycle
                     +--> DiscoveryObservation --> SourceCandidate --> QualificationPolicy
Collected references-+                                       |
                                          +------------------+----------------+
                                          |                                   |
                                   REGISTRY_POLICY                        AGENT_RECON
                                   (no LLM)                           (ReconCoordinator)
                                          +------------------+----------------+
                                                             |
                                                   QUALIFIED or pending
                                                             |
                                              explicit promotion
                                                             |
                                                             v
                                      Source + Endpoint + CollectionPolicy
                                                             |
                                                   Sandbox / HTTPS or Tor
                                                             |
                                           Content + Extraction + SourceAnalysis
                                                             |
                                      MonitoringProfile matching and result API
```

## Qualification routing — cross-PR v0.2 decision

**Principle:** registry provenance and structured metadata can be adequate to **qualify a candidate**, but must not be conflated with endpoint authenticity, reachability, authorization, or permission to collect. Do not require a model merely to repeat a well-established, policy-accepted classification.

A versioned, operator-controlled `CandidateQualificationPolicy` runs in trusted deterministic application code **after** discovery validation/reconciliation. It chooses exactly one path according to this decision matrix:

| Condition | Action | Decision provenance |
| --- | --- | --- |
| Provider on explicit trusted-adapter allowlist; feed acquired/parsed through approved adapter; locator normalized and permitted scheme; required source classification and identifiers present; record has durable provenance; no material source-identity contradiction; policy explicitly authorizes this record class | **Deterministic `REGISTRY_POLICY` qualification**; no LLM or required crawler reconnaissance | Provider identity, upstream record key/version, observed evidence references, normalized locator, trusted policy ID/version, rule IDs, evaluation time |
| Known provider but incomplete class/locator, ambiguous identity, disputed mirror claim, conflicting feeds, weak provenance, or record outside deterministic acceptance policy | **`AGENT_RECON`** via existing ReconCoordinator **when safe and authorized**, otherwise `RECONNAISSANCE_PENDING` | Decision to route/pending with reason and retained discovery observations; agent path uses its actual `ReconAssessment` |
| Invalid/unparseable/disallowed locator, unsupported scheme, or source with no resolvable endpoint | Reject invalid ingestion or retain unresolved reference as appropriate; **do not crawl or fabricate a qualified candidate** | Validation outcome/evidence; no invented endpoint |
| Known candidate/endpoint already reconciled | Reuse existing identity; update discovery/qualification evidence only as policy permits | Existing candidate/Source identity plus new evidence; no automatic duplication or promotion |

**Qualification record:** introduce an immutable, durable `QualificationDecision` (or an equivalent explicitly typed append-only record specified by PR 17) with method `REGISTRY_POLICY` or `AGENT_RECON`, candidate identity, outcome, evaluated timestamp, evidence references, applicable policy/provider ID and version, and optional actual `ReconAssessment` reference **only for** `AGENT_RECON`. A registry-path `QUALIFIED` candidate never acquires a fabricated `ReconAssessment`. If existing code assumes every `QUALIFIED` candidate has a `ReconAssessment`, extend the application/read models in PR 19 instead of emitting fake LLM outcomes. Persist the decision and the existing expected-state candidate transition/event atomically through versioned stored functions/UnitOfWork, with replay/race convergence.

**Boundaries:** deterministic qualification is **not** automatic Source promotion, endpoint approval, successful reachability validation, collection policy creation, or authenticated access. Public-source reachability can be checked separately in a permitted, sandboxed probe if required by downstream policy; it is not an LLM classification step and cannot widen crawl authority. A registry `requires_auth` claim may set evidence-backed `CREDENTIALS_PENDING` through trusted access policy and does not make credentials usable. Operators control which adapters/record classes are trusted and can revoke/disable deterministic qualification without rewriting immutable history. Agents cannot edit allowlists, policies or qualification records directly.

**Implementation ownership:** PR 17 defines the minimal qualification-decision domain and durable provenance contract; PR 18 supplies registry evidence but does not silently grant global trust; PR 19 implements policy routing, atomic qualification decisions, legacy reconnaissance integration, and fallback; PR 27 exercises both qualification paths, including no-agent assertions and negative/trust-revocation cases. No source discovery provider is implicitly authoritative solely because it is well known.

## Authenticated-source access readiness — cross-PR v0.2 decision

**Scope:** source/candidate authentication readiness is a separate orthogonal state, **not** another `CandidateStatus` or `SourceStatus`, and not part of MonitoringProfile identity. The precise persistence mapping (candidate versus managed Source, endpoint-specific overrides when different endpoints require different access) is finalized in PR 17's detailed design, without changing the v0.1 candidate lifecycle. Authentication requirements discovered during reconnaissance must be persisted, survive candidate promotion and retries, and be exposed for operator action.

| `AccessStatus` | Meaning | Collection eligibility |
| --- | --- | --- |
| `UNKNOWN` | Authentication needs have not been established | No authenticated collection authorization; public-only inspection can proceed under existing policy |
| `PUBLIC` | Approved endpoint is accessible without authentication | Eligible for unauthenticated collection when other policy gates pass |
| `CREDENTIALS_PENDING` | Authentication required; authorized credentials not yet provisioned and validated | Authenticated collection blocked; operator action required |
| `READY` | Source-scoped credentials are provisioned **and authentication was successfully validated** | Eligible for authenticated collection under explicit policy |
| `AUTH_FAILED` | Provided access no longer authenticates (expired/rejected/session invalid) | Authenticated collection suspended; bounded retries only, operator action required |
| `ACCESS_REVOKED` | Source access was deliberately disabled or authorization withdrawn | Collection forbidden until explicit operator reauthorization and validation |

- **Account creation is manual and external to Darkula.** Operators obtain access and provision secret references; Darkula does not create accounts, solve CAPTCHA/MFA automatically, procure access, or bypass restrictions. An account is an *external source credential*, not a Darkula user/account.
- **Discovery/recon:** structured registries and public inspection can indicate `requires_auth`, but a reported requirement is evidence to verify, not permission to log in. A useful candidate is not rejected merely for requiring credentials. Qualification using only public evidence is possible if sufficient; otherwise keep the existing retryable `RECONNAISSANCE_PENDING` lifecycle and independently mark `CREDENTIALS_PENDING`.
- **Access state and lifecycle are orthogonal.** Candidate eligibility, Source ACTIVE/INACTIVE, endpoint ACTIVE/INACTIVE, and CollectionPolicy authorization remain independent constraints. No new `CandidateStatus.CREDENTIALS_PENDING` or `SourceStatus.CREDENTIALS_PENDING`.
- **Credential ownership:** a Source/approved endpoint references an operator-provisioned secret; credentials do not belong to MonitoringProfiles. Different endpoint login requirements must not result in credential leakage across endpoints. `READY` requires a trusted successful authentication check; merely saving a reference leaves `CREDENTIALS_PENDING`.
- **Transitions:** `UNKNOWN` -> `PUBLIC` or `CREDENTIALS_PENDING` on verified evidence; `CREDENTIALS_PENDING` -> `READY` only after provisioning and validation; `READY` -> `AUTH_FAILED` on validated authentication failure; `AUTH_FAILED` -> `READY` after corrected access and successful validation; any active/pending state -> `ACCESS_REVOKED` by explicit operator revocation. Leaving `ACCESS_REVOKED` requires explicit operator reauthorization and revalidation. Public/protected reclassification must be backed by new observations and a trusted state transition.
- **Operator workflow:** persist state, reason, timestamps, and sanitized evidence; expose credentials-pending/failed/revoked views through operator API/CLI. Reconciliation and worker replays are idempotent, no endless authentication retries, and no automatic collection while blocked.
- **PR ownership:** PR 17 defines the access-state semantics and durable foundation; PR 19 integrates recognition, reconciliation, and pending-state transitions with candidate reconnaissance/admission; PR 26 implements the operator provisioning references, credential validation, session handling, failure/revocation handling and resumption gates. PR 27 tests the full lifecycle. Earlier PRs must never claim credentials are usable before PR 26.

## PR 17 — Discovery domain and provenance persistence (P0) — COMPLETED

**Depends on:** completed v0.1. **Dominant concern:** normalize and persist discovery without attempting to browse.

> **Delivered:** immutable `DiscoveryObservation` (bounded enums, semantic replay tuple/digest, conflict-on-payload-change), deterministic I/O-free HTTP(S)/onion locator normalization, additive migration `0009_discovery_foundation.sql` with new versioned stored functions, `DiscoveryRepository`/`AccessRepository`/`QualificationRepository` through the existing UnitOfWork, in-memory parity fakes, `SourceDiscoveryService.ingest` (no candidate creation), orthogonal `CREDENTIALS_PENDING` evidence for already-existing candidates, and the atomic/immutable qualification decision + candidate event/status primitive. Scope note: ingest produces **zero** candidate lifecycle effects; PR 19 owns admission/reconciliation and trusted qualification policy. Live registry ingestion, crawling, credential provisioning, and MonitoringProfiles remain out of scope.

- Introduce a minimal versioned, provider-neutral discovery observation contract: asserted locator, normalized locator, source kind hint, observed/discovered timestamps, origin reference, provider identity, provenance/evidence, and confidence/validation status where meaningful.
- Distinguish a source-locator observation from a source-name-only reference. Preserve unverified classifications as claims.
- Define independent `AccessStatus` semantics and persist source/candidate access requirements/readiness and their provenance, without overloading v0.1 candidate/source statuses. Plan the candidate-to-managed-Source handoff and endpoint-specific access differences; credential secret resolution is not part of PR 17.
- Add durable provenance/observation persistence through existing UnitOfWork and *additive* versioned PostgreSQL migration/functions; do not edit migrations `0001`–`0008`.
- Define `SourceDiscoveryService` interface and provider adapter boundary. Avoid a generic workflow engine or a second candidate/recon model.
- Enforce URI normalization/validation, bounded field lengths, source type validation, canonical time handling, deterministic replay/idempotency, and safe storage of provider metadata.
- Define a separate immutable `QualificationDecision` contract with `REGISTRY_POLICY` / `AGENT_RECON` method, outcome, candidate ID, evidence references, policy/provider ID and version, and optional **actual** `ReconAssessment` linkage (agent path only). Design additive stored-function persistence to support an atomic qualification decision plus candidate event/status transition; do not create a second `ReconAssessment` type.

**Acceptance:** immutable qualification decisions preserve method/evidence/policy versions without fabricated agent assessments; duplicate observations replay without duplicate canonical candidate effects; recorded authentication requirements can persist as `CREDENTIALS_PENDING` independently of candidate status; distinct provider evidence remains retained; a name-only reference cannot trigger crawling; malformed/malicious URLs and source records fail safely; PostgreSQL and in-memory tests agree on identity/provenance semantics.

**Out of scope:** external feed network access, candidate promotion policy, crawling, MonitoringProfiles.

## PR 18 — Structured registry ingestion and RansomLook adapter (P0)

**Depends on:** PR 17. **Dominant concern:** ingest an allowed external registry into the common contract.

- Add a bounded, provider-neutral polling/import path and one concrete RansomLook adapter using documented, currently permitted REST/JSON surfaces.
- Before implementation, verify the provider's live schema, authentication requirements, licensing/redistribution terms, quotas, permitted automated access, and stable identifiers; STOP if legal or technical assumptions cannot be verified.
- Validate schema and pagination, handle error/rate-limit responses, incremental polling/checkpointing, retriable failures, and source attribution. Remote data is not inherently authoritative.
- Deterministic fixture responses must exercise all operational logic in CI; real provider calls are opt-in.

**Acceptance:** reproducible import from fixture to persistent observations/candidates; repeated polling is idempotent; changed/missing fields do not destroy history; rate limits and provider outages are bounded and observable.

**Out of scope:** a general MISP/STIX/TAXII platform, crawling candidate URLs, credentialed clandestine access.

## PR 19 — Candidate reconciliation, deduplication and admission (P0)

**Depends on:** PRs 17–18. **Dominant concern:** reason deterministically about known candidate and endpoint identity.

- Define canonical locator normalization and candidate semantic keys without assuming one URL equals one Source.
- Reconcile new observations against candidate entrypoints and managed SourceEndpoints; record possible aliases/mirrors as **evidence-backed suggestions**.
- Repeated/multiple feed observations can enrich history; they must not silently promote a candidate, merge Sources, or rewrite Source identities.
- Implement a deterministic, versioned `CandidateQualificationPolicy` and routing decision matrix: accept only explicitly allowed registry adapters/record classes with complete metadata, normalized locator and durable evidence, with no conflicting identity claims; otherwise route to existing `ReconCoordinator` under authorization or remain pending. Do not invoke an LLM in the `REGISTRY_POLICY` success path.
- Preserve the existing `ReconCoordinator` for agent-assisted cases and persist `AGENT_RECON` decisions with references to genuine `ReconAssessment` records. The `REGISTRY_POLICY` path persists its own decision and the existing `QUALIFIED` candidate transition/event without inventing `ReconAssessment` rows.
- Commit qualification decision plus expected-state event/status transition atomically and idempotently via existing UnitOfWork and additive stored-function contracts. Reconcile consumers that assume all qualified candidates have agent assessments.
- Define explicit admission criteria/queueing to either qualification path, with limits and failure/retry behavior; keep final authorization with trusted application logic.
- When structured or recon evidence indicates a login gate, persist `CREDENTIALS_PENDING` while retaining candidate lifecycle status; qualify based on sufficient public evidence or remain `RECONNAISSANCE_PENDING` for further authorized inspection. Never reject solely because credentials are absent.

**Acceptance:** deterministic acceptance of a trusted, complete registry record performs zero agent/LLM calls, persists a `REGISTRY_POLICY` decision and `QUALIFIED` event without a fake `ReconAssessment`; ambiguous/conflicting/unknown records route to authorized agent reconnaissance or remain pending; provider disablement/revocation prevents new policy qualification; decision/transition replay/races converge, distinct claimed mirrors stay unmerged unless approved, known endpoints do not spawn duplicate source collection, rejected/inaccessible candidates retain auditable history, and an authentication-gated candidate is retained in `CREDENTIALS_PENDING` without an automatic `REJECT`.

## PR 20 — Controlled real-world HTTPS crawling (P0)

**Depends on:** v0.1 sandbox/crawler. **Dominant concern:** add a separate operator-approved real-network capability without relaxing Fake World isolation.

- Introduce narrowly scoped HTTPS destination authorization and network egress enforcement; handle DNS rebinding, IP-classification/private/link-local/metadata access, redirect chains, proxy configuration, and TLS/certificate failures.
- Preserve same-origin/allowed-path/budget checks in the trusted controller **and** independent sandbox network restrictions.
- Reuse `CrawlerController`, `CrawlerRuntime`, and `PodmanSandbox`; no direct HTTP/browser access from an agent or trusted app path.
- Add explicit opt-in integration with harmless, controlled public endpoints; default CI remains local Fake World.

**Acceptance:** an approved public endpoint is collected end-to-end, and cross-origin redirects, rebinding, forbidden networks, time/resource violations, and cancellation fail closed without sandbox escape or credential leakage.

## PR 21 — Tor transport and onion crawling (P0)

**Depends on:** PR 20. **Dominant concern:** Tor egress confined to a controlled proxy transport.

- Add onion locator validation and a Tor-specific sandbox transport policy with no direct clearnet fallback and no unintended DNS resolution outside Tor.
- Restrict connection to a controlled Tor endpoint, while preserving per-crawl destination, path, redirect, page, request, and time authorization.
- Bound isolation/retry behavior and apply source-specific operational/legal review before live use.

**Acceptance:** deterministic fake Tor/proxy failure tests and opt-in controlled Tor smoke tests; deny proxy bypass, clearnet fallback, unrelated internal destinations, unauthorized redirects, and unlimited retry behavior.

## PR 22 — Recursive link-based source discovery (P0)

**Depends on:** PRs 17 and 20. **Dominant concern:** turn references found while collecting into new discovery observations.

- Extend bounded crawler/normalization contracts to retain *out-of-origin references as data* without following them. Do not weaken the existing origin boundary.
- Extract normalized candidate URLs and link context deterministically; associate them with persisted content evidence and source provenance.
- Feed observations through `SourceDiscoveryService`, deduplication/admission policy and the existing reconciliation path.
- Add bounded fan-out, rate controls, source-to-source relationship claims, and loop suppression.

**Acceptance:** an outgoing link can create an auditable candidate without additional network navigation; arbitrary URLs embedded in hostile content cannot expand the crawler allowlist; mirror claims do not merge identities; retries converge.

## PR 23 — Semantic source-reference recognition (P1)

**Depends on:** PR 22. **Dominant concern:** recognize references not expressible as direct links.

- Reuse existing model-backed extraction and evidence-grounding conventions to extract source-related claims (renamed forum, affiliated channel, replacement endpoint, unlinked source name).
- Require exact supported spans and durable provenance; unresolved names remain unresolvable references rather than fictitious URLs.
- Keep semantic predictions separate from deterministic validation, source reconciliation, promotion, and crawl authorization.
- Evaluate precision, unsupported claims, adversarial prompt injection, language variation, and repeated messages using Fake World.

**Acceptance:** no fabricated endpoints or invented evidence; ambiguous references stay pending; no model can expand crawler authority.

## PR 24 — MonitoringProfile domain and persistence (P0)

**Depends on:** v0.1 domain/persistence; parallel with PRs 17–23.

- Add `MonitoringProfile` with generated `monitoring_profile_id` UUID, `name`, globally unique arbitrary `correlation_id` (generate a UUID string if absent), and optional bounded entity-match definitions.
- A caller-chosen correlation ID is unique **across this Darkula instance**, not tenant-scoped; collisions reject deterministically/race-safely.
- Expose authoritative lookup from correlation ID to monitoring profile UUID; never treat the correlation ID as the identifier for normal profile operations.
- Avoid any user/account/tenant concepts. Source and monitoring identities remain independent.
- Do not confuse existing `SourceAssessment.profile_name/profile_version` (analysis profile/version) with MonitoringProfiles.

**Acceptance:** UUID/correlation identity and uniqueness are proven across retries/concurrent creation; profile entity definitions validate; no regression to existing Source or source-analysis models.

## PR 25 — Profile matching and programmatic interfaces (P0)

**Depends on:** PR 24 and existing v0.1 extraction. **Dominant concern:** produce and retrieve evidence-backed results per profile without duplicating source collection.

- Implement bounded, explainable entity matching over persisted normalized/extracted observations and relevant source analysis; retain source/content/evidence provenance and match policy version.
- Offer minimal documented programmatic endpoints for creating/updating/reading profiles, querying profile-scoped matches, and resolving `correlation_id` to `monitoring_profile_id`. Profile-scoped API operations use the Darkula UUID, with the correlation-resolution endpoint as an explicit exception.
- Keep framework-wide source management in trusted application services/CLI as appropriate; do not introduce account-based API access control.
- **Storage boundary:** retain current content-addressed shared raw/normalized source artifacts (`sha256/<prefix>/<hash>`), which are source-owned and profile-agnostic. Profile-specific derived/export artifacts may use paths prefixed by `monitoring_profile_id`; maintain durable linkage back to shared source artifacts. This clarifies the earlier profile-ID ObjectStore-path proposal without rewriting the v0.1 content-addressing contract.
- Document deployment-perimeter protection for any externally exposed API; no assertion that account-free means safely Internet-public.

**Acceptance:** one source crawl provides evidence to two independent profiles with different matches and no duplicate crawling; all profile-scoped requests use UUIDs; correlation lookup is unique; artifact paths/linkage and replay/idempotency are tested.

## PR 26 — Authorized authenticated source collection (P1)

**Depends on:** PRs 20–21. **Dominant concern:** securely pass a source-specific, authorized session/credential into a single sandbox execution.

- Implement operator-managed credential provisioning references and trusted secret resolution, using the independently persisted `AccessStatus` defined in PR 17/19. Secrets are scoped to approved Source/endpoint and one sandbox execution; no credential values in database policy, event streams, telemetry, or model context.
- Support bounded login, session expiration and reauthentication through approved adapters, with **manual account creation and operator-managed CAPTCHA/MFA/approval**. Provisioning a reference alone does not make `READY`: require trusted successful validation before enabling authenticated collection. No autonomous account registration, purchases, or access-control bypass.
- Transition `CREDENTIALS_PENDING -> READY` only after successful validation; `READY -> AUTH_FAILED` on authentication rejection/expiry; resume `AUTH_FAILED -> READY` only on corrected access plus revalidation. Explicit `ACCESS_REVOKED` blocks further access until operator reauthorization. Failed/missing credentials suppress scheduled authenticated work without unbounded retries. Expose state and sanitized failure reason via operator API/CLI; include redaction and credential isolation tests.

**Acceptance:** controlled synthetic authenticated source succeeds after manual credential provisioning and validation; credentials-pending and failed sources are not collected, `AUTH_FAILED` cannot loop indefinitely, `ACCESS_REVOKED` cannot self-reactivate, re-provisioning and validation can resume collection, and no secret crosses Source/endpoint boundaries or enters artifacts/telemetry.

## PR 27 — v0.2 end-to-end hardening and release qualification (P0)

**Depends on:** all v0.2 work selected for release; PR 23/26 are planned P1 inclusions and may be explicitly deferred only by a documented roadmap amendment. **Dominant concern:** prove the integrated release behavior.

- Full-stack fixture-based workflows: trusted complete structured feed -> persistent discovery/provenance -> `REGISTRY_POLICY` qualification **without** ReconAgent -> explicit promotion -> HTTPS/Tor-controlled collection -> extraction/SourceAnalyst -> two MonitoringProfiles -> programmatic results; separately, ambiguous/untrusted feed -> authorized ReconCoordinator and actual ReconAssessment, or pending if not inspectable.
- Verify outbox/replay/concurrency, source/endpoint identity, cross-source dedup, cancellation, transport failure recovery, profile uniqueness, authentication-required pending state, credential provisioning/validation (if PR 26 is included), revocation, and security invariants.
- Define measured discovery precision, useful candidate yield, new-source rate, latency, duplicate/mirror rate, and collection cost; do not invent a universal benchmark threshold before a baseline exists.
- Update README, configuration/security/deployment/testing/evaluation docs and [Production Readiness](PRODUCTION_READINESS.md) with verified v0.2 behavior and remaining gaps. Run `./build.sh --qa`, `./build.sh --sec`, and `./build.sh --intg` on the capable host; external smoke tests remain separate, operator-approved and opt-in.

**Acceptance:** an explicitly documented and reproducible v0.2 demonstration meets the release scenario; any failures, missing features, and production-readiness gaps are disclosed instead of marked complete.

## Dependency and execution sequence

| Stage | PRs | Outcome |
| --- | --- | --- |
| Discovery foundation | 17 -> 18 -> 19 | Durable structured discovery through candidate admission, tested offline |
| Real-world collection | 20 -> 21 | Bounded HTTPS then Tor, no change to default Fake World security |
| Recursive/semantic discovery | 22 -> 23 | External references as evidence, then grounded implicit references |
| Monitoring profiles | 24 -> 25 | UUID-scoped matching and API (can proceed in parallel with 17–23) |
| Restricted access | 26 | Source-specific authorized credentials (after live transport) |
| Qualification | 27 | Full-stack security, replay and operator-readiness gate |

**Notes:** PR 22 requires actual out-of-origin link *observation*, not unauthorized crawler traversal. PR 25 does not wait for PRs 20–23 if developed against v0.1 Fake World and fixtures. Detailed PR authors must inspect fresh `main` and adjust PR boundaries if underlying code has evolved.

## v0.2 acceptance scenario

1. From an allow-listed, machine-readable source registry, Darkula imports a new source locator with provider provenance and stable import identity.
2. Duplicate feed polls converge to the same candidate while preserving independent evidence observations.
3. An explicit qualification policy classifies a fully supported trusted registry record using `REGISTRY_POLICY` without agent/LLM access; an ambiguous or insufficiently supported record uses authorized ReconCoordinator/`AGENT_RECON` or remains pending. Both outcomes have durable method/evidence provenance; only agent inspection produces `ReconAssessment`. Explicit Source promotion remains separate.
4. A managed Source is collected through approved HTTPS or Tor transport with sandbox egress independently enforced.
5. Normalization, source-owned ObjectStore artifacts, extraction, relationship/geography resolution when available, and SourceAnalyst produce persistent evidence and assessment history.
6. An authenticated candidate discovered without credentials remains tracked as `CREDENTIALS_PENDING` and is never analytically rejected for that reason. When PR 26 is included, a controlled fixture proves operator-provisioned credentials, `READY` validation, failure/revocation suspension and authorized resume.
7. Two MonitoringProfiles independently match relevant observations, and results are retrieved by profile UUID while raw source content is collected once.
8. Replays, retries, source outages, rejected mirror assertions, policy changes, and aborted containers cannot create duplicate authoritative effects or expand permitted access.
9. The system produces useful, versioned telemetry and a documented report of evidence, safety tests, metrics and remaining deployment limitations.

## Explicit non-goals

- Multitenancy, users, user management, SSO, billing, reseller functionality, commercial/private collectors or Cyberinth product workflows.
- Rewriting ATI or developing a full threat investigation/SIEM platform.
- A comprehensive user-facing UI, broad messaging-platform harvesting, or a universal directory of all darknet services.
- Automatic acquisition of credentials, evasion of access controls, or unbounded downloading of data dumps.
- HA/distributed orchestration, exactly-once delivery guarantees, a global canonical entity graph, or a complete MISP/STIX/TAXII server.
- Silent changes to prior frozen migrations or v0.1 security invariants.

## Planning and change-control rules

- This is a **roadmap**, not a coding-agent implementation contract. Before starting PR 17 or any other PR, create a detailed plan compliant with [Detailed PR Plan Authoring Guide](DETAILED_PR_PLAN_AUTHORING_GUIDE.md), grounded in fresh main (domain, repositories/stored functions, adapter/SPI, config/composition, tests and security).
- One dominant architectural concern per PR; stop instead of improvising if upstream schema, provider permissions, sandbox restrictions or existing invariants make the plan invalid.
- Each PR must include an explicit test matrix, security/failure coverage, documentation changes and acceptance criteria; do not declare a workstream delivered until validated on the agreed environment.
- Treat roadmap P0/P1 as implementation priority, **not current completion state**. Any material reordering, P1 deferral, or change to the shared-artifact/profile-path decision requires explicit documentation review.
