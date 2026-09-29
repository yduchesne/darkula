# SPDX-License-Identifier: AGPL-3.0-only
"""No-op AgentObservability backend for the ``NONE`` selection (PR 3).

The observability contract is fail-open with respect to application work:
an observability backend failure must never fail, retry, or otherwise alter
the observed operation. The no-op backend trivially satisfies this: it
records nothing and never captures prompts, model output, or content.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext

from darkula.app.agent_observability import (
    AgentObservability,
    AgentOperationMetadata,
)


class NoOpAgentObservability(AgentObservability):
    """Provider-neutral observability backend that records nothing."""

    def operation(
        self,
        *,
        metadata: AgentOperationMetadata,
    ) -> AbstractContextManager[None]:
        """Return a harmless no-op scope for the observed operation."""
        return nullcontext(None)


__all__ = ["NoOpAgentObservability"]
