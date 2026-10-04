# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula persistence ownership boundary.

PR 2 freezes **transaction ownership**. PR 4 adds the bounded error subtypes
and repository exposure required by the source-domain persistence
foundation:

- One :class:`UnitOfWork` represents one real PostgreSQL transaction.
- Commit is explicit.
- An exceptional exit from the context manager rolls back unless the
  transaction was already safely resolved.
- External LLM/network/crawler work must not occur inside long-lived
  persistence transactions.
- Repository contracts (:class:`~darkula.app.repositories.SourceCandidateRepository`
  and :class:`~darkula.app.repositories.SourceRepository`) are exposed
  through :class:`UnitOfWork`; application/domain code never constructs
  database clients directly.

No SQLAlchemy ``Session`` and no ``psycopg`` connection appear here: this
module stays provider-neutral and imports repository contracts only for
annotation purposes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from darkula.app.repositories import (
        OutboxRepository,
        ProcessedMessageRepository,
        SourceCandidateRepository,
        SourceRepository,
    )


class PersistenceError(RuntimeError):
    """Bounded, provider-neutral persistence failure.

    Public messages never echo database provider exception text or stored
    content.
    """


class NotFoundError(PersistenceError):
    """A requested persisted record does not exist."""


class ConflictError(PersistenceError):
    """A controlled persistence conflict: duplicate identity or an invalid
    expected-state lifecycle transition. No data was changed."""


class IntegrityError(PersistenceError):
    """A database integrity violation (for example a foreign-key or check
    constraint). The message never echoes provider text or stored values."""


class PersistenceUnavailableError(PersistenceError):
    """The persistence backend cannot be reached or a connection cannot be
    obtained. Never contains DSNs, credentials, or raw driver messages."""


class MappingError(PersistenceError):
    """A persisted value cannot be mapped to a documented domain value
    (for example an unsupported enum label). Bounded and data-free."""


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

    @property
    @abstractmethod
    def source_candidates(self) -> SourceCandidateRepository:
        """Return the candidate repository bound to this transaction."""

    @property
    @abstractmethod
    def sources(self) -> SourceRepository:
        """Return the source repository bound to this transaction."""

    @property
    @abstractmethod
    def outbox(self) -> OutboxRepository:
        """Return the transactional outbox repository (PR 5)."""

    @property
    @abstractmethod
    def processed_messages(self) -> ProcessedMessageRepository:
        """Return the durable consumer-idempotency repository (PR 5)."""


class DarkulaSpi(ABC):
    """Darkula-owned persistence service boundary.

    Application code obtains :class:`UnitOfWork` instances only through this
    boundary; it never constructs database clients directly.
    """

    @abstractmethod
    def unit_of_work(self) -> UnitOfWork:
        """Return one fresh UnitOfWork representing one transaction."""


__all__ = [
    "ConflictError",
    "DarkulaSpi",
    "IntegrityError",
    "MappingError",
    "NotFoundError",
    "PersistenceError",
    "PersistenceUnavailableError",
    "UnitOfWork",
]
