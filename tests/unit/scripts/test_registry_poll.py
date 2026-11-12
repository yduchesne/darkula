# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for PR 18 registry configuration and one-shot CLI gates."""

from __future__ import annotations

import asyncio
from typing import Any

import darkula_registry_poll as cli  # type: ignore[import-not-found]
import pytest
from pydantic import ValidationError

from darkula.app.persistence import ConflictError
from darkula.app.registry_polling import RegistryPollOutcome
from darkula.config.settings import RegistrySettings, Settings


class TestRegistrySettings:
    """U35: default-off, bounded, fail-closed configuration."""

    def test_defaults_are_disabled_and_bounded(self) -> None:
        settings = Settings()
        assert settings.registry.ransomlook_enabled is False
        assert settings.registry.ransomlook_live_enabled is False
        assert settings.registry.max_entries_per_run == 10
        assert settings.registry.max_locations_per_group == 64
        assert settings.registry.max_index_entries == 2000
        assert settings.registry.max_response_bytes == 8388608
        assert settings.registry.request_timeout_seconds == 8.0
        assert settings.registry.request_interval_seconds == 2.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_entries_per_run": 0},
            {"max_entries_per_run": 11},
            {"max_locations_per_group": 0},
            {"max_locations_per_group": 65},
            {"max_index_entries": 0},
            {"max_index_entries": 2001},
            {"max_response_bytes": 0},
            {"max_response_bytes": 8_388_609},
            {"request_timeout_seconds": 0.5},
            {"request_timeout_seconds": 9.0},
            {"request_interval_seconds": 1.0},
        ],
    )
    def test_rejects_out_of_bounds_values(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            RegistrySettings(**kwargs)

    def test_unknown_keys_fail_closed(self) -> None:
        with pytest.raises(ValidationError):
            RegistrySettings(unknown_setting=True)  # type: ignore[call-arg]


class TestCliGates:
    """U35/U36: live selection is explicitly triple-gated."""

    def test_help_is_offline_and_successful(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as error:
            cli.main(["--help"])
        assert error.value.code == 0
        assert "registry" in capsys.readouterr().out.lower()

    def test_missing_live_flag_exits_two(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--provider", "ransomlook"]) == 2
        assert "--live" in capsys.readouterr().err

    def test_unknown_provider_is_rejected(self) -> None:
        with pytest.raises(SystemExit) as error:
            cli.main(["--provider", "other", "--live"])
        assert error.value.code == 2

    def test_live_without_enable_flags_exits_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "load_settings", lambda: Settings())
        assert cli.main(["--provider", "ransomlook", "--live"]) == 2

    def test_partial_enable_gate_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings = Settings(registry=RegistrySettings(ransomlook_enabled=True))
        monkeypatch.setattr(cli, "load_settings", lambda: settings)
        assert cli.main(["--provider", "ransomlook", "--live"]) == 2
        assert "disabled" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (RegistryPollOutcome.COMPLETED, 0),
            (RegistryPollOutcome.COOLDOWN, 0),
            (RegistryPollOutcome.TEMPORARY_ERROR, 3),
            (RegistryPollOutcome.PERMANENT_ERROR, 2),
            (RegistryPollOutcome.CHECKPOINT_RACE, 4),
        ],
    )
    def test_outcome_exit_codes(
        self, outcome: RegistryPollOutcome, expected: int
    ) -> None:
        assert cli._exit_for_outcome(outcome) == expected

    def test_run_with_disabled_flags_makes_no_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "load_settings", lambda: Settings())

        def _forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("no transport may be constructed while disabled")

        monkeypatch.setattr(cli, "StdlibRegistryHttpTransport", _forbidden)
        assert asyncio.run(cli._run("ransomlook")) == 2


class TestConflictExitMapping:
    """T20: provider-data permanent vs immutable persisted conflict."""

    def test_immutable_conflict_still_exits_four(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def raising(provider: str) -> int:
            del provider
            raise ConflictError("immutable conflict")

        monkeypatch.setattr(cli, "_run", raising)
        assert cli.main(["--provider", "ransomlook", "--live"]) == 4

    def test_permanent_outcome_maps_to_disabled_exit(self) -> None:
        assert cli._exit_for_outcome(RegistryPollOutcome.PERMANENT_ERROR) == 2
