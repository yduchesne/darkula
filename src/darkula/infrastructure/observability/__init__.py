# SPDX-License-Identifier: AGPL-3.0-only
"""Agent/LLM observability package (provider-neutral backends).

PR 3 ships only the coherent ``NONE`` backend; LangSmith/Langfuse adapters
are PR 15 work and must never be referenced here.
"""

from darkula.infrastructure.observability.noop import NoOpAgentObservability

__all__ = ["NoOpAgentObservability"]
