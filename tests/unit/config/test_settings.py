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


class TestQualificationSettings:
    """PR 19: default-deny registry trust with fail-closed limits."""

    def test_trust_is_disabled_by_default(self) -> None:
        settings = Settings()
        assert settings.qualification.ransomlook_location_trusted is False
        assert settings.qualification.max_admission_batch == 25

    def test_frozen(self) -> None:
        settings = Settings()
        with pytest.raises(ValidationError):
            settings.qualification.ransomlook_location_trusted = True

    @pytest.mark.parametrize("limit", [0, 51, -1])
    def test_invalid_batch_limit_rejected(self, limit: int) -> None:
        with pytest.raises(ValidationError):
            Settings(qualification={"max_admission_batch": limit})  # type: ignore[arg-type]

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                qualification={  # type: ignore[arg-type]
                    "ransomlook_location_trusted": True,
                    "wildcard": "*",
                }
            )

    def test_environment_override(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("DARKULA_QUALIFICATION__RANSOMLOOK_LOCATION_TRUSTED", "true")
        assert Settings().qualification.ransomlook_location_trusted is True


class TestPublicHttpsSettings:
    """PR 20 U20-12: default-off exact live configuration."""

    def test_u20_12_defaults_are_off_and_empty(self) -> None:
        from darkula.config.settings import CrawlerSettings

        crawler = CrawlerSettings()
        assert crawler.real_https_enabled is False
        assert crawler.real_https_allowed_origins == ()
        assert crawler.real_https_egress_network == "darkula-live-egress"
        assert crawler.real_https_gateway_image == "localhost/darkula-https-egress:1"
        assert crawler.real_https_max_pages == 20
        assert crawler.real_https_max_requests == 60
        assert crawler.real_https_max_depth == 2
        assert crawler.real_https_max_timeout_seconds == 90.0

    def test_u20_12_nonempty_environment_enables_exact_allowlist(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_CRAWLER__REAL_HTTPS_ENABLED", "true")
        monkeypatch.setenv(
            "DARKULA_CRAWLER__REAL_HTTPS_ALLOWED_ORIGINS",
            '["https://approved.example.org:443"]',
        )
        settings = Settings()
        assert settings.crawler.real_https_enabled is True
        assert settings.crawler.real_https_allowed_origins == (
            "https://approved.example.org:443",
        )

    def test_u20_12_empty_environment_cannot_enable(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_CRAWLER__REAL_HTTPS_ENABLED", "")
        monkeypatch.setenv("DARKULA_CRAWLER__REAL_HTTPS_ALLOWED_ORIGINS", "")
        settings = Settings()
        assert settings.crawler.real_https_enabled is False
        assert settings.crawler.real_https_allowed_origins == ()

    @pytest.mark.parametrize(
        "origins",
        [
            ("approved.example.org",),
            ("http://approved.example.org:443",),
            ("https://approved.example.org",),
            ("https://approved.example.org:8443",),
            ("https://approved.example.org:443/path",),
            ("https://*.example.org:443",),
            (
                "https://approved.example.org:443",
                "https://approved.example.org:443",
            ),
        ],
    )
    def test_u20_12_noncanonical_allowlist_rejected(
        self, origins: tuple[str, ...]
    ) -> None:
        from darkula.config.settings import CrawlerSettings

        with pytest.raises(ValidationError):
            CrawlerSettings(real_https_allowed_origins=origins)

    def test_u20_12_hard_live_limits_enforced(self) -> None:
        from darkula.config.settings import CrawlerSettings

        for field, value in (
            ("real_https_max_pages", 21),
            ("real_https_max_requests", 61),
            ("real_https_max_depth", 3),
            ("real_https_max_timeout_seconds", 90.1),
            ("real_https_max_timeout_seconds", 0.5),
        ):
            with pytest.raises(ValidationError):
                CrawlerSettings(**{field: value})  # type: ignore[arg-type]
