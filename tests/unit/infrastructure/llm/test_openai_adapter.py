# SPDX-License-Identifier: AGPL-3.0-only
"""PR 10 provider adapter unit matrix (LA1-LA10).

Tests the real ``OpenAiLlmClient`` adapter while stubbing only the external
SDK transport boundary (the ``openai.AsyncOpenAI`` client instance it owns).
The adapter's own validation, error mapping, retry policy, and cancellation
behavior run unmodified; no Darkula ``LlmClient`` type is mocked when
testing the adapter itself.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from pydantic import BaseModel

from darkula.app.llm import LlmError, LlmErrorCode
from darkula.infrastructure.llm import OpenAiLlmClient

_API_KEY = "sk-test-do-not-expose"


class _DecisionModel(BaseModel):
    """Small Darkula-owned structured-output model for adapter tests."""

    action: str
    confidence: float


def _fake_parsed(*, instance: BaseModel | None, refusal: str | None = None) -> Any:
    """A ParsedChatCompletion-shaped object with a parsed message."""
    message = SimpleNamespace(parsed=instance, refusal=refusal)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def _status_response(status_code: int) -> Any:
    """An httpx.Response-shaped object the SDK exception ctor needs."""
    return SimpleNamespace(
        status_code=status_code,
        request=SimpleNamespace(),
        headers={"x-request-id": "test"},
    )


class _FakeAsyncOpenAI:
    """Injected stand-in for the owned SDK transport client."""

    def __init__(self) -> None:
        self.result: Any | None = None
        self.error: BaseException | None = None
        self.calls: list[dict[str, Any]] = []

    def _completions(self) -> Any:
        return SimpleNamespace(parse=self._parse)

    async def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("fake transport had no scripted result")
        return self.result

    @property
    def beta(self) -> Any:
        return SimpleNamespace(chat=SimpleNamespace(completions=self._completions()))


def _client(*, fake: _FakeAsyncOpenAI | None = None, **kwargs: Any) -> OpenAiLlmClient:
    """Build the real adapter, then inject the stub transport client."""
    client = OpenAiLlmClient(
        model_name=kwargs.pop("model_name", "darkula-test"),
        api_key=kwargs.pop("api_key", _API_KEY),
        timeout_seconds=kwargs.pop("timeout_seconds", 30.0),
        base_url=kwargs.pop("base_url", None),
        **kwargs,
    )
    object.__setattr__(client, "_client", fake or _FakeAsyncOpenAI())
    return client


class TestOpenAiLlmClient:
    """LA1-LA8: one-attempt structured output with bounded mapping."""

    @pytest.mark.asyncio
    async def test_la1_valid_structured_output_returns_exact_model(self) -> None:
        fake = _FakeAsyncOpenAI()
        client = _client(fake=fake)
        instance = _DecisionModel(action="inspect", confidence=0.5)
        fake.result = _fake_parsed(instance=instance)
        output = await client.generate_structured(
            system_prompt="s",
            user_prompt="u",
            response_model=_DecisionModel,
            operation_name="recon.assess",
        )
        assert isinstance(output, _DecisionModel)
        assert output is instance
        call = fake.calls[0]
        assert call["response_format"] is _DecisionModel
        assert call["model"] == "darkula-test"
        assert call["timeout"] == 30.0

    @pytest.mark.asyncio
    async def test_la2_timeout_maps_to_timed_out(self) -> None:
        fake = _FakeAsyncOpenAI()
        fake.error = openai.APITimeoutError(
            request=httpx.Request("GET", "https://x.example")
        )
        client = _client(fake=fake)
        with pytest.raises(LlmError) as captured:
            await client.generate_structured(
                system_prompt="s",
                user_prompt="u",
                response_model=_DecisionModel,
                operation_name="recon.assess",
            )
        assert captured.value.code is LlmErrorCode.TIMEOUT

    @pytest.mark.asyncio
    async def test_la3_provider_failure_maps(self) -> None:
        errors = [
            openai.InternalServerError(
                "boom", response=_status_response(500), body=None
            ),
            openai.APIConnectionError(
                request=httpx.Request("GET", "https://x.example")
            ),
            openai.RateLimitError(
                "slow down", response=_status_response(429), body=None
            ),
        ]
        for error in errors:
            fake = _FakeAsyncOpenAI()
            fake.error = error
            client = _client(fake=fake)
            with pytest.raises(LlmError) as captured:
                await client.generate_structured(
                    system_prompt="s",
                    user_prompt="u",
                    response_model=_DecisionModel,
                    operation_name="recon.assess",
                )
            assert captured.value.code is LlmErrorCode.PROVIDER_FAILURE
            assert captured.value.retryable is True

    @pytest.mark.asyncio
    async def test_la4_invalid_output_maps(self) -> None:
        # No parsed content at all.
        fake = _FakeAsyncOpenAI()
        fake.result = _fake_parsed(instance=None, refusal=None)
        client = _client(fake=fake)
        with pytest.raises(LlmError) as captured:
            await client.generate_structured(
                system_prompt="s",
                user_prompt="u",
                response_model=_DecisionModel,
                operation_name="recon.assess",
            )
        assert captured.value.code is LlmErrorCode.INVALID_STRUCTURED_OUTPUT
        assert captured.value.retryable is True

    @pytest.mark.asyncio
    async def test_la4_wrong_type_maps(self) -> None:
        class _Other(BaseModel):
            x: int

        fake = _FakeAsyncOpenAI()
        fake.result = _fake_parsed(instance=_Other(x=1))
        client = _client(fake=fake)
        with pytest.raises(LlmError) as captured:
            await client.generate_structured(
                system_prompt="s",
                user_prompt="u",
                response_model=_DecisionModel,
                operation_name="recon.assess",
            )
        assert captured.value.code is LlmErrorCode.INVALID_STRUCTURED_OUTPUT

    @pytest.mark.asyncio
    async def test_la4_refusal_maps(self) -> None:
        fake = _FakeAsyncOpenAI()
        fake.result = _fake_parsed(instance=None, refusal="I cannot do that")
        client = _client(fake=fake)
        with pytest.raises(LlmError) as captured:
            await client.generate_structured(
                system_prompt="s",
                user_prompt="u",
                response_model=_DecisionModel,
                operation_name="recon.assess",
            )
        assert captured.value.code is LlmErrorCode.INVALID_STRUCTURED_OUTPUT

    @pytest.mark.asyncio
    async def test_la4_empty_choices_maps(self) -> None:
        fake = _FakeAsyncOpenAI()
        fake.result = SimpleNamespace(choices=[])
        client = _client(fake=fake)
        with pytest.raises(LlmError) as captured:
            await client.generate_structured(
                system_prompt="s",
                user_prompt="u",
                response_model=_DecisionModel,
                operation_name="recon.assess",
            )
        assert captured.value.code is LlmErrorCode.INVALID_STRUCTURED_OUTPUT

    @pytest.mark.asyncio
    async def test_la5_config_auth_failure_maps(self) -> None:
        errors = [
            openai.AuthenticationError(
                "bad key", response=_status_response(401), body=None
            ),
            openai.PermissionDeniedError(
                "denied", response=_status_response(403), body=None
            ),
            openai.NotFoundError("no model", response=_status_response(404), body=None),
            openai.BadRequestError("bad", response=_status_response(400), body=None),
        ]
        for error in errors:
            fake = _FakeAsyncOpenAI()
            fake.error = error
            client = _client(fake=fake)
            with pytest.raises(LlmError) as captured:
                await client.generate_structured(
                    system_prompt="s",
                    user_prompt="u",
                    response_model=_DecisionModel,
                    operation_name="recon.assess",
                )
            assert captured.value.code is LlmErrorCode.CONFIGURATION_ERROR
            assert captured.value.retryable is False

    @pytest.mark.asyncio
    async def test_la6_cancellation_propagates_unchanged(self) -> None:
        fake = _FakeAsyncOpenAI()
        fake.error = asyncio.CancelledError()
        client = _client(fake=fake)
        with pytest.raises(asyncio.CancelledError):
            await client.generate_structured(
                system_prompt="s",
                user_prompt="u",
                response_model=_DecisionModel,
                operation_name="recon.assess",
            )

    @pytest.mark.asyncio
    async def test_la7_secret_and_raw_text_absent_from_public_error(self) -> None:
        fake = _FakeAsyncOpenAI()
        fake.error = RuntimeError("secret sk-live-12345 exploded inside provider")
        client = _client(fake=fake)
        with pytest.raises(LlmError) as captured:
            await client.generate_structured(
                system_prompt="s",
                user_prompt="u",
                response_model=_DecisionModel,
                operation_name="recon.assess",
            )
        message = str(captured.value)
        assert captured.value.code is LlmErrorCode.PROVIDER_FAILURE
        assert "sk-live-12345" not in message
        assert "exploded" not in message
        assert "password" not in message

    @pytest.mark.asyncio
    async def test_la8_retries_disabled_client_construction(self) -> None:
        # The real adapter constructs the real SDK client with max_retries=0
        # and a bounded timeout; the SDK client itself records these.
        client = OpenAiLlmClient(
            model_name="darkula-model", api_key=_API_KEY, timeout_seconds=7.0
        )
        transport = client._client
        assert transport.max_retries == 0
        assert transport.timeout == 7.0
        assert transport.api_key == _API_KEY
        assert str(transport.base_url) == "https://api.openai.com/v1/"

    def test_la9_missing_config_fails_fast(self) -> None:
        with pytest.raises(ValueError, match="model_name"):
            OpenAiLlmClient(model_name="", api_key="k")
        with pytest.raises(ValueError, match="api_key"):
            OpenAiLlmClient(model_name="m", api_key="  ")
        with pytest.raises(ValueError, match="timeout"):
            OpenAiLlmClient(model_name="m", api_key="k", timeout_seconds=0)
        with pytest.raises(ValueError, match="timeout"):
            OpenAiLlmClient(model_name="m", api_key="k", timeout_seconds=601)

    def test_la10_adapter_construction_is_offline(self) -> None:
        client = OpenAiLlmClient(model_name="m", api_key=_API_KEY, timeout_seconds=9.0)
        assert client.model_name == "m"
        assert isinstance(client._client, openai.AsyncOpenAI)
        # Constructing performed no transport calls (nothing to assert on a
        # live SDK client without opening a connection).
        assert client._timeout_seconds == 9.0
