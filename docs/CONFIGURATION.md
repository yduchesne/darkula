# Darkula Configuration

## PR 4 status — layered resolution + PostgreSQL fields

PR 3 implemented the full layered Pydantic Settings resolution in
`darkula/config/settings.py` (typed field definitions) and
`darkula/config/loader.py` (file loading, deep merge, env-last semantics,
safe diagnostics). PR 4 extended the typed field set with the PostgreSQL
database group.

### Database group (PR 4)

`[database]` (Pydantic `DatabaseSettings`) — the only delivered driver is
`postgresql`:

| Field | Default | Notes |
| --- | --- | --- |
| `driver` | `postgresql` | Only delivered driver; no fake fallback |
| `host` | `localhost` (dev) / `darkula-postgres` (base container name) | |
| `port` | `35432` (dev: host-published Darkula port; container port stays `5432`) | |
| `name` | `darkula` | |
| `user` | `darkula` | |
| `password` | `None` (optional) | Redacted in diagnostics; inject via `DARKULA_DATABASE__PASSWORD` |
| `connect_timeout_seconds` | `10.0` | Must be positive |
| `pool_min_size` | `1` | Must be `>= 1` |
| `pool_max_size` | `4` | Must be `>= pool_min_size` |

Invalid pool bounds and non-positive timeouts fail fast at validation.
Environment overrides follow the same rules as every other field
(`DARKULA_DATABASE__HOST`, `DARKULA_DATABASE__PASSWORD`, ...).

The `development` profile (default) targets the Darkula-owned Podman
PostgreSQL through the host-published port `35432`; inside the Darkula
Podman network the service keeps its standard port `5432` (base layer
`host = "darkula-postgres"`, `port = 5432`).

### DataStream group (PR 5)

`[datastream]` (Pydantic `DataStreamSettings`) — `fake` is the
deterministic offline driver; `redpanda` is the delivered production
adapter (`RedpandaDataStream`, PR 5):

| Field | Default | Notes |
| --- | --- | --- |
| `driver` | `redpanda` (code) / `fake` (base layer) | Production selection never silently substitutes the fake |
| `bootstrap_servers` | `darkula-redpanda:9092` (base) / `127.0.0.1:39092` (development: host-published Darkula port; container Kafka port stays `9092`) | Comma-separated `host:port` list |
| `client_id` | `darkula` | Non-blank |
| `poll_timeout_ms` | `500` | Bounded; must be `>= 1`; never a no-timeout poll |
| `max_poll_records` | `100` | Bounded; must be `>= 1` |

Only required typed Redpanda settings exist: no arbitrary Kafka
configuration dictionary and no committed secrets. Environment overrides
follow the standard rules (`DARKULA_DATASTREAM__DRIVER`,
`DARKULA_DATASTREAM__BOOTSTRAP_SERVERS`, ...), with unset/empty no-ops.
The `production` profile selects `redpanda`; the `development` profile
overrides `bootstrap_servers` to the host-published `127.0.0.1:39092`, and
the base layer names the container (`darkula-redpanda:9092`) for
container-to-container consumers.

### File layout (repo-local, stdlib TOML)

```text
config/base.toml                    # required baseline (lowest file layer)
config/profiles/<profile>.toml      # required selected profile
config/local.toml                   # optional operator override (git-ignored)
```

- Profile selection: non-empty `DARKULA_CONFIG_PROFILE`; the deterministic
  default is `development`. The name must match `^[a-z][a-z0-9_-]*$`.
  The profile is never inferred from machine/user/CI.
- Parsing uses stdlib `tomllib` only; no YAML and no dotenv.
- Shipped profiles: `development` (default; local object-store root under
  `var/darkula/objects`), `test` (in-memory object store), and
  `production` (selects the PR 5 Redpanda driver and the PR 8 S3 object
  store; the operator must provide `DARKULA_OBJECT_STORE__BUCKET` because
  a remote bucket is never committed).

### Precedence (exact)

```text
defaults < base < selected profile < local override < non-empty environment
```

- **Environment is the ultimate override.** It is applied last by
  reordering the Pydantic Settings sources (see
  `_EnvFirstSettings.settings_customise_sources`), so a file value can
  never outrank a non-empty `DARKULA_*` variable.
- **Unset and empty environment variables have no effect**
  (`env_ignore_empty = True`): they never erase a lower-layer value.
- Prefix is `DARKULA_`; nested environment variables use the `__`
  delimiter, e.g. `DARKULA_DATASTREAM__DRIVER`.
- Nested mappings deep-merge recursively; scalars/lists/tuples replace
  wholesale (no invented list merging).

### Failure rules (all bounded, fail closed)

