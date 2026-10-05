# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic Fake World scenario registry (PR 6).

Canonical scenarios are registered explicitly by ID/version — no filesystem
discovery, no ambient environment dependence. The registry returns shared
*immutable* scenario definitions; mutable runtime state (sessions, counters)
lives only on fresh ``FakeWorldRenderer`` instances.
"""

from __future__ import annotations

from darkula.testing.fake_world.identifiers import (
    ScenarioId,
    ScenarioVersion,
)
from darkula.testing.fake_world.model import FakeWorldScenario
from darkula.testing.fake_world.scenarios.blackgate_v1 import (
    build_blackgate_core_v1,
)
from darkula.testing.fake_world.scenarios.cross_source_v1 import (
    build_cross_source_v1,
)


class UnknownScenarioError(KeyError):
    """Deterministic failure for an unknown scenario ID/version.

    This is a test-only API contract: callers catch ``KeyError`` or this
    typed subclass; it is never rendered as a source response.
    """


#: Explicit canonical registry: (scenario_id, version) -> scenario.
_REGISTERED: dict[tuple[str, int], FakeWorldScenario] = {}
_REGISTERED[("blackgate-core", 1)] = build_blackgate_core_v1()
_REGISTERED[("darkula-cross-source", 1)] = build_cross_source_v1()


def get_scenario(
    scenario_id: str | ScenarioId, *, version: int = 1
) -> FakeWorldScenario:
    """Return the canonical scenario with the given ID/version.

    Raises :class:`UnknownScenarioError` for unknown IDs/versions and
    :class:`~darkula.testing.fake_world.identifiers.FakeWorldValidationError`
    for invalid version numbers.
    """
    ScenarioVersion(version)  # validates positivity deterministically
    key = (str(scenario_id), int(version))
    try:
        return _REGISTERED[key]
    except KeyError:
        raise UnknownScenarioError(
            f"unknown Fake World scenario {key!r}; registered scenarios are "
            f"{sorted(_REGISTERED)}"
        ) from None


def list_scenarios() -> tuple[FakeWorldScenario, ...]:
    """Return all registered canonical scenarios in registry order."""
    return tuple(_REGISTERED.values())


__all__ = [
    "UnknownScenarioError",
    "get_scenario",
    "list_scenarios",
]
