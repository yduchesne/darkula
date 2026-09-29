# Darkula Configuration

## PR 2 status — skeleton only

PR 2 delivered the typed skeleton in `darkula/config/settings.py`: a frozen root `Settings` (Pydantic `BaseSettings`) with the documented subsystem groups, immutable `extra = "forbid"` nested models, driver enums, and the profile-selector concept (`config_profile` / `DARKULA_CONFIG_PROFILE`).

Explicitly **not** implemented in PR 2 (owned by PR 3):

- profile-file loading and base/profile/local-override merging;
- dotenv and local user-override resolution;
- secret resolution.

Frozen contract already in force:

- environment variables are the ultimate override;
- unset or **empty** environment variables have no effect (`env_ignore_empty = True`);
- environment prefix is `DARKULA_`;
- nested environment variables use the `__` delimiter (for example `DARKULA_DATASTREAM__DRIVER`);
- unknown settings fields fail closed (`extra = "forbid"`).

## Framework
Use Pydantic Settings for typed configuration. Darkula should reuse ATI's proven configuration concepts after inspecting fresh ATI main, while remaining self-contained.

## Precedence
From lowest to highest:
1. code/default settings
2. base configuration
3. selected profile configuration
4. local/user override configuration
5. non-empty environment variables

**Environment variables are the ultimate override.** An unset variable has no effect. An empty-string environment variable also has no effect and must not erase a value resolved from lower layers.

Profile selection is expected (for example development/test/production) under a Darkula-specific setting such as DARKULA_CONFIG_PROFILE; exact file formats/names remain TBD.

## Organization
Typed nested settings should follow subsystems such as database, datastream, object_store, crawler, llm, agent_observability, telemetry, extraction, and collection.

## Composition
Configuration is resolved and validated at startup. The composition root selects concrete implementations (for example RedpandaDataStream, LocalFileObjectStore/S3 adapter, LangChainLlmClient). Application/domain code depends on interfaces and must not repeatedly branch on driver settings.

## Secrets
Ordinary configuration may contain secret references but should not encourage plaintext secrets. Introduce a secret-resolution abstraction compatible with the ATI approach when implementation begins. Environment-variable override semantics do not justify logging secret values.

## Validation
Fail fast before workers consume messages when required settings or combinations are invalid. Adapter-specific requirements should be validated at composition/startup without leaking provider types into domain code.

## Required tests
Test every precedence boundary, empty/unset environment behavior, type validation, profile selection, invalid adapter combinations, and secret redaction. Environment tests must restore process state and remain order-independent.