| Condition | Result |
| --- | --- |
| required `base.toml` missing | `ConfigError` |
| selected profile missing | `ConfigError` |
| malformed TOML | `ConfigError` (file path only, never contents) |
| malformed profile name | `ValueError` |
| unknown key / invalid type (file or env) | Pydantic `ValidationError` |
| optional `local.toml` missing | accepted |

### Diagnostics and secrets

`resolve_layers()` returns the raw layers; `diagnostic_summary()` renders a
deterministic, single-line summary with the selected profile, loaded layer
names, and the **redacted** effective configuration. Key/value markers
(`secret`, `password`, `token`, `api_key`, `private_key`, `credential`,
`auth`, `cookie`, `session`, ...) are replaced with `<redacted>`.
Diagnostics never dump the environment wholesale. There is no secret
resolver in PR 3.

### ObjectStore group (PR 3/PR 8)

`[object_store]` (Pydantic `ObjectStoreSettings`):

| Field | Default | Notes |
| --- | --- | --- |
| `driver` | `local` (base) / `in_memory` (test) / `s3` (production) | `local`, `in_memory`, `s3`, `r2` |
| `local_root` | `None` | Required (fail-fast) when driver is `local` |
| `bucket` | `None` | Required (fail-fast) for `s3`/`r2`; never committed |
| `endpoint_url` | `None` | Optional; overrides S3/R2-compatible endpoint |
| `region` | `None` | Optional AWS region |
| `access_key_id` / `secret_access_key` / `session_token` | `None` | Credentials; redacted from diagnostics |
| `prefix` | `None` | Optional physical-key prefix |
| `connect_timeout_seconds` / `read_timeout_seconds` | `10.0` / `60.0` | Must be positive |

Composition maps `s3` and `r2` both to the single
`S3CompatibleObjectStore` adapter (endpoint/credentials differ). Remote
construction makes no network call at composition (lazy). Environment
overrides follow the standard rules (`DARKULA_OBJECT_STORE__BUCKET`, ...),
with unset/empty no-ops and credential redaction in `diagnostic_summary()`.

### Composition

`load_settings()` returns validated `Settings`; `darkula/composition.py`
selects concrete implementations centrally. Selecting an unavailable
production driver (LangSmith/Langfuse) raises
`UnavailableDriverError` (fail fast) — never a silent fake substitution.
PR 5 ships the `redpanda` DataStream driver alongside the deterministic
`fake` driver; PR 8 delivers the `s3`/`r2` S3-compatible ObjectStore
driver; PR 10 delivers the `openai` LlmClient driver alongside `fake`;
PR 4/3 shipped `local`/`in_memory` ObjectStore drivers, the `fake`
LlmClient driver, and the `none` agent-observability backend.

### Required environment-variable test hygiene

Loader tests clear ambient `DARKULA_*` variables before each test and
manage process state through `monkeypatch` (restored automatically), so
the suite is order-independent and never depends on the developer's shell.

### Collection group (PR 9)

`[collection]` (Pydantic `CollectionSettings`) — behavior fields for the
managed-source collection lifecycle:

| Field | Default | Notes |
| --- | --- | --- |
| `schedule_batch_size` | `10` | Bounded positive sweep size for one `CollectionScheduler.schedule_due` call |
| `worker_poll_batch_size` | `10` | Bounded positive poll size for one `CollectionWorker.process_once` call |
| `execution_lease_seconds` | `600.0` | Positive RUNNING lease that a crashed worker's recovery must outlive (prefer lease >= max bounded crawl + ingestion margin) |
| `max_run_attempts` | `3` | Bounded `[1, 10]` same-run reclaim attempts; each attempt is new crawl provenance |

Policy schedule bounds are fixed domain constants in v0.1
(`MIN_POLICY_INTERVAL_SECONDS` / `MAX_POLICY_INTERVAL_SECONDS` in
`darkula.domain.collection`); no operator-facing interval settings exist.
Environment overrides follow the standard rules
(`DARKULA_COLLECTION__MAX_RUN_ATTEMPTS`, ...) with unset/empty no-ops.

### LLM provider group (PR 10)

`[llm]` (Pydantic `LlmSettings`) gains the provider-backed driver:

| Field | Default | Notes |
| --- | --- | --- |
| `driver` | `fake` | `fake` (deterministic `FakeLlmClient`) or `openai` (delivered PR 10 adapter) |
| `timeout_seconds` | `None` | Bounded per-attempt timeout in `(0, 600]`; `None` falls back to the adapter default `60.0`; never unbounded |

`[llm.provider]` (Pydantic `LlmProviderSettings`) — only the fields the
delivered adapter needs:

