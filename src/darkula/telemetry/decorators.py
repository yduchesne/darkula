# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula OTEL decorator contract (PR 2 skeleton).

PR 1 selects OpenTelemetry as the operational telemetry standard and prefers
Darkula-owned decorators around method/function boundaries that match a
telemetry operation:

- ``traced``  -> one span for a stabilized operation boundary;
- ``timed``   -> one duration measurement for a stabilized operation;
- ``counted`` -> one invocation counter for a stabilized operation.

PR 2 freezes these public names/signatures and their preservation semantics.
The decorators in this module are **contract-only no-ops**: they validate
their static names/attributes at decoration time (fail fast on developer
errors) and otherwise preserve behavior exactly. They do **not** emit
telemetry and must not be mistaken for working instrumentation. PR 3 owns the
testable OTEL-API-backed instrumentation primitives (tracer/meter resolution,
exporters, span/measurement emission).

Preserved semantics (and therefore the tests in :mod:`tests.unit.telemetry`):

- sync return value;
- async return value;
- ordinary exception propagation unchanged;
- ``asyncio.CancelledError`` propagation unchanged;
- function metadata preserved via :func:`functools.wraps`.

These decorators are a thin OTEL convenience, never a second observable
framework and never an abstraction that makes OTEL replaceable.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def _validate_static_name(value: str, *, kind: str) -> str:
    """Return a trimmed non-blank static name or raise ``ValueError``."""
    if not value or not value.strip():
        raise ValueError(f"{kind} must not be blank")
    name = value.strip()
    if len(name) > 200:
        raise ValueError(f"{kind} must not exceed 200 characters")
    if any(ord(char) < 32 for char in name):
        raise ValueError(f"{kind} must not contain control characters")
    return name


def _validate_static_attributes(
    attributes: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    """Validate bounded string static attributes and return them frozen.

    Full attribute-validation policy (deep bounded checks, secret
    allowlisting) is PR 3 work; PR 2 only rejects non-string values and
    blank/overlong keys so developer mistakes fail at decoration time.
    """
    if not attributes:
        return ()
    for key, value in attributes.items():
        _validate_static_name(key, kind="attribute name")
        if not isinstance(value, str):
            raise ValueError("static attributes must map to strings")
    return tuple(sorted(attributes.items()))


def traced(
    *,
    span_name: str,
    attributes: Mapping[str, str] | None = None,
) -> Callable[[F], F]:
    """Decorate a sync/async callable as one stabilized traced operation.

    PR 2 contract-only no-op: validates ``span_name``/``attributes`` at
    decoration time and otherwise preserves the wrapped callable unchanged.
    PR 3 will emit one OTEL span per invocation.
    """
    name = _validate_static_name(span_name, kind="span_name")
    _validate_static_attributes(attributes)
    del name  # retained for the PR 3 implementation; no emission yet

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await func(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return cast(F, sync_wrapper)

    return decorator


def timed(
    *,
    metric: str,
    attributes: Mapping[str, str] | None = None,
) -> Callable[[F], F]:
    """Decorate a sync/async callable as one stabilized timed operation.

    PR 2 contract-only no-op: validates the metric name/attributes at
    decoration time and otherwise preserves the wrapped callable unchanged.
    PR 3 will record one duration measurement per invocation.
    """
    _validate_static_name(metric, kind="metric")
    _validate_static_attributes(attributes)

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await func(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return cast(F, sync_wrapper)

    return decorator


def counted(
    *,
    metric: str,
    attributes: Mapping[str, str] | None = None,
) -> Callable[[F], F]:
    """Decorate a sync/async callable as one stabilized counted operation.

    PR 2 contract-only no-op: validates the metric name/attributes at
    decoration time and otherwise preserves the wrapped callable unchanged.
    PR 3 will increment one counter per invocation.
    """
    _validate_static_name(metric, kind="metric")
    _validate_static_attributes(attributes)

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await func(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return cast(F, sync_wrapper)

    return decorator


__all__ = ["counted", "timed", "traced"]
