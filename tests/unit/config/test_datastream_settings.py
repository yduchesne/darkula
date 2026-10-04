# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for PR 5 DataStream (Redpanda) settings."""

from __future__ import annotations

from pydantic import ValidationError
from pytest import MonkeyPatch, raises

from darkula.config.settings import (
    DataStreamDriver,
    DataStreamSettings,
    Settings,
)


class TestDataStreamDefaults:
    """PR 5 default data-stream view."""

    def test_defaults_match_base_layer(self) -> None:
        settings = DataStreamSettings()
        assert settings.driver is DataStreamDriver.REDPANDA
        assert settings.bootstrap_servers == "darkula-redpanda:9092"
        assert settings.client_id == "darkula"
        assert settings.poll_timeout_ms == 500
        assert settings.max_poll_records == 100


class TestBoundedFields:
    """Bounded Redpanda settings fail fast at configuration time."""

    def test_blank_bootstrap_servers_rejected(self) -> None:
        with raises(ValidationError):
            DataStreamSettings(bootstrap_servers="   ")

    def test_blank_client_id_rejected(self) -> None:
        with raises(ValidationError):
            DataStreamSettings(client_id="")

    def test_zero_poll_timeout_rejected(self) -> None:
        with raises(ValidationError):
            DataStreamSettings(poll_timeout_ms=0)

    def test_zero_max_poll_records_rejected(self) -> None:
        with raises(ValidationError):
            DataStreamSettings(max_poll_records=0)

    def test_unknown_field_rejected(self) -> None:
        with raises(ValidationError):
            DataStreamSettings(security_protocol="PLAINTEXT")  # type: ignore[call-arg]


class TestEnvironment:
    """Environment remains the ultimate override for Redpanda settings."""

    def test_env_override(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("DARKULA_DATASTREAM__BOOTSTRAP_SERVERS", "broker:9092")
        settings = Settings()
        assert settings.datastream.bootstrap_servers == "broker:9092"

    def test_empty_env_has_no_effect(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("DARKULA_DATASTREAM__BOOTSTRAP_SERVERS", "")
        settings = Settings()
        assert settings.datastream.bootstrap_servers == "darkula-redpanda:9092"
