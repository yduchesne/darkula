# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for PR 4 database settings (connection/pool fields)."""

from __future__ import annotations

from pydantic import ValidationError
from pytest import MonkeyPatch, raises

from darkula.config.settings import DatabaseDriver, DatabaseSettings


class TestDatabaseDefaults:
    """PR 4 default development database view."""

    def test_defaults_match_development_profile(self) -> None:
        settings = DatabaseSettings()
        assert settings.driver is DatabaseDriver.POSTGRESQL
        assert settings.host == "localhost"
        assert settings.port == 35432
        assert settings.name == "darkula"
        assert settings.user == "darkula"
        assert settings.password is None
        assert settings.pool_min_size == 1
        assert settings.pool_max_size == 4

    def test_conninfo_contains_connection_fields(self) -> None:
        settings = DatabaseSettings()
        conninfo = settings.conninfo()
        assert "host=localhost" in conninfo
        assert "port=35432" in conninfo
        assert "dbname=darkula" in conninfo
        assert "user=darkula" in conninfo
        assert "password=" not in conninfo

    def test_conninfo_includes_password_when_configured(self) -> None:
        settings = DatabaseSettings(password="pw")
        assert "password=pw" in settings.conninfo()


class TestPoolValidation:
    """Invalid pool bounds fail fast at configuration time."""

    def test_zero_min_pool_rejected(self) -> None:
        with raises(ValidationError):
            DatabaseSettings(pool_min_size=0)

    def test_max_below_min_rejected(self) -> None:
        with raises(ValidationError):
            DatabaseSettings(pool_min_size=4, pool_max_size=2)

    def test_non_positive_timeout_rejected(self) -> None:
        with raises(ValidationError):
            DatabaseSettings(connect_timeout_seconds=0)


class TestEnvironment:
    """Non-empty env overrides are the ultimate override for DB fields."""

    def test_env_override_fields(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("DARKULA_DATABASE__HOST", "pg.internal")
        monkeypatch.setenv("DARKULA_DATABASE__PORT", "5432")
        monkeypatch.setenv("DARKULA_DATABASE__USER", "svc")
        from darkula.config.settings import Settings

        settings = Settings()
        assert settings.database.host == "pg.internal"
        assert settings.database.port == 5432
        assert settings.database.user == "svc"

    def test_empty_env_value_is_noop(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("DARKULA_DATABASE__PASSWORD", "")
        settings = DatabaseSettings(password="from-profile")
        assert settings.password == "from-profile"

    def test_unknown_database_field_rejected(self) -> None:
        with raises(ValidationError):
            DatabaseSettings(ssl_cert="/tmp/x")  # type: ignore[call-arg]
