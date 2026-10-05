# SPDX-License-Identifier: AGPL-3.0-only
"""Central composition tests (COMP-*): available selections and fail-fast."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from darkula.composition import Runtime, UnavailableDriverError, compose
from darkula.config.loader import load_settings
from darkula.config.settings import (
    DataStreamDriver,
    DataStreamSettings,
    ObjectStoreDriver,
    ObjectStoreSettings,
    Settings,
)
from darkula.infrastructure.data_stream import RedpandaDataStream
from darkula.infrastructure.object_store import (
    InMemoryObjectStore,
    LocalFileObjectStore,
    S3CompatibleObjectStore,
)
from darkula.infrastructure.observability import NoOpAgentObservability
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_data_stream import FakeDataStream
from darkula.testing.fake_llm import FakeLlmClient

#: The shipped repo config tree (hermetic; never the developer's local.toml).
_SHIPPED_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture(autouse=True)
def _clean_darkula_env(monkeypatch: MonkeyPatch) -> None:
    """Remove every ambient DARKULA_* variable before each test."""
    for key in [key for key in os.environ if key.startswith("DARKULA_")]:
        monkeypatch.delenv(key, raising=False)


def _shipped() -> Settings:
    return load_settings(config_dir=_SHIPPED_CONFIG)


class TestDelivered:
    """The default (development) profile composes delivered internals."""

    def test_default_profile_composes_fake_internals(self) -> None:
        runtime = compose(settings=_shipped())
        assert isinstance(runtime, Runtime)
        assert isinstance(runtime.data_stream, FakeDataStream)
        assert isinstance(runtime.object_store, LocalFileObjectStore)
        assert isinstance(runtime.llm, FakeLlmClient)
        assert isinstance(runtime.agent_observability, NoOpAgentObservability)
        assert isinstance(runtime.persistence, PostgresDarkulaSpi)
        assert runtime.telemetry.tracer_provider is not None
        assert runtime.telemetry.meter_provider is not None

    def test_persistence_is_lazy_never_connected_at_compose(self) -> None:
        runtime = compose(settings=_shipped())
        spi = runtime.persistence
        assert isinstance(spi, PostgresDarkulaSpi)
        assert spi.is_started is False

    def test_test_profile_composes_in_memory_object_store(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_CONFIG_PROFILE", "test")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert isinstance(runtime.object_store, InMemoryObjectStore)

    def test_disabled_telemetry_yields_empty_runtime(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_TELEMETRY__ENABLED", "false")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert runtime.telemetry.tracer_provider is None
        assert runtime.telemetry.meter_provider is None

    def test_compose_is_repeatable_without_global_side_effects(self) -> None:
        first = compose(settings=_shipped())
        second = compose(settings=_shipped())
        assert type(first.data_stream) is type(second.data_stream)
        assert first.object_store is not second.object_store
        assert isinstance(first.object_store, LocalFileObjectStore)


class TestFailFast:
    """Unavailable production selections fail instead of faking."""

    def test_redpanda_datastream_composes_production_adapter(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_DATASTREAM__DRIVER", "redpanda")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert isinstance(runtime.data_stream, RedpandaDataStream)

    def test_production_profile_composes_s3_with_bucket(self) -> None:
        # PR 8 delivered the S3-compatible adapter; production selects s3 and
        # requires the operator to provide a bucket.
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_CONFIG_PROFILE", "production")
            monkeypatch.setenv("DARKULA_OBJECT_STORE__BUCKET", "darkula-prod")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert isinstance(runtime.object_store, S3CompatibleObjectStore)

    def test_s3_with_bucket_composes_adapter(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_OBJECT_STORE__DRIVER", "s3")
            monkeypatch.setenv("DARKULA_OBJECT_STORE__BUCKET", "darkula-s3")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert isinstance(runtime.object_store, S3CompatibleObjectStore)

    def test_r2_with_bucket_composes_same_adapter(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_OBJECT_STORE__DRIVER", "r2")
            monkeypatch.setenv("DARKULA_OBJECT_STORE__BUCKET", "darkula-r2")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert isinstance(runtime.object_store, S3CompatibleObjectStore)

    def test_remote_without_bucket_fails_fast(self) -> None:
        # C5: missing mandatory remote config fails fast (never falls back).
        for driver in ("s3", "r2"):
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setenv("DARKULA_OBJECT_STORE__DRIVER", driver)
                settings = load_settings(config_dir=_SHIPPED_CONFIG)
            with pytest.raises(UnavailableDriverError, match="bucket"):
                compose(settings=settings)

    def test_composition_makes_no_network_call(self) -> None:
        # C9: constructing the S3-compatible adapter performs no network I/O.
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_OBJECT_STORE__DRIVER", "s3")
            monkeypatch.setenv("DARKULA_OBJECT_STORE__BUCKET", "darkula-s3")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        store = compose(settings=settings).object_store
        assert isinstance(store, S3CompatibleObjectStore)
        # Client is only built on first use; constructing is offline.
        assert store._client_cache is None

    def test_langsmith_backend_fails_fast(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_AGENT_OBSERVABILITY__BACKEND", "langsmith")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError, match="LangSmith"):
            compose(settings=settings)

    def test_langfuse_backend_fails_fast(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_AGENT_OBSERVABILITY__BACKEND", "langfuse")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError, match="Langfuse"):
            compose(settings=settings)

    def test_local_driver_without_root_fails_fast(self) -> None:
        settings = Settings(
            datastream=DataStreamSettings(driver=DataStreamDriver.FAKE),
            object_store=ObjectStoreSettings(
                driver=ObjectStoreDriver.LOCAL, local_root=None
            ),
        )
        with pytest.raises(UnavailableDriverError, match="local_root"):
            compose(settings=settings)

    def test_never_silently_substitutes(self) -> None:
        # Explicit REDPANDA yields the production adapter, never the fake.
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_DATASTREAM__DRIVER", "redpanda")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert isinstance(runtime.data_stream, RedpandaDataStream)
        assert not isinstance(runtime.data_stream, FakeDataStream)


class TestLlmComposition:
    """PR 10: the OPENAI driver composes the production adapter (never a
    silent fake substitute) and the runtime exposes recon capabilities."""

    def test_openai_driver_composes_provider_adapter(self) -> None:
        from darkula.infrastructure.llm import OpenAiLlmClient

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_LLM__DRIVER", "openai")
            monkeypatch.setenv("DARKULA_LLM__PROVIDER__MODEL_NAME", "darkula-test")
            monkeypatch.setenv("DARKULA_LLM__PROVIDER__API_KEY", "sk-test")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert isinstance(runtime.llm, OpenAiLlmClient)
        assert not isinstance(runtime.llm, FakeLlmClient)

    def test_openai_without_model_or_key_fails_fast(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_LLM__DRIVER", "openai")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError, match="model_name"):
            compose(settings=settings)
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_LLM__DRIVER", "openai")
            monkeypatch.setenv("DARKULA_LLM__PROVIDER__MODEL_NAME", "darkula-test")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError, match="api_key"):
            compose(settings=settings)

    def test_fake_driver_composes_fake_never_openai(self) -> None:
        from darkula.infrastructure.llm import OpenAiLlmClient

        runtime = compose(settings=_shipped())
        assert isinstance(runtime.llm, FakeLlmClient)
        assert not isinstance(runtime.llm, OpenAiLlmClient)

    def test_runtime_exposes_recon_agent_and_coordinator(self) -> None:
        from darkula.app.recon import ReconCoordinator
        from darkula.app.recon_agent import ReconAgent

        runtime = compose(settings=_shipped())
        assert isinstance(runtime.recon_agent, ReconAgent)
        assert isinstance(runtime.recon_coordinator, ReconCoordinator)
        assert runtime.recon_agent.settings.max_turns == 4

    def test_runtime_exposes_deterministic_extraction_service(self) -> None:
        from darkula.app.extraction import DeterministicExtractionService

        runtime = compose(settings=_shipped())
        assert isinstance(runtime.extraction_service, DeterministicExtractionService)
        assert runtime.extraction_service._profile.profile_name == (
            "deterministic-observables"
        )
        assert runtime.extraction_service._profile.profile_version == "v1"

    def test_runtime_exposes_semantic_extraction_service(self) -> None:
        from darkula.app.semantic_extraction import SemanticExtractionService

        runtime = compose(settings=_shipped())
        assert isinstance(
            runtime.semantic_extraction_service, SemanticExtractionService
        )
        assert runtime.semantic_extraction_service.profile_name == "semantic-entities"
        assert runtime.semantic_extraction_service.profile_version == "v1"

    def test_semantic_extraction_disabled_is_not_composed(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_SEMANTIC_EXTRACTION__ENABLED", "false")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert runtime.semantic_extraction_service is None

    def test_runtime_exposes_relationship_extraction_service(self) -> None:
        from darkula.app.relationship_extraction import (
            RelationshipExtractionService,
        )

        runtime = compose(settings=_shipped())
        assert isinstance(
            runtime.relationship_extraction_service, RelationshipExtractionService
        )
        assert (
            runtime.relationship_extraction_service.profile_name
            == "relationship-assertions"
        )
        assert runtime.relationship_extraction_service.profile_version == "v1"
        # The service reuses the single composed LlmClient, never a new one.
        assert runtime.relationship_extraction_service._llm is runtime.llm

    def test_relationship_extraction_disabled_is_not_composed(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_RELATIONSHIP_EXTRACTION__ENABLED", "false")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert runtime.relationship_extraction_service is None

    def test_geography_is_disabled_by_default(self) -> None:
        runtime = compose(settings=_shipped())
        assert runtime.geography_service is None

    def test_geography_enabled_without_resolver_fails_fast(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_GEOGRAPHY__ENABLED", "true")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError):
            compose(settings=settings)

    def test_geography_enabled_with_explicit_fake_composes(self) -> None:
        from darkula.app.geography import GeographicResolutionService

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_GEOGRAPHY__ENABLED", "true")
            monkeypatch.setenv("DARKULA_GEOGRAPHY__RESOLVER", "fake")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert isinstance(runtime.geography_service, GeographicResolutionService)
