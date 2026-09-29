# SPDX-License-Identifier: AGPL-3.0-only
"""Typed configuration skeleton (PR 2).

PR 2 freezes only the typed, immutable subsystem grouping and the Pydantic
Settings contract. It explicitly does **not** implement the full profile /
file / dotenv / local-override merge machinery:

- profile-file loading and merging, dotenv, and local-user override
  resolution are owned by PR 3;
- environment variables are the ultimate override: a non-empty
  ``DARKULA_*`` variable wins, while an unset or **empty** environment
  variable has no override effect (``env_ignore_empty = True``);
- unknown fields fail closed (``extra = "forbid"`` everywhere);
- nested environment variables use the ``__`` delimiter, so the
  ``DARKULA_DATASTREAM__DRIVER`` variable selects the stream driver.

The profile selector concept is frozen as ``config_profile``, which is read
from the documented ``DARKULA_CONFIG_PROFILE`` variable; no profile
resolution is performed here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Documented environment variable that selects a configuration profile.
#: PR 3 owns profile loading/merging; PR 2 only freezes the selector name.
CONFIG_PROFILE_ENV_VAR = "DARKULA_CONFIG_PROFILE"


class DataStreamDriver(StrEnum):
    """Selected DataStream implementation driver (PR 5 owns the adapter)."""

    REDPANDA = "redpanda"


class ObjectStoreDriver(StrEnum):
    """Selected ObjectStore implementation driver (PR 8 owns adapters)."""

    LOCAL = "local"
    S3 = "s3"
    R2 = "r2"


class AgentObservabilityBackend(StrEnum):
    """Selected agent-observability backend (PR 15 owns adapters)."""

    NONE = "none"
    LANGSMITH = "langsmith"
    LANGFUSE = "langfuse"


class DatabaseSettings(BaseModel):
    """Structured-domain persistence group (PostgreSQL; PR 4 owns schema)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "localhost"
    port: int = 5432
    name: str = "darkula"


class DataStreamSettings(BaseModel):
    """Asynchronous command/event plane selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: DataStreamDriver = DataStreamDriver.REDPANDA


class ObjectStoreSettings(BaseModel):
    """Large/raw artifact storage selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: ObjectStoreDriver = ObjectStoreDriver.LOCAL


class CrawlerSettings(BaseModel):
    """Crawler/sandbox group; behavior fields are owned by PR 7."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_enabled: bool = False


class LlmSettings(BaseModel):
    """LLM invocation group; provider selection is owned by PR 10."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float | None = None


class AgentObservabilitySettings(BaseModel):
    """Agent/LLM observability backend selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: AgentObservabilityBackend = AgentObservabilityBackend.NONE


class TelemetrySettings(BaseModel):
    """Operational OpenTelemetry group; PR 3 owns provider/exporter setup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    otlp_endpoint: str | None = None


class ExtractionSettings(BaseModel):
    """Content-extraction group; behavior fields are owned by PR 11/12."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CollectionSettings(BaseModel):
    """Collection group; behavior fields are owned by PR 9."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Settings(BaseSettings):
    """Root typed settings with the frozen pydantic-settings contract.

    ``env_prefix`` is ``DARKULA_`` and ``env_ignore_empty`` is always true,
    preserving the documented rule that a non-empty process environment
    variable is the ultimate override while an unset or empty variable has
    no effect. Unknown fields fail closed.
    """

    model_config = SettingsConfigDict(
        env_prefix="DARKULA_",
        env_ignore_empty=True,
        extra="forbid",
        env_nested_delimiter="__",
    )

    config_profile: str | None = None
    """Selected profile name (documented concept only; PR 3 resolves it)."""

    database: DatabaseSettings = DatabaseSettings()
    datastream: DataStreamSettings = DataStreamSettings()
    object_store: ObjectStoreSettings = ObjectStoreSettings()
    crawler: CrawlerSettings = CrawlerSettings()
    llm: LlmSettings = LlmSettings()
    agent_observability: AgentObservabilitySettings = AgentObservabilitySettings()
    telemetry: TelemetrySettings = TelemetrySettings()
    extraction: ExtractionSettings = ExtractionSettings()
    collection: CollectionSettings = CollectionSettings()


__all__ = [
    "CONFIG_PROFILE_ENV_VAR",
    "AgentObservabilityBackend",
    "AgentObservabilitySettings",
    "CollectionSettings",
    "CrawlerSettings",
    "DataStreamDriver",
    "DataStreamSettings",
    "DatabaseSettings",
    "ExtractionSettings",
    "LlmSettings",
    "ObjectStoreDriver",
    "ObjectStoreSettings",
    "Settings",
    "TelemetrySettings",
]
