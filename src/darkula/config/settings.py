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

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Documented environment variable that selects a configuration profile.
#: PR 3 owns profile loading/merging; PR 2 only freezes the selector name.
CONFIG_PROFILE_ENV_VAR = "DARKULA_CONFIG_PROFILE"


class DataStreamDriver(StrEnum):
    """Selected DataStream implementation driver.

    ``FAKE`` is the deterministic, offline :class:`FakeDataStream` delivered
    by PR 3. ``REDPANDA`` remains the future adapter (PR 5); selecting it in
    PR 3 validates but fails fast at composition.
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
    "DatabaseSettings",
    "ExtractionSettings",
    "LlmDriver",
    "LlmSettings",
    "ObjectStoreDriver",
    "ObjectStoreSettings",
    "Settings",
    "TelemetrySettings",
]
