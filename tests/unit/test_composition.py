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
from darkula.infrastructure.object_store import (
    InMemoryObjectStore,
    LocalFileObjectStore,
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

    def test_redpanda_datastream_fails_fast(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_DATASTREAM__DRIVER", "redpanda")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError, match="Redpanda"):
            compose(settings=settings)

    def test_production_profile_fails_fast_until_pr5_pr8(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_CONFIG_PROFILE", "production")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError, match="Redpanda"):
            compose(settings=settings)

    def test_s3_object_store_fails_fast(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_OBJECT_STORE__DRIVER", "s3")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError, match="S3"):
            compose(settings=settings)

    def test_r2_object_store_fails_fast(self) -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_OBJECT_STORE__DRIVER", "r2")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError, match="R2"):
            compose(settings=settings)

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
        # Explicit REDPANDA must fail even though a fake stream exists.
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DARKULA_DATASTREAM__DRIVER", "redpanda")
            settings = load_settings(config_dir=_SHIPPED_CONFIG)
        with pytest.raises(UnavailableDriverError):
            compose(settings=settings)
