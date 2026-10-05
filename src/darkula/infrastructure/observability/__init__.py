# SPDX-License-Identifier: AGPL-3.0-only
"""Agent/LLM observability package (provider-neutral backends).

PR 15 delivers the LangSmith adapter behind the existing
:class:`~darkula.app.agent_observability.AgentObservability` SPI. Provider
SDK types stay inside the infrastructure adapter. Langfuse remains an
explicitly unavailable backend (composition fails fast).
"""

from darkula.infrastructure.observability.langsmith import (
    LangSmithAgentObservability,
    LangSmithRun,
    LangSmithRunFactory,
    build_langsmith_run_factory,
)
from darkula.infrastructure.observability.noop import NoOpAgentObservability

__all__ = [
    "LangSmithAgentObservability",
    "LangSmithRun",
    "LangSmithRunFactory",
    "NoOpAgentObservability",
    "build_langsmith_run_factory",
]
