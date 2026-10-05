# SPDX-License-Identifier: AGPL-3.0-only
"""Narrow central composition root (PR 3, extended by PR 4 and PR 5).

Receives the resolved :class:`~darkula.config.settings.Settings` and
constructs only implementations that exist:

- ``FakeDataStream`` when the fake stream driver is selected;
- ``RedpandaDataStream`` when the Redpanda driver is selected (PR 5: the
  production DataStream adapter; the fake is never silently substituted
  for an explicitly selected production driver);
- ``InMemoryObjectStore`` / ``LocalFileObjectStore`` / ``S3CompatibleObjectStore``
  for the delivered object-store drivers (PR 8: ``s3`` and ``r2`` both
  compose the S3-compatible adapter);
- ``DeterministicContentNormalizer`` + ``ContentIngestService`` for the PR 8
  normalization/content boundary;
- ``FakeLlmClient`` (the only LLM driver);
- ``NoOpAgentObservability`` for the ``NONE`` backend;
- ``PostgresDarkulaSpi`` when the PostgreSQL persistence driver is
  selected (PR 4: the only delivered driver);
- the local telemetry runtime via
  :func:`~darkula.telemetry.setup.configure_telemetry`.

Selection is centralized here: application/domain code never branches on
driver settings. Selecting an unavailable production driver (LangSmith/
Langfuse, provider LLM, a remote ObjectStore without a configured bucket)
raises :class:`UnavailableDriverError` immediately; Darkula never silently
substitutes a fake for an explicitly selected production driver.

The composed :class:`PostgresDarkulaSpi` is lazy: constructing it opens no
connections. Callers start it explicitly (``await spi.start()``) before
first use, keeping unit/QA paths infrastructure-free.
"""

from __future__ import annotations

from dataclasses import dataclass

