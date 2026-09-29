# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the configuration skeleton (CFG-*)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from darkula.config.settings import (
    CONFIG_PROFILE_ENV_VAR,
    AgentObservabilityBackend,
    DataStreamDriver,
    ObjectStoreDriver,
    Settings,
)


class TestFailClosed:
    """CFG-01: unknown fields are rejected."""

    def test_unknown_top_level_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(unknown_setting="value")  # type: ignore[call-arg]

    def test_unknown_nested_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                database={"host": "localhost", "unknown": "value"}  # type: ignore[arg-type]
            )

    def test_unknown_nested_field_on_future_groups_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(crawler={"scheduler": "cron"})  # type: ignore[arg-type]


class TestDefaults:
    """CFG-05: importing settings has no profile-loading side effect."""

    def test_defaults_construct_without_environment(self) -> None:
        settings = Settings()
        assert settings.config_profile is None
        assert settings.datastream.driver is DataStreamDriver.REDPANDA
        assert settings.object_store.driver is ObjectStoreDriver.LOCAL
        assert settings.agent_observability.backend is AgentObservabilityBackend.NONE
        assert settings.telemetry.enabled is True

    def test_modules_frozen(self) -> None:
        settings = Settings()
        with pytest.raises(ValidationError):
            settings.database.host = "other"


class TestEnvironment:
    """CFG-02/CFG-03: env prefix, nested delimiter, empty values ignored."""

    def test_env_prefix_and_nested_override(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("DARKULA_OBJECT_STORE__DRIVER", "s3")
        settings = Settings()
        assert settings.object_store.driver is ObjectStoreDriver.S3

    def test_empty_environment_value_has_no_effect(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_OBJECT_STORE__DRIVER", "")
        settings = Settings()
        assert settings.object_store.driver is ObjectStoreDriver.LOCAL

    def test_empty_config_profile_value_has_no_effect(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CONFIG_PROFILE_ENV_VAR, "")
        settings = Settings()
        assert settings.config_profile is None

    def test_non_empty_config_profile_is_observable(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CONFIG_PROFILE_ENV_VAR, "test")
        settings = Settings()
        assert settings.config_profile == "test"

    def test_environment_state_restored(self, monkeypatch: MonkeyPatch) -> None:
        # Empty-string assignment is ignored and the previous variable state
        # is restored by pytest's monkeypatch teardown (order-independent).
        monkeypatch.setenv("DARKULA_LLM__TIMEOUT_SECONDS", "")
        assert Settings().llm.timeout_seconds is None


class TestDriverEnumValidation:
    """CFG-04: invalid driver enums are rejected."""

    @pytest.mark.parametrize(
        "group, driver",
        [
            ("datastream", "kafka"),
            ("object_store", "gcs"),
            ("agent_observability", "wandb"),
        ],
    )
    def test_invalid_driver_rejected(self, group: str, driver: str) -> None:
        kwargs: dict[str, object] = {group: {"driver": driver}}
        with pytest.raises(ValidationError):
            Settings(**kwargs)  # type: ignore[arg-type]


class TestProfileDeferred:
    """CFG-06: profile resolution is explicitly not implemented in PR 2."""

    def test_profile_selector_concept_frozen(self) -> None:
        # The selector name is frozen; PR 3 owns loading/merging.
        assert CONFIG_PROFILE_ENV_VAR == "DARKULA_CONFIG_PROFILE"
        settings = Settings()
        assert settings.config_profile is None

    def test_no_profile_loading_machinery_exported(self) -> None:
        import darkula.config.settings as settings_module

        for unimplemented in ("load_profiles", "resolve_profile", "merge_config"):
            assert not hasattr(settings_module, unimplemented), (
                f"{unimplemented} must remain PR 3 work"
            )
