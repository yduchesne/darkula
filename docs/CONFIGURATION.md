# Darkula Configuration

## PR 3 status — layered resolution delivered

PR 3 implemented the full layered Pydantic Settings resolution in
`darkula/config/settings.py` (typed field definitions) and
`darkula/config/loader.py` (file loading, deep merge, env-last semantics,
safe diagnostics).

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
  `production` (selects Redpanda/S3, which do not exist in PR 3 and
  therefore fail fast at composition until PR 5/8).

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

### Composition

`load_settings()` returns validated `Settings`; `darkula/composition.py`
selects concrete implementations centrally. Selecting an unavailable
production driver (Redpanda, S3/R2, LangSmith/Langfuse, provider LLM)
raises `UnavailableDriverError` (fail fast) — never a silent fake
substitution. PR 3 ships the `fake` DataStream driver, `local`/`in_memory`
ObjectStore drivers, the `fake` LlmClient driver, and the `none`
agent-observability backend.

### Required environment-variable test hygiene

Loader tests clear ambient `DARKULA_*` variables before each test and
manage process state through `monkeypatch` (restored automatically), so
the suite is order-independent and never depends on the developer's shell.