from darkula.app.agent_observability import AgentObservability
from darkula.app.artifacts import ArtifactStorageService
from darkula.app.collection import SourceCollectionService
from darkula.app.collection_scheduler import CollectionScheduler
from darkula.app.collection_worker import CollectionWorker
from darkula.app.content import ContentIngestService
from darkula.app.data_stream import DataStream
from darkula.app.llm import LlmClient
from darkula.app.normalization import ContentNormalizer, DeterministicContentNormalizer
from darkula.app.object_store import ObjectStore
from darkula.app.persistence import DarkulaSpi
from darkula.app.recon import ReconCoordinator
from darkula.app.recon_agent import ReconAgent
from darkula.config.settings import (
    AgentObservabilityBackend,
    DatabaseDriver,
    DataStreamDriver,
    LlmDriver,
    ObjectStoreDriver,
    SandboxDriver,
    Settings,
)
from darkula.crawler import Crawler, CrawlerController
from darkula.infrastructure.data_stream import RedpandaDataStream
from darkula.infrastructure.llm import OpenAiLlmClient
from darkula.infrastructure.object_store import (
    InMemoryObjectStore,
    LocalFileObjectStore,
    S3CompatibleObjectStore,
)
from darkula.infrastructure.observability import NoOpAgentObservability
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.infrastructure.sandbox import PodmanSandbox
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
    ``await runtime.persistence.start()`` before first use. ``crawler`` is
    lazy with respect to Podman: constructing it never touches podman.
    """

    data_stream: DataStream
    object_store: ObjectStore
    content_normalizer: ContentNormalizer
    content_ingest: ContentIngestService
    collection_service: SourceCollectionService
    collection_scheduler: CollectionScheduler
    collection_worker: CollectionWorker
    llm: LlmClient
    agent_observability: AgentObservability
    recon_agent: ReconAgent
    recon_coordinator: ReconCoordinator
    persistence: DarkulaSpi
    crawler: Crawler
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
    raise UnavailableDriverError(
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
    if driver is ObjectStoreDriver.S3 or driver is ObjectStoreDriver.R2:
        if not settings.object_store.bucket:
            raise UnavailableDriverError(
                f"{driver.value} ObjectStore requires object_store.bucket"
            )
        return S3CompatibleObjectStore(
            bucket=settings.object_store.bucket,
            endpoint_url=settings.object_store.endpoint_url,
            region=settings.object_store.region,
            access_key_id=settings.object_store.access_key_id,
            secret_access_key=settings.object_store.secret_access_key,
            session_token=settings.object_store.session_token,
            prefix=settings.object_store.prefix,
            connect_timeout_seconds=settings.object_store.connect_timeout_seconds,
            read_timeout_seconds=settings.object_store.read_timeout_seconds,
        )
    raise UnavailableDriverError(
        f"unsupported ObjectStore driver: {driver}"
    )  # defensive; validation rejects unknowns


def _compose_content_normalizer(object_store: ObjectStore) -> ContentNormalizer:
    """Compose the deterministic normalization capability (PR 8).

    The normalizer stores artifact bytes through the composed ObjectStore;
    it holds no database connection (persistence is the caller's short UoW).
    """
    return DeterministicContentNormalizer(
        artifact_service=ArtifactStorageService(object_store=object_store)
    )


def _compose_content_ingest(
    normalizer: ContentNormalizer, persistence: DarkulaSpi
) -> ContentIngestService:
    """Compose the content-ingestion orchestration (PR 8)."""
    return ContentIngestService(normalizer=normalizer, spi=persistence)


def _compose_llm(settings: Settings) -> LlmClient:
    if settings.llm.driver is LlmDriver.FAKE:
        return FakeLlmClient()
    if settings.llm.driver is LlmDriver.OPENAI:
        provider = settings.llm.provider
        if not provider.model_name or not provider.api_key:
            raise UnavailableDriverError(
                "OpenAI LlmClient requires llm.provider.model_name and "
                "llm.provider.api_key"
            )
        return OpenAiLlmClient(
            model_name=provider.model_name,
            api_key=provider.api_key,
            base_url=provider.base_url,
            timeout_seconds=settings.llm.timeout_seconds or 60.0,
        )
    raise UnavailableDriverError(
        f"unsupported LLM driver: {settings.llm.driver}"
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


def _compose_crawler(settings: Settings) -> Crawler:
    """Compose the single Crawler capability (PR 7).

    Only the real Podman sandbox driver exists; the fake is never silently
    substituted for an explicitly selected production driver.
    """
    if settings.crawler.driver is SandboxDriver.PODMAN:
        return CrawlerController(
            settings=settings.crawler,
            sandbox=PodmanSandbox.from_settings(settings.crawler),
        )
    raise UnavailableDriverError(
        f"unsupported sandbox driver: {settings.crawler.driver}"
    )


def _compose_collection(
    settings: Settings,
    persistence: DarkulaSpi,
    crawler: Crawler,
    content_ingest: ContentIngestService,
    data_stream: DataStream,
) -> tuple[SourceCollectionService, CollectionScheduler, CollectionWorker]:
    """Compose the PR 9 collection capability.

    The service/scheduler/worker share one persistence SPI, the single PR 7
    Crawler, the PR 8 ContentIngestService, and the DataStream. The worker
    and service never hold a database transaction across crawler/HTTP/
    ObjectStore/broker I/O (enforced by their short-UoW structure).
    """
    service = SourceCollectionService(
        spi=persistence,
        crawler=crawler,
        content_ingest=content_ingest,
        settings=settings.collection,
    )
    scheduler = CollectionScheduler(spi=persistence)
    worker = CollectionWorker(
        spi=persistence,
        data_stream=data_stream,
        service=service,
        settings=settings.collection,
    )
    return service, scheduler, worker


def _compose_recon(
    persistence: DarkulaSpi,
    llm: LlmClient,
    observability: AgentObservability,
    crawler: Crawler,
    settings: Settings,
) -> tuple[ReconAgent, ReconCoordinator]:
    """Compose the PR 10 reconnaissance capability.

    The ReconAgent shares the single composed LlmClient/AgentObservability
    and the Coordinator shares the single PR 7 Crawler and the persistence
    SPI; no rival recon abstractions are created.
    """
    agent = ReconAgent(
        llm=llm,
        observability=observability,
        settings=settings.recon,
    )
    coordinator = ReconCoordinator(
        spi=persistence,
        agent=agent,
        crawler=crawler,
        settings=settings.recon,
    )
    return agent, coordinator


def compose(*, settings: Settings) -> Runtime:
    """Compose every runtime implementation from resolved settings."""
    object_store = _compose_object_store(settings)
    persistence = _compose_persistence(settings)
    content_normalizer = _compose_content_normalizer(object_store)
    content_ingest = _compose_content_ingest(content_normalizer, persistence)
    data_stream = _compose_data_stream(settings)
    crawler = _compose_crawler(settings)
    collection_service, collection_scheduler, collection_worker = _compose_collection(
        settings=settings,
        persistence=persistence,
        crawler=crawler,
        content_ingest=content_ingest,
        data_stream=data_stream,
    )
    llm = _compose_llm(settings)
    observability = _compose_observability(settings)
    recon_agent, recon_coordinator = _compose_recon(
        persistence=persistence,
        llm=llm,
        observability=observability,
        crawler=crawler,
        settings=settings,
    )
    return Runtime(
        data_stream=data_stream,
        object_store=object_store,
        content_normalizer=content_normalizer,
        content_ingest=content_ingest,
        collection_service=collection_service,
        collection_scheduler=collection_scheduler,
        collection_worker=collection_worker,
        llm=llm,
        agent_observability=observability,
        recon_agent=recon_agent,
        recon_coordinator=recon_coordinator,
        persistence=persistence,
        crawler=crawler,
        telemetry=configure_telemetry(
            enabled=settings.telemetry.enabled,
            service_name=settings.telemetry.service_name,
        ),
    )


__all__ = ["Runtime", "UnavailableDriverError", "compose"]
