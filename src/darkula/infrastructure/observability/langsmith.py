# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""LangSmith-backed ``AgentObservability`` adapter (PR 15).

Implements the existing, content-safe, fail-open
:class:`~darkula.app.agent_observability.AgentObservability` SPI. LangSmith
SDK types stay inside this infrastructure module; the application boundary
only ever sees :class:`AgentOperationMetadata`.

Safety properties frozen here:

- only bounded, content-free metadata is sent (operation/agent/model/profile/
  prompt-version/source/run identifiers);
- prompts, model outputs, collected content, credentials, and hidden
  reasoning are not representable through the SPI and are never sent;
- backend failures (start/finish/patch/flush) are fail-open and never alter
  the observed application operation;
- ``BaseException`` (including ``asyncio.CancelledError`` and
  ``KeyboardInterrupt``) is never swallowed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol, cast

from darkula.app.agent_observability import (
    AgentObservability,
    AgentOperationMetadata,
)

__all__ = [
    "LangSmithAgentObservability",
    "LangSmithRun",
    "LangSmithRunFactory",
    "build_langsmith_run_factory",
]


class LangSmithRun(Protocol):
    """Minimal LangSmith run lifecycle used by the adapter."""

    def post(self, *, exclude_child_runs: bool = True) -> None:
        """Create the run server-side."""
        ...

    def end(
        self,
        *,
        end_time: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Mark the run ended at ``end_time``."""
        ...

    def patch(self, *, exclude_inputs: bool | None = None) -> None:
        """Send the ended run's update."""
        ...


class LangSmithRunFactory(Protocol):
    """Factory creating one run object per observed operation."""

    def __call__(
        self,
        *,
        name: str,
        metadata: Mapping[str, Any],
        start_time: datetime,
    ) -> LangSmithRun:
        """Create (but not yet post) one run."""
        ...


def _safe_metadata(metadata: AgentOperationMetadata) -> dict[str, Any]:
    """Map only safe, content-free SPI fields to a metadata document."""
    payload: dict[str, Any] = {"darkula.operation_name": metadata.operation_name}
    optional = {
        "darkula.agent_name": metadata.agent_name,
        "darkula.model_provider": metadata.model_provider,
        "darkula.model_name": metadata.model_name,
        "darkula.model_profile": metadata.model_profile,
        "darkula.prompt_version": metadata.prompt_version,
        "darkula.source_id": metadata.source_id,
        "darkula.collection_run_id": metadata.collection_run_id,
    }
    for key, value in optional.items():
        if value is not None:
            payload[key] = value
    return payload


class _LangSmithOperation(AbstractContextManager[None]):
    """One fail-open LangSmith operation scope."""

    def __init__(
        self,
        *,
        run_factory: LangSmithRunFactory,
        metadata: AgentOperationMetadata,
    ) -> None:
        self._run_factory = run_factory
        self._metadata = metadata
        self._run: LangSmithRun | None = None

    def __enter__(self) -> None:
        try:
            run = self._run_factory(
                name=self._metadata.operation_name,
                metadata=_safe_metadata(self._metadata),
                start_time=datetime.now(UTC),
            )
            run.post()
        except Exception:
            self._run = None
            return None
        self._run = run
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        run = self._run
        if run is None:
            return
        try:
            run.end(end_time=datetime.now(UTC))
            run.patch(exclude_inputs=True)
        except Exception:  # nosec B110 - fail-open observability contract
            pass


class LangSmithAgentObservability(AgentObservability):
    """Provider-neutral SPI backed by an injected LangSmith run factory."""

    def __init__(
        self,
        *,
        run_factory: LangSmithRunFactory,
        flush: Callable[[], None] | None = None,
    ) -> None:
        self._run_factory = run_factory
        self._flush = flush

    def operation(
        self,
        *,
        metadata: AgentOperationMetadata,
    ) -> AbstractContextManager[None]:
        """Return one fail-open LangSmith operation scope."""
        return _LangSmithOperation(run_factory=self._run_factory, metadata=metadata)

    def flush(self) -> None:
        """Flush the backend fail-open (never raises into application work)."""
        if self._flush is None:
            return
        try:
            self._flush()
        except Exception:
            return


def build_langsmith_run_factory(
    *,
    project: str,
    api_key: str | None,
    endpoint_url: str | None = None,
) -> LangSmithRunFactory:
    """Build a real LangSmith run factory (imports the SDK lazily).

    The SDK import is deliberately isolated here so provider types never
    reach application/domain code and tests can inject a fake factory
    without importing LangSmith.
    """
    from langsmith import Client, RunTree

    client = Client(api_url=endpoint_url, api_key=api_key)

    def factory(
        *,
        name: str,
        metadata: Mapping[str, Any],
        start_time: datetime,
    ) -> LangSmithRun:
        return cast(
            LangSmithRun,
            RunTree(
                name=name,
                run_type="llm",
                inputs={},
                start_time=start_time,
                extra={"metadata": dict(metadata)},
                project_name=project,
                ls_client=client,
            ),
        )

    return factory
