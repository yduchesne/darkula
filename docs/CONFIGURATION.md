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
production driver (LangSmith/Langfuse, provider LLM) raises
`UnavailableDriverError` (fail fast) — never a silent fake substitution.
PR 5 ships the `redpanda` DataStream driver alongside the deterministic
`fake` driver; PR 8 delivers the `s3`/`r2` S3-compatible ObjectStore
driver; PR 4/3 shipped `local`/`in_memory` ObjectStore drivers, the `fake`
LlmClient driver, and the `none` agent-observability backend.

### Required environment-variable test hygiene

Loader tests clear ambient `DARKULA_*` variables before each test and
manage process state through `monkeypatch` (restored automatically), so
the suite is order-independent and never depends on the developer's shell.
