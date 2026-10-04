# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical Fake World scenario definitions (PR 6).

Scenario modules define immutable world state and traceability; they never
encode expected Darkula answers. The registry references the builders here
explicitly — no module auto-discovery.
"""

from darkula.testing.fake_world.scenarios.blackgate_v1 import (
    build_blackgate_core_v1,
)

__all__ = ["build_blackgate_core_v1"]
