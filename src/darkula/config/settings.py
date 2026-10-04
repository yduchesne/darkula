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

    ``local_root`` confines the PR 3 :class:`LocalFileObjectStore` to one
    filesystem root (required and fail-fast when ``driver`` is ``LOCAL``).
    The S3/R2-compatible production adapter (PR 8) reads the remote fields
    below; ``bucket`` is required for the ``s3``/``r2`` drivers and the rest
    are overridable, with non-empty environment variables as the ultimate
    override. Diagnostics redact the credential-like fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: ObjectStoreDriver = ObjectStoreDriver.LOCAL
    local_root: pathlib.Path | None = None

    # Remote (S3/R2-compatible) fields. Never committed to source control.
    bucket: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    prefix: str | None = None
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 60.0

    @field_validator("bucket", "endpoint_url", "region", "prefix")
    @classmethod
    def _validate_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("remote object-store field must not be blank")
        if any(ord(ch) < 32 for ch in stripped):
            raise ValueError(
                "remote object-store field must not contain control characters"
            )
        return stripped

    @field_validator("connect_timeout_seconds", "read_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("object-store timeouts must be positive")
        return value


class SandboxDriver(StrEnum):
    """Selected Sandbox implementation driver.

    ``PODMAN`` is the only PR 7 implementation: one disposable Darkula-owned
    Podman container per execution. There is deliberately no fake/in-memory
    production fallback; composition fails closed when the driver is
    unavailable and never silently substitutes :class:`FakeSandbox`.
    """

    PODMAN = "podman"


class CrawlerSettings(BaseModel):
    """Crawler/sandbox settings group (behavior fields owned by PR 7).

    Values here are **maxima and defaults** for the trusted controller; the
    sandbox itself enforces hard limits derived from each execution's
    policy. Security defaults are restrictive. No raw Podman CLI argument
    strings are ever accepted from configuration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Selected sandbox implementation driver (explicit; fail closed).
    driver: SandboxDriver = SandboxDriver.PODMAN
    #: Deterministic crawler runtime image tag (never ``latest``).
    runtime_image: str = "localhost/darkula-crawler-runtime:1.63.0"
    #: Darkula-owned internal Podman network used by the podman sandbox to
    #: enforce authorized-only connectivity. Must carry ``darkula.owned=true``;
    #: the tooling provisions it and never touches foreign networks.
    sandbox_network: str = "darkula-intg"
    #: Default execution timeout (seconds) when a request does not set one.
    default_timeout_seconds: float = 60.0
    #: Trusted-side ceiling applied to any requested timeout.
    max_timeout_seconds: float = 600.0
    #: Default maximum pages a crawl may render.
    default_max_pages: int = 20
    #: Trusted-side ceiling applied to any requested page budget.
    max_max_pages: int = 200
    #: Default maximum HTTP requests a crawl may issue.
    default_max_requests: int = 60
    #: Trusted-side ceiling applied to any requested request budget.
    max_max_requests: int = 500
    #: Default maximum navigation depth from the start page.
    default_max_depth: int = 4
    #: Trusted-side ceiling applied to any requested depth budget.
    max_max_depth: int = 10
    #: Default sandbox memory ceiling (bytes) for crawler executions.
    sandbox_memory_bytes: int = 1024 * 1024 * 1024
    #: Default sandbox CPU ceiling for crawler executions.
    sandbox_cpus: float = 2.0
    #: Default sandbox PID ceiling for crawler executions.
    sandbox_pids: int = 512
    #: Hard bound on the workload input payload crossing into the sandbox.
    max_input_bytes: int = 65536
    #: Hard bound on the runtime output payload crossing back.
    max_output_bytes: int = 1024 * 1024
    #: Hard bound on a single observed page's rendered text excerpt.
    max_page_text_bytes: int = 4096
    #: Hard bound on the number of discovered links returned per execution.
    max_discovered_links: int = 500

    @field_validator("runtime_image")
    @classmethod
    def _validate_runtime_image(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("crawler.runtime_image must not be blank")
        if value.strip().endswith(":latest"):
            raise ValueError("crawler.runtime_image must not use the 'latest' tag")
        return value.strip()

    @field_validator("default_timeout_seconds")
    @classmethod
    def _validate_default_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("crawler.default_timeout_seconds must be positive")
        return value

    @field_validator("max_timeout_seconds")
    @classmethod
    def _validate_max_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("crawler.max_timeout_seconds must be positive")
        return value

    @field_validator(
        "default_max_pages",
        "max_max_pages",
        "default_max_requests",
        "max_max_requests",
        "default_max_depth",
        "max_max_depth",
        "sandbox_memory_bytes",
        "sandbox_pids",
    )
    @classmethod
    def _validate_positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("crawler budgets must be positive integers")
        return value

    @field_validator("sandbox_cpus")
    @classmethod
    def _validate_cpus(cls, value: float) -> float:
        if value < 0.25 or value > 32.0:
            raise ValueError("crawler.sandbox_cpus must be within [0.25, 32.0]")
        return value

    @field_validator(
        "max_input_bytes",
        "max_output_bytes",
        "max_page_text_bytes",
        "max_discovered_links",
    )
    @classmethod
    def _validate_positive_bound(cls, value: int) -> int:
        if value < 1:
            raise ValueError("crawler byte/collection bounds must be positive")
        return value

    @model_validator(mode="after")
    def _validate_budget_order(self) -> Self:
        if self.max_timeout_seconds < self.default_timeout_seconds:
            raise ValueError(
                "crawler.max_timeout_seconds must be >= default_timeout_seconds"
            )
        if self.max_max_pages < self.default_max_pages:
            raise ValueError("crawler.max_max_pages must be >= default_max_pages")
        if self.max_max_requests < self.default_max_requests:
            raise ValueError("crawler.max_max_requests must be >= default_max_requests")
        if self.max_max_depth < self.default_max_depth:
            raise ValueError("crawler.max_max_depth must be >= default_max_depth")
        return self


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
    "SandboxDriver",
    "Settings",
    "TelemetrySettings",
]
