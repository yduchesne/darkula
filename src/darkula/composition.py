# SPDX-License-Identifier: AGPL-3.0-only
"""Narrow central composition root (PR 3, extended by PR 4 and PR 5).

Receives the resolved :class:`~darkula.config.settings.Settings` and
constructs only implementations that exist:

- ``FakeDataStream`` when the fake stream driver is selected;
- ``RedpandaDataStream`` when the Redpanda driver is selected (PR 5: the
  production DataStream adapter; the fake is never silently substituted
  for an explicitly selected production driver);
- ``InMemoryObjectStore`` / ``LocalFileObjectStore`` for the delivered
  object-store drivers;
- ``FakeLlmClient`` (the only LLM driver);
- ``NoOpAgentObservability`` for the ``NONE`` backend;
- ``PostgresDarkulaSpi`` when the PostgreSQL persistence driver is
  selected (PR 4: the only delivered driver);
- the local telemetry runtime via
  :func:`~darkula.telemetry.setup.configure_telemetry`.

Selection is centralized here: application/domain code never branches on
driver settings. Selecting an unavailable production driver (S3/R2,
LangSmith/Langfuse, future providers) raises
:class:`UnavailableDriverError` immediately; Darkula never silently
substitutes a fake for an explicitly selected production driver.

The composed :class:`PostgresDarkulaSpi` is lazy: constructing it opens no
connections. Callers start it explicitly (``await spi.start()``) before
first use, keeping unit/QA paths infrastructure-free.
"""

from __future__ import annotations

from dataclasses import dataclass

from darkula.app.agent_observability import AgentObservability
from darkula.app.data_stream import DataStream
from darkula.app.llm import LlmClient
from darkula.app.object_store import ObjectStore
from darkula.app.persistence import DarkulaSpi
from darkula.config.settings import (
    AgentObservabilityBackend,
    DatabaseDriver,
    DataStreamDriver,
    LlmDriver,
    ObjectStoreDriver,
    Settings,
)
from darkula.infrastructure.data_stream import RedpandaDataStream
from darkula.infrastructure.object_store import (
    InMemoryObjectStore,
    LocalFileObjectStore,
)
from darkula.infrastructure.observability import NoOpAgentObservability
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.telemetry.setup import TelemetryRuntime, configure_telemetry
from darkula.testing.fake_data_stream import FakeDataStream
from darkula.testing.fake_llm import FakeLlmClient


class UnavailableDriverError(RuntimeError):
    """An explicitly selected implementation does not exist in PR 3.

    Raised instead of silently substituting a fake. The message is bounded
    and names only the unavailable driver category.
    """


@dataclass(frozen=True, slots=True)
class Runtime:
    """The composed runtime: every delivery-bound implementation.

    ``persistence`` is lazy: opening it requires an explicit
    ``await runtime.persistence.start()`` before first use.
    """

    data_stream: DataStream
    object_store: ObjectStore
    llm: LlmClient
    agent_observability: AgentObservability
    persistence: DarkulaSpi
    telemetry: TelemetryRuntime


def _compose_data_stream(settings: Settings) -> DataStream:
    if settings.datastream.driver is DataStreamDriver.FAKE:
        return FakeDataStream()
    if settings.datastream.driver is DataStreamDriver.REDPANDA:
        return RedpandaDataStream(
            bootstrap_servers=settings.datastream.bootstrap_servers,
            client_id=settings.datastream.client_id,
            poll_timeout_ms=settings.datastream.poll_timeout_ms,
            max_poll_records=settings.datastream.max_poll_records,
        )
    raise UnavailableDriverError(  # defensive; validation rejects unknowns
        f"unsupported DataStream driver: {settings.datastream.driver}"
    )


def _compose_object_store(settings: Settings) -> ObjectStore:
    driver = settings.object_store.driver
    if driver is ObjectStoreDriver.IN_MEMORY:
        return InMemoryObjectStore()
    if driver is ObjectStoreDriver.LOCAL:
        root = settings.object_store.local_root
        if root is None:
            raise UnavailableDriverError(
                "LocalFileObjectStore requires object_store.local_root"
            )
        return LocalFileObjectStore(root)
    if driver is ObjectStoreDriver.S3:
        raise UnavailableDriverError(
            "S3 ObjectStore is not available in PR 3 (PR 8 owns the adapter)"
        )
    if driver is ObjectStoreDriver.R2:
        raise UnavailableDriverError(
            "R2 ObjectStore is not available in PR 3 (PR 8 owns the adapter)"
        )
    raise UnavailableDriverError(
        f"unsupported ObjectStore driver: {driver}"
    )  # defensive; validation rejects unknowns


def _compose_llm(settings: Settings) -> LlmClient:
    if settings.llm.driver is LlmDriver.FAKE:
        return FakeLlmClient()
    raise UnavailableDriverError(
        f"unsupported LLM driver: {settings.llm.driver} (provider drivers "
        "are PR 10 work)"
    )  # defensive; validation rejects unknowns


def _compose_observability(settings: Settings) -> AgentObservability:
    backend = settings.agent_observability.backend
    if backend is AgentObservabilityBackend.NONE:
        return NoOpAgentObservability()
    if backend is AgentObservabilityBackend.LANGSMITH:
        raise UnavailableDriverError(
            "LangSmith agent observability is not available in PR 3 (PR 15)"
        )
    if backend is AgentObservabilityBackend.LANGFUSE:
        raise UnavailableDriverError(
            "Langfuse agent observability is not available in PR 3 (PR 15)"
        )
    raise UnavailableDriverError(
        f"unsupported agent-observability backend: {backend}"
    )  # defensive; validation rejects unknowns


def _compose_persistence(settings: Settings) -> DarkulaSpi:
    if settings.database.driver is DatabaseDriver.POSTGRESQL:
        return PostgresDarkulaSpi.from_settings(settings.database)
    raise UnavailableDriverError(  # defensive; validation rejects unknowns
        f"unsupported persistence driver: {settings.database.driver}"
    )


def compose(*, settings: Settings) -> Runtime:
    """Compose every runtime implementation from resolved settings."""
    return Runtime(
        data_stream=_compose_data_stream(settings),
        object_store=_compose_object_store(settings),
        llm=_compose_llm(settings),
        agent_observability=_compose_observability(settings),
        persistence=_compose_persistence(settings),
        telemetry=configure_telemetry(
            enabled=settings.telemetry.enabled,
            service_name=settings.telemetry.service_name,
        ),
    )


__all__ = ["Runtime", "UnavailableDriverError", "compose"]
