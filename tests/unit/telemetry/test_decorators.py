# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the OTEL decorator contract (TEL-*)."""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Callable

import pytest

from darkula.telemetry.decorators import counted, timed, traced


@traced(span_name="test.sync_op")
def _sync_add(left: int, right: int) -> int:
    return left + right


@timed(metric="test.sync_op.duration")
def _sync_boom() -> None:
    raise RuntimeError("boom")


@timed(metric="test.async_op.duration")
async def _async_timed(value: str) -> str:
    return value


@timed(metric="test.async_fail.duration")
async def _async_timed_boom() -> None:
    raise ValueError("async boom")


@counted(metric="test.sync_op.calls")
def _sync_counted(value: int) -> int:
    return value * 2


@counted(metric="test.async_fail.calls")
async def _async_counted_boom() -> None:
    raise KeyError("async key boom")


@counted(metric="test.sync_op.calls")
async def _async_echo(value: str) -> str:
    return value


@traced(span_name="test.cancel")
async def _async_cancel() -> None:
    raise asyncio.CancelledError()


class TestSyncBehavior:
    """TEL-01: decorated sync functions preserve return values."""

    def test_sync_return_value_preserved(self) -> None:
        assert _sync_add(2, 3) == 5

    def test_ordinary_exception_propagates(self) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            _sync_boom()


class TestAsyncBehavior:
    """TEL-02/03/04: async return/exception/cancellation preservation."""

    @pytest.mark.asyncio
    async def test_async_return_value_preserved(self) -> None:
        assert await _async_echo("hello") == "hello"

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            await _async_cancel()

    @pytest.mark.asyncio
    async def test_timed_async_return_and_exception(self) -> None:
        assert await _async_timed("value") == "value"
        with pytest.raises(ValueError, match="async boom"):
            await _async_timed_boom()

    def test_counted_sync_return(self) -> None:
        assert _sync_counted(21) == 42

    @pytest.mark.asyncio
    async def test_counted_async_exception_propagates(self) -> None:
        with pytest.raises(KeyError, match="async key boom"):
            await _async_counted_boom()


class TestMetadata:
    """TEL-05: function metadata/name preserved via functools.wraps."""

    def test_docstring_and_name_preserved_sync(self) -> None:
        @traced(span_name="test.meta")
        def documented(value: int) -> int:
            """Original docstring."""
            return value * 2

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "Original docstring."

    @pytest.mark.asyncio
    async def test_docstring_and_name_preserved_async(self) -> None:
        @counted(metric="test.meta.calls")
        async def documented(value: int) -> int:
            """Original async docstring."""
            return value * 2

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "Original async docstring."
        assert await documented(3) == 6


class TestDecorateTimeValidation:
    """Static names are checked at decoration time (fail fast)."""

    @pytest.mark.parametrize(
        "build",
        [
            lambda: traced(span_name="  "),
            lambda: timed(metric="  "),
            lambda: counted(metric="  "),
        ],
    )
    def test_blank_static_name_rejected(self, build: Callable[[], object]) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            build()

    def test_non_string_attribute_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="strings"):
            traced(span_name="test.op", attributes={"count": 1})  # type: ignore[dict-item]


class TestNoClientDependency:
    """TEL-06: Prometheus/Jaeger/Loki client dependencies stay absent."""

    def test_decorator_module_has_no_vendor_client_imports(self) -> None:
        source = pathlib.Path(__file__).resolve().parents[3] / "src"
        text = (source / "darkula" / "telemetry" / "decorators.py").read_text()
        for forbidden in (
            "import prometheus_client",
            "import jaeger",
            "import loki",
            "from opentelemetry.sdk",
            "import opentelemetry.sdk",
        ):
            assert forbidden not in text, f"vendor client leaked: {forbidden}"

    def test_module_attributes_remain_clean(self) -> None:
        from darkula.telemetry import decorators

        assert not hasattr(decorators, "prometheus_client")
        assert not hasattr(decorators, "opentelemetry")