| Field | Default | Notes |
| --- | --- | --- |
| `model_name` | `None` | **Required (fail-fast) when driver is `openai`** |
| `api_key` | `None` | **Required (fail-fast) when driver is `openai`**; secret — never logged, `<redacted>` in diagnostics |
| `base_url` | `None` | Optional endpoint override |

There is deliberately **no retry-count setting**: one `LlmClient` call is
one model attempt (`max_retries=0`), so hidden retries are structurally
impossible. Environment overrides follow the standard rules
(`DARKULA_LLM__DRIVER`, `DARKULA_LLM__PROVIDER__MODEL_NAME`, ...) with
unset/empty no-ops; an unavailable provider selection fails fast at
composition (`UnavailableDriverError`) and never silently substitutes
`FakeLlmClient`.

### Reconnaissance group (PR 10)

`[recon]` (Pydantic `ReconSettings`) — trusted-side hard maxima for the
bounded Coordinator <-> ReconAgent <-> Crawler loop. The model can never
increase them:

| Field | Default | Notes |
| --- | --- | --- |
| `max_turns` | `4` | Max agent decision turns per execution |
| `max_inspections` | `3` | Max authorized crawler inspections per execution |
| `max_pages_per_inspection` | `6` | Crawler page budget per inspection (<= 200) |
| `max_requests_per_inspection` | `40` | Crawler request budget per inspection (<= 500) |
| `max_depth_per_inspection` | `2` | Crawler depth budget per inspection (<= 10) |
| `inspection_timeout_seconds` | `90.0` | Crawler timeout per inspection, in `(0, 600]` |
| `max_evidence_items` | `12` | In-memory evidence registry cap (<= 32) |
| `max_excerpt_chars` | `800` | Deterministic per-observation excerpt/title truncation |
| `max_context_chars` | `6000` | Deterministic aggregate evidence-context cap (>= `max_excerpt_chars`) |
| `structured_output_repair_attempts` | `1` | Accounted `INVALID_STRUCTURED_OUTPUT` repair attempts in `[0, 3]` |

Environment overrides follow the standard rules
(`DARKULA_RECON__MAX_TURNS`, ...) with unset/empty no-ops; invalid values
fail closed at validation.

### Extraction group (PR 11)

PR 11 adds exactly one extraction setting; everything else about
deterministic extraction is a frozen, developer-controlled contract.

| Field | Default | Notes |
| --- | --- | --- |
| `max_entities_per_content` | `5000` | Hard cap on persisted occurrences for one content/profile extraction. Must be within `[1, 1000000]`. Exceeding it fails typed; facts are never silently truncated. |

Overrides follow the standard rules
(`DARKULA_EXTRACTION__MAX_ENTITIES_PER_CONTENT`), with unset/empty no-ops.

Deliberately **not** configurable: extractor regexes/lists, extractor
name/version values, the `deterministic-observables/v1` profile name/version,
the canonical normalized-text byte bound (reused from
`MAX_NORMALIZED_TEXT_BYTES`), and normalization semantics. Changing any of
those is a versioned code change, never a runtime knob.

### Semantic extraction group (PR 12)

Model-backed semantic extraction reuses the composed `LlmClient`; only bounds
are configurable (the vocabulary and prompt are frozen code).

| Field | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | Compose `SemanticExtractionService` with the existing `LlmClient`. |
| `max_entities_per_content` | `500` | Hard cap on accepted semantic occurrences; within `[1, 100000]`. Over-cap fails typed. |
| `max_input_bytes` | `262144` | Hard bound on canonical text sent to the model. |
| `max_attempts` | `1` | Explicit bounded attempts; PR 12 ships a single attempt. Within `[1, 3]`. |

Overrides: `DARKULA_SEMANTIC_EXTRACTION__ENABLED`,
`DARKULA_SEMANTIC_EXTRACTION__MAX_ENTITIES_PER_CONTENT`, ...

### Geography group (PR 12)

| Field | Default | Notes |
| --- | --- | --- |
| `enabled` | `false` | Opt-in geographic resolution. |
| `resolver` | `none` | `none` or `fake`. Enabling geography without a resolver fails fast; the fake is never silently substituted in production and no live provider is delivered. |
| `context_chars` | `200` | Exact code-point context window around a mention (`[0, 2000]`). |
| `max_candidates` | `4` | Reserved resolver-candidate bound. |
| `max_input_bytes` | `1048576` | Hard bound on canonical text read for resolution. |

Overrides: `DARKULA_GEOGRAPHY__ENABLED`, `DARKULA_GEOGRAPHY__RESOLVER`,
`DARKULA_GEOGRAPHY__CONTEXT_CHARS`, ...

