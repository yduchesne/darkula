# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded PostgreSQL driver-error mapping (PR 4).

Production repository code never leaks raw driver messages. Every driver
failure is mapped onto a darkula.persistence error subtype with a bounded,
data-free message; ``asyncio.CancelledError`` always propagates unchanged.

Mapping rules:

- unique-violation (SQLSTATE 23505) -> :class:`ConflictError` (duplicate
  identity; never overwrite);
- foreign-key (23503), not-null (23502), check (23514) -> :class:`IntegrityError`;
- connect/interface/pool failures -> :class:`PersistenceUnavailableError`;
- anything else -> :class:`PersistenceError` (bounded, generic).

The mapping is projection-ed on the caller side: repositories call
:func:`map_driver_error` inside ``except`` blocks that do **not** catch
:class:`BaseException`, so cancellation is never seen here.
"""

from __future__ import annotations

import psycopg
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from psycopg_pool import PoolClosed, PoolTimeout

from darkula.app.persistence import (
    ConflictError,
    IntegrityError,
    PersistenceError,
    PersistenceUnavailableError,
)

#: Bounded, data-free messages; never include driver text, DSNs, or values.
_DUPLICATE_IDENTITY = "a record with this identity already exists"
_MISSING_REFERENCE = "a referenced record does not exist"
_NOT_AVAILABLE = "the persistence backend is unavailable"
_GENERIC = "the persistence operation failed"

#: ``psycopg`` pool exceptions raised before/without a connection.
_UNAVAILABLE_DRIVER_ERRORS: tuple[type[Exception], ...] = (
    PoolClosed,
    PoolTimeout,
)


def map_driver_error(exc: BaseException) -> PersistenceError:
    """Map one driver exception to a bounded persistence error.

    Raises ``asyncio.CancelledError`` unchanged (never swallows it).
    """
    # psycopg surfaces unique violations as IntegrityError subclasses; check
    # the narrowest classes first, cancellation last as an explicit guard.
    if isinstance(exc, psycopg.Error):
        if isinstance(exc, UniqueViolation):
            return ConflictError(_DUPLICATE_IDENTITY)
        if isinstance(exc, ForeignKeyViolation):
            return IntegrityError(_MISSING_REFERENCE)
        if isinstance(exc, psycopg.IntegrityError):
            return IntegrityError(_MISSING_REFERENCE)
    if isinstance(exc, _UNAVAILABLE_DRIVER_ERRORS):
        return PersistenceUnavailableError(_NOT_AVAILABLE)
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return PersistenceUnavailableError(_NOT_AVAILABLE)
    return PersistenceError(_GENERIC)


__all__ = ["map_driver_error"]
