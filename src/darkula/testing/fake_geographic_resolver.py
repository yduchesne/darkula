# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic FakeGeographicResolver test double (PR 12).

Represents the **external geographic-resolution boundary** only. It is never
composed as a production fallback; tests select/inject it explicitly.

Semantics mirror :class:`~darkula.testing.fake_llm.FakeLlmClient`:

- scripted FIFO outcomes (``GeographicResolverResult`` or any exception);
- exact bounded call recording;
- optional response factory and default;
- optional expected-request assertion;
- fail closed with ``AssertionError`` when unscripted;
- cancellation can be scripted and propagates unchanged.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from darkula.app.geography import GeographicResolver
from darkula.domain.geography import (
    GeographicResolutionRequest,
    GeographicResolverIdentity,
    GeographicResolverResult,
)

#: One scripted outcome: a provider-neutral result or any exception.
Outcome = GeographicResolverResult | BaseException


@dataclass(frozen=True, slots=True)
class FakeGeographicResolverCall:
    """Exact bounded request of one fake resolver call."""

    request: GeographicResolutionRequest


class FakeGeographicResolver(GeographicResolver):
    """Deterministic, offline, inspectable GeographicResolver test double."""

    def __init__(
        self,
        *,
        identity: GeographicResolverIdentity | None = None,
        default_result: GeographicResolverResult | None = None,
        factory: Callable[[FakeGeographicResolverCall], GeographicResolverResult]
        | None = None,
        expected_request: GeographicResolutionRequest | None = None,
    ) -> None:
        self._identity = identity or GeographicResolverIdentity(
            name="fake-geo", version="v1"
        )
        self._queue: deque[Outcome] = deque()
        self._calls: list[FakeGeographicResolverCall] = []
        self._default_result = default_result
        self._factory = factory
        self._expected_request = expected_request

    @property
    def identity(self) -> GeographicResolverIdentity:
        """Return the configured bounded resolver identity."""
        return self._identity

    def enqueue(self, outcome: Outcome) -> None:
        """Append one scripted result or exception to the FIFO queue."""
        self._queue.append(outcome)

    def reset(self) -> None:
        """Clear the scripted queue and recorded calls."""
        self._queue.clear()
        self._calls.clear()

    @property
    def calls(self) -> tuple[FakeGeographicResolverCall, ...]:
        """Return every recorded call in invocation order."""
        return tuple(self._calls)

    @property
    def call_count(self) -> int:
        """Return the number of recorded calls."""
        return len(self._calls)

    async def resolve(
        self, request: GeographicResolutionRequest
    ) -> GeographicResolverResult:
        """Return one scripted provider-neutral result or raise."""
        call = FakeGeographicResolverCall(request=request)
        self._calls.append(call)
        if self._expected_request is not None and request != self._expected_request:
            raise AssertionError(
                "FakeGeographicResolver received an unexpected request"
            )
        if self._queue:
            outcome = self._queue.popleft()
        elif self._factory is not None:
            outcome = self._factory(call)
        elif self._default_result is not None:
            outcome = self._default_result
        else:
            raise AssertionError("FakeGeographicResolver has no scripted outcome")
        if isinstance(outcome, BaseException):
            raise outcome
        if not isinstance(outcome, GeographicResolverResult):
            raise AssertionError(
                "FakeGeographicResolver only supports provider-neutral results"
            )
        return outcome


__all__ = ["FakeGeographicResolver", "FakeGeographicResolverCall", "Outcome"]
