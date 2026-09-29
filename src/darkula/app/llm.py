# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula-owned LLM operation boundary.

This is the narrow, provider-neutral LLM seam used by Darkula application
code. It is structured-output only: every operation supplies an explicit
Darkula/Pydantic ``response_model`` and receives an instance of exactly that
type. No provider-specific response object, chat history, configuration
read, persistence call, hidden retry loop, or tool use escapes this
boundary.

LLM failures are typed and bounded. Public messages never contain prompt
content, model output, raw provider exception text, or credentials; they are
safe to propagate into application logs and errors.
``asyncio.CancelledError`` always propagates unchanged so cooperative
cancellation is never swallowed by error mapping.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel

from darkula.domain.identifiers import OperationName

ResponseT = TypeVar("ResponseT", bound=BaseModel)
"""Type variable bound to Darkula/Pydantic structured-output models."""


class LlmErrorCode(StrEnum):
    """Stable categories for typed LLM operation failures.

    The taxonomy is deliberately small and provider-neutral. Finer-grained
    provider categories (for example rate limiting or content refusal) remain
    future work and must be mapped conservatively rather than exposed.
    """

    TIMEOUT = "timeout"
    """The model operation exceeded the configured bounded timeout."""

    PROVIDER_FAILURE = "provider_failure"
    """The provider failed while serving the operation."""

    INVALID_STRUCTURED_OUTPUT = "invalid_structured_output"
    """The provider response could not be validated as the requested model."""

    CONFIGURATION_ERROR = "configuration_error"
    """The LLM was misconfigured or the provider rejected the configuration."""


class LlmError(RuntimeError):
    """A typed LLM operation failure with a stable code and retryability flag.

    ``retryable`` expresses the documented, provider-neutral retry policy:
    only ``INVALID_STRUCTURED_OUTPUT`` (and, at the caller's conservative
    discretion, configured transient ``PROVIDER_FAILURE``) may be retried.
    Authentication/configuration failures and cancellation are never retried.
    """

    message: str = "LLM operation failed"

    def __init__(self, code: LlmErrorCode, *, retryable: bool = False) -> None:
        """Initialize with the stable category and retryability decision."""
        super().__init__(f"{self.message}: {code.value}")
        self.code = code
        self.retryable = retryable


def validate_operation_name(operation_name: str) -> str:
    """Return a normalized operation name or raise ``ValueError``.

    ``operation_name`` must satisfy the same non-blank, bounded,
    control-free rules as :class:`~darkula.domain.identifiers.OperationName`.
    This is the contract-enforced entry validation for LLM calls.
    """
    return OperationName(operation_name).value


class LlmClient(ABC):
    """Generate one schema-validated structured output from a model.

    One ``generate_structured`` call represents one model attempt; there is
    no hidden retry loop implied by the interface. Implementations are
    single-attempt transports: bounded structured-output repair is the
    application service's explicit, accounted policy, never a hidden adapter
    loop. No implementation may read environment/configuration directly,
    persist anything, or execute tools.

    Implementations override the private abstract hook
    :meth:`_generate_structured`; the public :meth:`generate_structured`
    method validates the bounded operation metadata before the attempt.
    """

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ResponseT],
        operation_name: str,
    ) -> ResponseT:
        """Return an instance of ``response_model`` or raise :class:`LlmError`.

        ``operation_name`` is stable, bounded metadata validated against the
        :class:`~darkula.domain.identifiers.OperationName` rules; it is never
        prompt content. Implementations map provider/framework failures to
        ``LlmError`` categories with bounded safe messages and propagate
        cancellation unchanged.
        """
        validate_operation_name(operation_name)
        return await self._generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
            operation_name=operation_name,
        )

    @abstractmethod
    async def _generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ResponseT],
        operation_name: str,
    ) -> ResponseT:
        """Implement one schema-validated model attempt.

        Contract semantics are inherited from :meth:`generate_structured`;
        this hook is invoked only after validation has passed.
        """


__all__ = [
    "LlmClient",
    "LlmError",
    "LlmErrorCode",
    "ResponseT",
    "validate_operation_name",
]
