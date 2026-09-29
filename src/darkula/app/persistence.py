# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula persistence ownership boundary.

PR 2 freezes **transaction ownership**, not repositories and not schema.

- One :class:`UnitOfWork` represents one real future PostgreSQL transaction.
- Commit is explicit.
- An exceptional exit from the context manager rolls back unless the
  transaction was already safely resolved.
- External LLM/network/crawler work must not occur inside long-lived
  persistence transactions.
- Future repositories are accessed through this boundary rather than by
  constructing database clients directly (repository exposure is PR 4 work).

No SQLAlchemy ``Session``, no ``psycopg`` connection, and no domain
repository methods exist in PR 2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self


class PersistenceError(RuntimeError):
    """Bounded, provider-neutral persistence failure.

    Public messages never echo database provider exception text or stored
    content.
    """


class UnitOfWork(ABC):
    """One explicit persistence transaction boundary.

    Typical usage:

    .. code-block:: python

        async with uow_factory() as uow:
            ...  # repository work inside the transaction
            await uow.commit()

    The context manager exits on exception by rolling back; success requires
    an explicit :meth:`commit` and otherwise rolls back on exit.
    """

    @abstractmethod
    async def __aenter__(self) -> Self:
        """Open the persistence transaction and return this UnitOfWork."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back on an exceptional exit.

        Success does not commit: the caller must call :meth:`commit`
        explicitly after durable processing succeeds. An exceptional exit
        rolls back unless the transaction was already safely resolved.
        """
        if exc_type is not None:
            await self.rollback()

    @abstractmethod
    async def commit(self) -> None:
        """Persist the transaction explicitly and durably."""

    @abstractmethod
    async def rollback(self) -> None:
        """Discard the transaction explicitly (safe to call once)."""


class DarkulaSpi(ABC):
    """Darkula-owned persistence service boundary.

    Application code obtains :class:`UnitOfWork` instances only through this
    boundary; it never constructs database clients directly.
    """

    @abstractmethod
    def unit_of_work(self) -> UnitOfWork:
        """Return one fresh UnitOfWork representing one transaction."""


__all__ = ["DarkulaSpi", "PersistenceError", "UnitOfWork"]
