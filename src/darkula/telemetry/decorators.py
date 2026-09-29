# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula OTEL decorators (PR 2 contract, PR 3 SDK emission).

``traced`` emits one span per stabilized operation boundary; ``timed`` emits
one seconds histogram point; ``counted`` increments one invocation counter
exactly once at operation entry. They are a thin convenience over the
OpenTelemetry **API** only (the SDK lives in :mod:`darkula.telemetry.setup`
and tests): no SDK import appears in this module.

Preserved semantics (PR 2 contract, unchanged):

- sync and async return values;
- ordinary exceptions propagate unchanged (recorded on the span);
- ``asyncio.CancelledError`` propagates unchanged and is never an ordinary
  error (cancellation records no duration point);
- function metadata is preserved via :func:`functools.wraps`.

Security/telemetry rules (PR 3):

- **never** capture function arguments or return values;
- only developer-controlled static attributes are attached, validated by
  :func:`~darkula.telemetry.attributes.validate_bounded_attributes`
  (bounded, control-free, secret-free) at decoration time;
- duration metric names enforce the canonical ``<operation>.duration``
  suffix and the seconds unit;
- disabled/unconfigured OTEL falls back to the API no-op proxy: the
  decorators remain behavior-preserving no-ops.

Tracer/meter resolution goes through the injectable module seams
``get_tracer``/``get_histogram``/``get_counter`` so deterministic tests can
bind in-memory providers without touching global OpenTelemetry state.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from darkula.telemetry.attributes import (
    ERROR_OUTCOME,
    OUTCOME,
    SUCCESS_OUTCOME,
    validate_bounded_attributes,
)
from darkula.telemetry.metrics import DURATION_UNIT, get_counter, get_histogram
from darkula.telemetry.tracing import get_tracer

F = TypeVar("F", bound=Callable[..., Any])


def _validate_static_name(value: str, *, kind: str) -> str:
    """Return a trimmed non-blank bounded static name or raise ``ValueError``."""
    if not value or not value.strip():
        raise ValueError(f"{kind} must not be blank")
    name = value.strip()
    if len(name) > 200:
        raise ValueError(f"{kind} must not exceed 200 characters")
    if any(ord(char) < 32 for char in name):
        raise ValueError(f"{kind} must not contain control characters")
    return name


def _validate_duration_metric(metric: str) -> str:
    """Require the canonical ``.duration`` suffix for duration metrics."""
    name = _validate_static_name(metric, kind="metric")
    if not name.endswith(".duration"):
        raise ValueError(
            "duration metric must use the canonical '<operation>.duration' suffix"
        )
    return name


def _record_exception(span: trace.Span, exc: Exception) -> None:
    """Record an ordinary exception on a span with OTel standard semantics.

    The exception event uses OTel's standard ``record_exception`` behavior;
    Darkula therefore applies these decorators only at boundaries whose
    exceptions are already safe and bounded.
    """
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR))


def traced(
    *,
    span_name: str,
    attributes: Mapping[str, str] | None = None,
) -> Callable[[F], F]:
    """Decorate a sync/async callable as one stabilized traced operation.

    Emits exactly one span per invocation with only the validated static
    attributes. Return values, ordinary exceptions, and cancellation are
    preserved unchanged; arguments and results are never attached.
    """
    name = _validate_static_name(span_name, kind="span_name")
    static_attributes = validate_bounded_attributes(attributes)

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                tracer = get_tracer()
                with tracer.start_as_current_span(
                    name, attributes=static_attributes
                ) as span:
                    try:
                        return await func(*args, **kwargs)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        _record_exception(span, exc)
                        raise

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            with tracer.start_as_current_span(
                name, attributes=static_attributes
            ) as span:
                try:
                    return func(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _record_exception(span, exc)
                    raise

        return cast(F, sync_wrapper)

    return decorator


def _outcome_attributes(
    outcome: str,
    static_attributes: dict[str, str],
) -> dict[str, str]:
    """Merge the bounded outcome attribute with the static attributes."""
    merged = dict(static_attributes)
    merged[OUTCOME] = outcome
    return merged


def timed(
    *,
    metric: str,
    attributes: Mapping[str, str] | None = None,
) -> Callable[[F], F]:
    """Decorate a sync/async callable as one stabilized timed operation.

    Measures one ``time.perf_counter()`` span and records one seconds
    histogram point tagged ``darkula.outcome`` = ``success`` or ``error``.
    Cancellation records no point and propagates unchanged; the same
    ordinary exception propagates after recording its error point.
    """
    name = _validate_duration_metric(metric)
    static_attributes = validate_bounded_attributes(attributes)

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                histogram = get_histogram(name, unit=DURATION_UNIT, description=name)
                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    histogram.record(
                        time.perf_counter() - start,
                        attributes=_outcome_attributes(
                            ERROR_OUTCOME, static_attributes
                        ),
                    )
                    raise
                else:
                    histogram.record(
                        time.perf_counter() - start,
                        attributes=_outcome_attributes(
                            SUCCESS_OUTCOME, static_attributes
                        ),
                    )
                    return result

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            histogram = get_histogram(name, unit=DURATION_UNIT, description=name)
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:
                histogram.record(
                    time.perf_counter() - start,
                    attributes=_outcome_attributes(ERROR_OUTCOME, static_attributes),
                )
                raise
            else:
                histogram.record(
                    time.perf_counter() - start,
                    attributes=_outcome_attributes(SUCCESS_OUTCOME, static_attributes),
                )
                return result

        return cast(F, sync_wrapper)

    return decorator


def counted(
    *,
    metric: str,
    attributes: Mapping[str, str] | None = None,
) -> Callable[[F], F]:
    """Decorate a sync/async callable as one stabilized counted operation.

    Increments the counter exactly once **at operation entry**, so success,
    ordinary failure, and cancellation all count as one invocation.
    """
    name = _validate_static_name(metric, kind="metric")
    static_attributes = validate_bounded_attributes(attributes)

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                get_counter(name).add(1, static_attributes)
                return await func(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            get_counter(name).add(1, static_attributes)
            return func(*args, **kwargs)

        return cast(F, sync_wrapper)

    return decorator


__all__ = ["counted", "timed", "traced"]
