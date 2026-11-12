# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the PR 18 registry-poll checkpoint value."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from darkula.domain.registry_polling import (
    MAX_REGISTRY_CURSOR_LENGTH,
    RegistryPollCheckpoint,
    validate_registry_cursor,
)

_MOMENT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


class TestCursorValidation:
    def test_accepts_exact_bounded_names(self) -> None:
        assert validate_registry_cursor("access bay") == "access bay"
        assert validate_registry_cursor("a.b-c_d") == "a.b-c_d"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            " padded",
            "trailing ",
            "tab\tname",
            "line\nbreak",
            "del\x7f",
            "x" * (MAX_REGISTRY_CURSOR_LENGTH + 1),
        ],
    )
    def test_rejects_unsafe_names(self, value: str) -> None:
        with pytest.raises(ValueError):
            validate_registry_cursor(value)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            validate_registry_cursor(1)  # type: ignore[arg-type]


class TestCheckpointValue:
    def test_unpersisted_value(self) -> None:
        checkpoint = RegistryPollCheckpoint.unpersisted("ransomlook")
        assert checkpoint.provider_id == "ransomlook"
        assert checkpoint.cursor is None
        assert checkpoint.generation == 0
        assert checkpoint.cooldown_until is None
        assert checkpoint.last_completed_at is None

    def test_normalizes_timestamps(self) -> None:
        checkpoint = RegistryPollCheckpoint(
            provider_id="ransomlook",
            cursor="alpha",
            generation=3,
            cooldown_until=datetime(2026, 11, 11, 12, 0, tzinfo=UTC).astimezone(UTC),
            last_completed_at=_MOMENT,
        )
        assert checkpoint.cooldown_until == _MOMENT

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"provider_id": "UPPER"},
            {"provider_id": ""},
            {"cursor": ""},
            {"cursor": " lead"},
            {"generation": -1},
            {"cooldown_until": datetime(2026, 1, 1)},
            {"last_completed_at": datetime(2026, 1, 1)},
        ],
    )
    def test_rejects_invalid_values(self, kwargs: dict[str, object]) -> None:
        base: dict[str, object] = {
            "provider_id": "ransomlook",
            "cursor": None,
            "generation": 0,
        }
        base.update(kwargs)
        with pytest.raises(ValueError):
            RegistryPollCheckpoint(**base)  # type: ignore[arg-type]

    def test_rejects_boolean_generation(self) -> None:
        with pytest.raises(ValueError):
            RegistryPollCheckpoint(
                provider_id="ransomlook", cursor=None, generation=True
            )