Deliberately **not** configurable: the semantic entity vocabulary, response
schema, prompts, grounding rules, resolver identity in code, and the
`semantic-entities/v1` profile identity.

### Relationship extraction group (PR 13)

Content-derived relationship assertions reuse the composed `LlmClient` and
already-persisted entity occurrences; only bounds are configurable.

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Compose `RelationshipExtractionService` with the existing `LlmClient`. |
| `max_relationships_per_content` | `500` | Hard cap on accepted assertions for one content/profile; over-cap fails typed. |
| `max_model_candidates` | `500` | Hard bound on raw model-returned candidates (bounds untrusted output). |
| `max_support_chars` | `4096` | Hard bound on one stored/copied support text (code points). |
| `max_context_chars` | `256` | Hard per-candidate bound on disambiguation context (`[0, 4096]`). |
| `max_input_bytes` | `262144` | Hard bound on canonical text sent to the model. |

Overrides: `DARKULA_RELATIONSHIP_EXTRACTION__ENABLED`,
`DARKULA_RELATIONSHIP_EXTRACTION__MAX_RELATIONSHIPS_PER_CONTENT`, ...

Deliberately **not** configurable: the `RelationshipPredicate` vocabulary,
response schema, prompts, endpoint-ref catalog ordering/grounding rules,
extractor identity in code, and the `relationship-assertions/v1` profile
identity.

### Source analysis group (PR 14)

Historical source analysis reuses the composed `LlmClient` and persisted
Darkula observations; only bounds are configurable.

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Compose `SourceAnalysisService` with the existing `LlmClient`; disabled -> `Runtime.source_analysis_service is None`. |
| `max_window_days` | `30` | Maximum historical window one analysis request may span (`<= 3650`). |
| `max_evidence_items` | `500` | Hard cap on evidence items in one bounded context. |
| `max_context_chars` | `30000` | Hard aggregate bound (code points) on the analysis context text. |
| `max_evidence_summary_chars` | `1000` | Hard per-evidence bound on one trusted summary (`<= max_context_chars`). |
| `max_evidence_refs` | `100` | Hard cap on model-returned evidence references. |

Overrides: `DARKULA_SOURCE_ANALYSIS__ENABLED`,
`DARKULA_SOURCE_ANALYSIS__MAX_WINDOW_DAYS`,
`DARKULA_SOURCE_ANALYSIS__MAX_EVIDENCE_ITEMS`, ...

Deliberately **not** configurable: the `source-analysis/v1` profile identity,
`source.analyze` operation name, `source-analysis-v1` prompt, response/
characteristics schema, evidence-kind/ref format, score semantics, and
grounding rules. A semantic change requires a new profile version.

### Agent observability group (PR 15)

Selects and configures the agent/LLM observability backend. The existing LLM
credentials are reused for evaluations and are not duplicated here.

| Setting | Default | Meaning |
| --- | --- | --- |
| `backend` | `none` | `none` (no-op) or `langsmith` (PR 15 adapter). `langfuse` fails fast as not delivered. |
| `project` | `None` | LangSmith project name (required for the `langsmith` backend). |
| `api_key` | `None` | LangSmith API key; falls back to `LANGSMITH_API_KEY`/`LANGCHAIN_API_KEY`. Secret; redacted in diagnostics. |
| `endpoint_url` | `None` | Optional LangSmith API endpoint override (self-hosted). |

Overrides: `DARKULA_AGENT_OBSERVABILITY__BACKEND`,
`DARKULA_AGENT_OBSERVABILITY__PROJECT`, ...

Selecting `langsmith` without a project or API key fails fast at composition
(no silent `none` fallback).

### Evaluation group (PR 15)

Evaluation is explicit and opt-in; the ordinary build never requires it.

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Enable the explicit evaluation CLI. |
| `live_model_enabled` | `false` | Request a live-model run; fails fast when the composed `LlmClient` is the fake. |
| `dataset` | `darkula-cross-source` | Scenario/dataset identity for default runs. |
| `case_ids` | `()` | Case filter; empty selects the full registered suite. |
| `output_dir` | `.eval-results` | Bounded artifact output root (git-ignored). |
| `fail_on_case_error` | `true` | CLI exits non-zero when a case fails. |
| `quality_gate_enabled` | `false` | Enable an explicit caller-supplied quality gate. |

Overrides: `DARKULA_EVALUATION__ENABLED`,
`DARKULA_EVALUATION__LIVE_MODEL_ENABLED`, ...

Deliberately **not** configurable: metric definitions, evaluator semantics,
case expectations, and any universal quality threshold. Thresholds are
caller-supplied and explicit; there is no hard-coded model-quality gate.
