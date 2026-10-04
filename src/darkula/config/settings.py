# SPDX-License-Identifier: AGPL-3.0-only
"""Typed configuration (PR 2 skeleton extended by PR 3).

PR 2 froze the typed, immutable subsystem grouping and the Pydantic
Settings contract. PR 3 extends the field set (local object-store root,
telemetry service name, deterministic fake drivers) and adds the layered
profile/merge machinery in :mod:`darkula.config.loader`; this module still
performs **no** file loading.

Frozen contract:

- environment variables are the ultimate override: a non-empty
  ``DARKULA_*`` variable wins, while an unset or **empty** environment
  variable has no override effect (``env_ignore_empty = True``);
- unknown fields fail closed (``extra = "forbid"`` everywhere);
- nested environment variables use the ``__`` delimiter, so the
  ``DARKULA_DATASTREAM__DRIVER`` variable selects the stream driver.

The profile selector concept is frozen as ``config_profile``, read from
the documented ``DARKULA_CONFIG_PROFILE`` variable; loading/merging is
owned by :mod:`darkula.config.loader`.
"""

from __future__ import annotations

import pathlib
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Documented environment variable that selects a configuration profile.
#: PR 3 owns profile loading/merging; PR 2 only freezes the selector name.
CONFIG_PROFILE_ENV_VAR = "DARKULA_CONFIG_PROFILE"


class DataStreamDriver(StrEnum):
    """Selected DataStream implementation driver.

    ``REDPANDA`` is the production :class:`RedpandaDataStream` delivered by
    PR 5. ``FAKE`` is the deterministic, offline :class:`FakeDataStream`
    (PR 3); production selection never silently substitutes the fake.
    """

    REDPANDA = "redpanda"
    FAKE = "fake"


class ObjectStoreDriver(StrEnum):
    """Selected ObjectStore implementation driver.

    ``LOCAL``/``IN_MEMORY`` are delivered by PR 3; ``S3``/``R2`` remain
    future adapters (PR 8) that validate but fail fast at composition.
    """

    LOCAL = "local"
    IN_MEMORY = "in_memory"
    S3 = "s3"
    R2 = "r2"


class AgentObservabilityBackend(StrEnum):
    """Selected agent-observability backend (PR 15 owns adapters)."""

    NONE = "none"
    LANGSMITH = "langsmith"
    LANGFUSE = "langfuse"


class LlmDriver(StrEnum):
    """Selected LlmClient implementation driver.

    ``FAKE`` is the only PR 3 implementation: the deterministic, offline
    :class:`FakeLlmClient`. Production/provider drivers are future work
    (PR 10); any unavailable selection fails closed at validation/composition.
    """

    FAKE = "fake"


class DatabaseDriver(StrEnum):
    """Selected structured-domain persistence driver (PR 4).

    ``POSTGRESQL`` is the only delivered driver. There is deliberately no
    fake/in-memory production fallback: an unavailable driver fails closed.
    """

    POSTGRESQL = "postgresql"


