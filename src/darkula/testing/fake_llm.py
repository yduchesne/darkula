# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical deterministic FakeLlmClient (PR 3).

Implements the existing :class:`~darkula.app.llm.LlmClient` contract through
the private ``_generate_structured`` hook only: the public
``generate_structured`` validation from PR 2 is never bypassed.

Semantics:

- scripted outcomes are consumed FIFO and may be Pydantic response models or
  exceptions (any :class:`BaseException`, so ``asyncio.CancelledError`` can
  be scripted and propagates unchanged);
- every call is recorded exactly as a frozen :class:`FakeLlmCall`;
- an optional fixed default response and an optional response factory are
  supported;
- outcome precedence is ``queue -> factory -> default -> assertion failure``;
- an optional ``expected_response_model`` assertion fails closed on mismatch;
- a resolved response that is not an instance of the requested model is
  rejected as a typed ``LlmError`` (``INVALID_STRUCTURED_OUTPUT``);
- with no outcome available the client fails closed with ``AssertionError``
  so an unscripted test double is never silently accepted.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from darkula.app.llm import (
    LlmClient,
    LlmError,
    LlmErrorCode,
    ResponseT,
)

#: Type of one scripted outcome: a response model or any exception.
Outcome = BaseModel | BaseException


@dataclass(frozen=True, slots=True)
class FakeLlmCall:
    """Exact recorded inputs of one fake LLM call.

    ``response_model`` is the exact model class the caller requested, so
    tests can assert requested-type verification without duplicating it.
    Prompts are recorded verbatim for exact-call assertions.
    """

    system_prompt: str
    user_prompt: str
    response_model: type[BaseModel]
    operation_name: str


class FakeLlmClient(LlmClient):
    """Deterministic, offline, inspectable LlmClient test double."""

    def __init__(
        self,
        *,
        default_response: BaseModel | None = None,
        response_factory: Callable[[FakeLlmCall], BaseModel] | None = None,
        expected_response_model: type[BaseModel] | None = None,
    ) -> None:
        """Configure the fake.

        ``default_response`` is returned for every call with no queue or
        factory outcome (the same instance is reused). ``response_factory``
        receives the exact recorded :class:`FakeLlmCall` and returns the
        response. ``expected_response_model`` asserts every call requests
        exactly that model.
        """
        self._queue: deque[Outcome] = deque()
        self._calls: list[FakeLlmCall] = []
        self._default_response = default_response
        self._factory = response_factory
        self._expected_response_model = expected_response_model

    def enqueue(self, outcome: Outcome) -> None:
        """Append one scripted response or exception to the FIFO queue."""
        self._queue.append(outcome)

    def reset(self) -> None:
        """Clear the scripted queue and the recorded calls."""
        self._queue.clear()
        self._calls.clear()

    @property
    def calls(self) -> tuple[FakeLlmCall, ...]:
        """Return every recorded call, in invocation order."""
        return tuple(self._calls)

    @property
    def call_count(self) -> int:
        """Return the number of recorded calls."""
        return len(self._calls)

    async def _generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ResponseT],
        operation_name: str,
    ) -> ResponseT:
        """Implement the LlmClient hook with scripted deterministic outcomes."""
        call = FakeLlmCall(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
            operation_name=operation_name,
        )
        self._calls.append(call)

        if (
            self._expected_response_model is not None
            and response_model is not self._expected_response_model
        ):
            raise AssertionError(
                "FakeLlmClient expected response_model "
                f"{self._expected_response_model.__name__}, got "
                f"{response_model.__name__}"
            )

        if self._queue:
            outcome = self._queue.popleft()
        elif self._factory is not None:
            outcome = self._factory(call)
        elif self._default_response is not None:
            outcome = self._default_response
        else:
            raise AssertionError(
                f"FakeLlmClient has no scripted outcome for operation "
                f"{operation_name!r}"
            )

        if isinstance(outcome, BaseException):
            raise outcome
        if not isinstance(outcome, response_model):
            raise LlmError(LlmErrorCode.INVALID_STRUCTURED_OUTPUT, retryable=True)
        return outcome


__all__ = ["FakeLlmCall", "FakeLlmClient", "Outcome"]
