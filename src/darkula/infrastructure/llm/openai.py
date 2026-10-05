# SPDX-License-Identifier: AGPL-3.0-only
"""OpenAI structured-output LlmClient adapter (PR 10).

The single provider-backed :class:`~darkula.app.llm.LlmClient` adapter,
verified against ``openai==1.109.1`` (Python 3.14 compatible):

- ``client.beta.chat.completions.parse(..., response_format=<pydantic model>)``
  returns a ``ParsedChatCompletion`` whose ``choices[0].message.parsed`` is
  an instance of exactly the requested model;
- one ``LlmClient`` call is one model attempt: the SDK client is always
  constructed with ``max_retries=0`` and no tools/persistence are ever
  configured;
- a bounded timeout is applied at construction and per request;
- ``asyncio.CancelledError`` propagates unchanged (never mapped);
- provider failures map to the bounded ``LlmError`` taxonomy with sanitized
  public messages; raw provider exception text, prompts, model output, and
  credentials are never exposed.

Only this adapter may import ``openai``; application/domain contracts stay
provider-neutral.
"""

from __future__ import annotations

import asyncio

import openai

from darkula.app.llm import LlmClient, LlmError, LlmErrorCode, ResponseT

#: Bounded upper bound for the adapter's per-attempt timeout.
_MAX_TIMEOUT_SECONDS = 600.0


class OpenAiLlmClient(LlmClient):
    """One-attempt structured-output adapter over the OpenAI Python SDK.

    Constructing the client performs no network I/O; the SDK client is
    created with ``max_retries=0`` so an adapter call can never retry
    hidden behind a single ``LlmClient`` invocation.
    """

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not model_name or not model_name.strip():
            raise ValueError("model_name must not be blank")
        if not api_key or not api_key.strip():
            raise ValueError("api_key must not be blank")
        if not (0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS):
            raise ValueError("timeout_seconds must be within (0, 600]")
        self._model_name = model_name.strip()
        self._timeout_seconds = timeout_seconds
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    @property
    def model_name(self) -> str:
        """The configured provider model identifier (bounded metadata)."""
        return self._model_name

    async def _generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ResponseT],
        operation_name: str,
    ) -> ResponseT:
        """One schema-validated model attempt with a bounded timeout."""
        try:
            completion = await self._client.beta.chat.completions.parse(
                model=self._model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=response_model,
                timeout=self._timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except openai.APITimeoutError:
            raise LlmError(LlmErrorCode.TIMEOUT) from None
        except (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.NotFoundError,
            openai.BadRequestError,
            openai.UnprocessableEntityError,
        ) as exc:
            # Invalid API key/deployment/model or an unprocessable request
            # shape is a configuration problem, never provider output.
            raise LlmError(LlmErrorCode.CONFIGURATION_ERROR) from exc
        except (
            openai.RateLimitError,
            openai.InternalServerError,
            openai.APIConnectionError,
            openai.APIError,
        ) as exc:
            # Provider-side/service failures (5xx, connection, rate limit).
            raise LlmError(LlmErrorCode.PROVIDER_FAILURE, retryable=True) from exc
        except Exception as exc:
            # Unknown adapter-boundary failure: bounded provider failure; the
            # raw exception text is never exposed.
            raise LlmError(LlmErrorCode.PROVIDER_FAILURE) from exc

        if completion.choices is None or not completion.choices:
            raise LlmError(LlmErrorCode.INVALID_STRUCTURED_OUTPUT, retryable=True)
        message = completion.choices[0].message
        if message.refusal is not None:
            raise LlmError(LlmErrorCode.INVALID_STRUCTURED_OUTPUT, retryable=True)
        parsed = message.parsed
        if not isinstance(parsed, response_model):
            raise LlmError(LlmErrorCode.INVALID_STRUCTURED_OUTPUT, retryable=True)
        return parsed


__all__ = ["OpenAiLlmClient"]
