# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula-owned agent/LLM observability boundary.

Agent/LLM observability (LangSmith, Langfuse, future backends) is a distinct
concern from OpenTelemetry operational telemetry and is never embedded in
:class:`~darkula.app.llm.LlmClient`. This contract is provider-neutral: the
application layer never imports LangSmith or Langfuse.

Observation metadata is deliberately bounded and content-safe. The following
are **never** representable:

- prompts;
- model output;
- normalized/collected content;
- extracted credentials or PII;
- raw provider responses;
- hidden reasoning.

Backend behavior for future adapters is fail-open with respect to
application work, observability never triggers model retries, and
cancellation of the observed application operation is unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass

#: Upper bound for any portable agent-observability metadata field.
MAX_AGENT_OBSERVABILITY_FIELD_LENGTH = 200


@dataclass(frozen=True, slots=True)
class AgentOperationMetadata:
    """Bounded, content-free metadata for one observed agent/LLM operation.

    Only safe, bounded values are permitted. Optional identifiers such as
    ``source_id`` and ``collection_run_id`` may be represented only when the
    referenced values are safe to share with an observability backend.
    """

    operation_name: str
    """Non-blank, bounded, control-free operation name."""

    agent_name: str | None = None
    """Optional bounded agent name (never prompt or content)."""

    model_provider: str | None = None
    """Optional bounded model provider (for example ``openai``)."""

    model_name: str | None = None
    """Optional bounded model identifier (for example ``gpt-4o``)."""

    model_profile: str | None = None
    """Optional bounded deployment profile name."""

    prompt_version: str | None = None
    """Optional bounded prompt version tag."""

    source_id: str | None = None
    """Optional safely-representable source identifier."""

    collection_run_id: str | None = None
    """Optional safely-representable collection run identifier."""

    def __post_init__(self) -> None:
        """Validate all bounded metadata fields at construction time."""
        validate_agent_observation_metadata(self)


def _validate_optional_field(field_name: str, value: str | None) -> None:
    """Reject blank, over-long, or control-character-bearing metadata."""
    if value is None:
        return
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank when provided")
    if len(value) > MAX_AGENT_OBSERVABILITY_FIELD_LENGTH:
        raise ValueError(f"{field_name} exceeds the maximum observability field length")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{field_name} must not contain control characters")


def validate_agent_observation_metadata(metadata: AgentOperationMetadata) -> None:
    """Validate one :class:`AgentOperationMetadata` instance.

    The ``operation_name`` is required; every other field is optional but,
    when provided, must be bounded and control-free.
    """
    _validate_optional_field("operation_name", metadata.operation_name)
    _validate_optional_field("agent_name", metadata.agent_name)
    _validate_optional_field("model_provider", metadata.model_provider)
    _validate_optional_field("model_name", metadata.model_name)
    _validate_optional_field("model_profile", metadata.model_profile)
    _validate_optional_field("prompt_version", metadata.prompt_version)
    _validate_optional_field("source_id", metadata.source_id)
    _validate_optional_field("collection_run_id", metadata.collection_run_id)


class AgentObservability(ABC):
    """Record a provider-neutral agent/LLM operation lifecycle.

    ``operation`` returns a context manager whose scope covers one
    agent/LLM operation. Implementations are fail-open: a backend failure
    must never fail, retry, or otherwise alter the application-facing
    operation, and cancellation always propagates unchanged. Implementations
    never capture prompt/model-output/content by default.
    """

    @abstractmethod
    def operation(
        self,
        *,
        metadata: AgentOperationMetadata,
    ) -> AbstractContextManager[None]:
        """Open an observability operation scope for ``metadata``.

        ``metadata`` must already satisfy the bounded-metadata validation
        enforced by :class:`AgentOperationMetadata`; the returned context
        manager is entered by the caller around the agent/LLM operation it
        describes.
        """


__all__ = [
    "MAX_AGENT_OBSERVABILITY_FIELD_LENGTH",
    "AgentObservability",
    "AgentOperationMetadata",
    "validate_agent_observation_metadata",
]
