# SPDX-License-Identifier: AGPL-3.0-only
"""Narrow transaction-execution protocol shared by PostgreSQL repositories.

Repository implementations depend only on this small structural protocol:
they get a live driver connection on demand and a bounded closed-guard.
Deterministic unit tests satisfy the same protocol with fakes, so the
repositories' parameter marshalling, function invocation, and result
mapping are exercised offline while integration tests prove the real
stored functions.
"""

from __future__ import annotations

from typing import Any, Protocol

from psycopg import AsyncConnection


class ExecutionScope(Protocol):
    """The transaction view PostgreSQL repositories depend on."""

    def ensure_open(self) -> None:
        """Raise a bounded error when the transaction is closed."""

    def connection(self) -> AsyncConnection[Any]:
        """Return the live transaction connection (driver-owned)."""


__all__ = ["ExecutionScope"]