class DatabaseSettings(BaseModel):
    """Structured-domain persistence group (PostgreSQL; PR 4).

    ``password`` is optional so operator environments can inject it via a
    non-empty ``DARKULA_DATABASE__PASSWORD`` variable; diagnostic rendering
    redacts password-like keys. Connection strings are never echoed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: DatabaseDriver = DatabaseDriver.POSTGRESQL
    host: str = "localhost"
    #: Host-published Darkula PostgreSQL port (container port 5432).
    port: int = 35432
    name: str = "darkula"
    user: str = "darkula"
    password: str | None = None
    connect_timeout_seconds: float = 10.0
    pool_min_size: int = 1
    pool_max_size: int = 4

    def conninfo(self) -> str:
        """Return the libpq connection string for these settings.

        Includes the password when configured. Callers must never log or
        echo this value; it may contain credentials.
        """
        parts = [
            f"host={self.host}",
            f"port={self.port}",
            f"dbname={self.name}",
            f"user={self.user}",
            f"connect_timeout={self.connect_timeout_seconds:g}",
        ]
        if self.password is not None:
            parts.append(f"password={self.password}")
        return " ".join(parts)

    @field_validator("connect_timeout_seconds")
    @classmethod
    def _validate_connect_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        return value

    @model_validator(mode="after")
    def _validate_pool_bounds(self) -> Self:
        if self.pool_min_size < 1 or self.pool_max_size < self.pool_min_size:
            raise ValueError(
                "pool sizes must satisfy 1 <= pool_min_size <= pool_max_size"
            )
        return self


class DataStreamSettings(BaseModel):
    """Asynchronous command/event plane selection (PR 3, extended PR 5).

    ``driver`` selects the DataStream implementation centrally. ``FAKE`` is
    the deterministic offline :class:`FakeDataStream`; ``REDPANDA`` is the
    production Redpanda adapter delivered by PR 5. Only required typed
    Redpanda settings exist here -- no arbitrary Kafka configuration
    dictionary and no committed secrets; non-empty environment variables
    remain the ultimate override (``env_ignore_empty``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: DataStreamDriver = DataStreamDriver.REDPANDA
    #: Comma-separated ``host:port`` bootstrap servers for Kafka clients.
    bootstrap_servers: str = "darkula-redpanda:9092"
    #: Stable client identifier for the producer/consumer clients.
    client_id: str = "darkula"
    #: Bounded poll timeout in milliseconds (never a no-timeout poll).
    poll_timeout_ms: int = 500
    #: Bounded maximum records returned by one poll.
    max_poll_records: int = 100

    @field_validator("bootstrap_servers")
    @classmethod
    def _validate_bootstrap_servers(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("bootstrap_servers must not be blank")
        return value.strip()

    @field_validator("client_id")
    @classmethod
    def _validate_client_id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("client_id must not be blank")
        return value.strip()

    @field_validator("poll_timeout_ms")
    @classmethod
    def _validate_poll_timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError("poll_timeout_ms must be >= 1")
        return value

    @field_validator("max_poll_records")
    @classmethod
    def _validate_max_poll_records(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_poll_records must be >= 1")
        return value


class ObjectStoreSettings(BaseModel):
    """Large/raw artifact storage selection.

    ``local_root`` is the filesystem root to which the PR 3
    :class:`LocalFileObjectStore` confines every key. It is required
    (composition fails fast) when ``driver`` is ``LOCAL`` and optional
    otherwise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: ObjectStoreDriver = ObjectStoreDriver.LOCAL
    local_root: pathlib.Path | None = None


class CrawlerSettings(BaseModel):
    """Crawler/sandbox group; behavior fields are owned by PR 7."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_enabled: bool = False


class LlmSettings(BaseModel):
    """LLM invocation group.

    ``driver`` selects the LlmClient implementation centrally; PR 3 ships
    only :class:`~darkula.testing.fake_llm.FakeLlmClient`. Provider drivers
    are PR 10 work.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: LlmDriver = LlmDriver.FAKE
    timeout_seconds: float | None = None


class AgentObservabilitySettings(BaseModel):
    """Agent/LLM observability backend selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: AgentObservabilityBackend = AgentObservabilityBackend.NONE


class TelemetrySettings(BaseModel):
    """Operational OpenTelemetry group.

    PR 3 wires local tracer/meter providers when ``enabled`` is true. External
    OTLP export remains future work; ``otlp_endpoint`` is reserved metadata
    and is never contacted by PR 3.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    service_name: str = "darkula"
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
    """Selected profile name (documented concept only; loader.py resolves it)."""

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
    "DatabaseDriver",
    "DatabaseSettings",
    "ExtractionSettings",
    "LlmDriver",
    "LlmSettings",
    "ObjectStoreDriver",
    "ObjectStoreSettings",
    "Settings",
    "TelemetrySettings",
]
