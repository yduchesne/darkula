# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic FakeSandbox test double (PR 7).

Implements the **real** :class:`Sandbox` SPI above the sandbox boundary:
it records every received execution request/policy and returns scripted
results/failures on demand. It performs no Podman, no browser, and no
container behavior.

Rules frozen here:

- usage is limited to controller unit tests (``real CrawlerController ->
  FakeSandbox``); production composition never references it and never
  silently substitutes it for the real adapter;
- scripted results are returned in FIFO order; an empty script fails the
  test loudly instead of inventing behavior;
- scripted exceptions propagate unchanged (infrastructure failures are
  never converted to success);
- an explicit ``await_forever`` script makes ``execute`` cancellable so
  tests can assert ``asyncio.CancelledError`` propagation and record that
  cancellation reached the sandbox.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from darkula.sandbox.contracts import (
    Sandbox,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    validate_execution_request,
)


@dataclass(frozen=True, slots=True)
class _AwaitForever:
    """Script marker: block until the calling task is cancelled."""


@dataclass(frozen=True, slots=True)
class _Raise:
    """Script marker: raise the wrapped exception (typed failures)."""

    error: BaseException


class FakeSandbox(Sandbox):
    """Deterministic recording Sandbox for above-boundary unit tests."""

    def __init__(self) -> None:
        self.executions: list[SandboxExecutionRequest] = []
        """Every execution request received, in call order."""
        self.cancelled: list[SandboxExecutionRequest] = []
        """Execution requests whose ``execute`` was cancelled by the caller."""
        self._script: list[Any] = []

    def enqueue_result(self, result: SandboxExecutionResult) -> None:
        """Script one exact result to return (FIFO)."""
        self._script.append(result)

    def enqueue_error(self, error: BaseException) -> None:
        """Script a typed exception to raise (FIFO)."""
        self._script.append(_Raise(error))

    def enqueue_await_forever(self) -> None:
        """Script a cancellable infinite await (FIFO)."""
        self._script.append(_AwaitForever())

    @property
    def script_empty(self) -> bool:
        """Return whether no scripted outcomes remain."""
        return not self._script

    async def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        validate_execution_request(request)
        self.executions.append(request)
        try:
            item = self._script.pop(0)
        except IndexError as exc:  # pragma: no cover - test usage error
            raise AssertionError(
                "FakeSandbox received an execution with an empty script; "
                "enqueue a result/error/await-forever first"
            ) from exc
        if isinstance(item, _AwaitForever):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.append(request)
                raise
            raise AssertionError("await_forever marker resolved")  # pragma: no cover
        if isinstance(item, _Raise):
            raise item.error
        if not isinstance(item, SandboxExecutionResult):
            raise AssertionError("unexpected scripted result")
        return item


__all__ = ["FakeSandbox"]
