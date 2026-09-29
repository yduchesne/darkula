# SPDX-License-Identifier: AGPL-3.0-only
"""FakeLlmClient tests (L-* matrix)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from darkula.app.llm import LlmError, LlmErrorCode
from darkula.testing.fake_llm import FakeLlmCall, FakeLlmClient


class SentenceModel(BaseModel):
    """Small structured-output model for fake LLM tests."""

    text: str
    words: int


class SummaryModel(BaseModel):
    """A second structured-output model for type-mismatch tests."""

    summary: str


async def _call(
    client: FakeLlmClient,
    *,
    response_model: type[BaseModel] = SentenceModel,
    operation_name: str = "sources.describe_record",
) -> BaseModel:
    return await client.generate_structured(
        system_prompt="Reply with the requested model only.",
        user_prompt="What is in this record?",
        response_model=response_model,
        operation_name=operation_name,
    )


class TestTypedOutcomes:
    """L01: typed Pydantic response through the public validation path."""

    @pytest.mark.asyncio
    async def test_returns_requested_pydantic_model(self) -> None:
        client = FakeLlmClient()
        client.enqueue(SentenceModel(text="hello world", words=2))
        output = await _call(client)
        assert isinstance(output, SentenceModel)
        assert output.text == "hello world"
        assert output.words == 2

    @pytest.mark.asyncio
    async def test_l02_first_in_first_out(self) -> None:
        client = FakeLlmClient()
        client.enqueue(SentenceModel(text="one", words=1))
        client.enqueue(SentenceModel(text="two", words=2))
        first = await _call(client)
        second = await _call(client)
        assert isinstance(first, SentenceModel)
        assert isinstance(second, SentenceModel)
        assert first.text == "one"
        assert second.text == "two"

    @pytest.mark.asyncio
    async def test_l03_same_llm_error_instance_raised(self) -> None:
        client = FakeLlmClient()
        error = LlmError(LlmErrorCode.TIMEOUT, retryable=True)
        client.enqueue(error)
        with pytest.raises(LlmError) as exc_info:
            await _call(client)
        assert exc_info.value is error
        assert error.retryable is True

    @pytest.mark.asyncio
    async def test_l04_scripted_cancellation_propagates_unchanged(self) -> None:
        client = FakeLlmClient()
        client.enqueue(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _call(client)


class TestFailClosed:
    """L05/L06: wrong type and unscripted calls fail closed."""

    @pytest.mark.asyncio
    async def test_l05_wrong_type_rejected_as_invalid_structured_output(self) -> None:
        client = FakeLlmClient()
        client.enqueue(SentenceModel(text="wrong", words=1))
        with pytest.raises(LlmError) as exc_info:
            await _call(client, response_model=SummaryModel)
        assert exc_info.value.code is LlmErrorCode.INVALID_STRUCTURED_OUTPUT
        assert exc_info.value.retryable is True

    @pytest.mark.asyncio
    async def test_l06_unscripted_call_fails_closed(self) -> None:
        client = FakeLlmClient()
        with pytest.raises(AssertionError, match="no scripted outcome"):
            await _call(client)


class TestResolutionPrecedence:
    """L07-L10: default -> factory -> queue precedence."""

    @pytest.mark.asyncio
    async def test_l07_fixed_default_response(self) -> None:
        default = SentenceModel(text="default", words=1)
        client = FakeLlmClient(default_response=default)
        first = await _call(client)
        second = await _call(client)
        assert isinstance(first, SentenceModel)
        assert isinstance(second, SentenceModel)
        assert first.text == "default"
        assert second.text == "default"

    @pytest.mark.asyncio
    async def test_l08_factory_receives_exact_recorded_call(self) -> None:
        seen: list[FakeLlmCall] = []

        def factory(call: FakeLlmCall) -> BaseModel:
            seen.append(call)
            return SentenceModel(text=call.user_prompt, words=len(call.operation_name))

        client = FakeLlmClient(response_factory=factory)
        output = await _call(client, operation_name="sources.assess")
        assert isinstance(output, SentenceModel)
        assert output.text == "What is in this record?"
        assert output.words == len("sources.assess")
        assert len(seen) == 1
        assert seen[0].system_prompt == "Reply with the requested model only."
        assert seen[0].user_prompt == "What is in this record?"
        assert seen[0].response_model is SentenceModel
        assert seen[0].operation_name == "sources.assess"

    @pytest.mark.asyncio
    async def test_l09_queue_beats_factory(self) -> None:
        client = FakeLlmClient(
            response_factory=lambda call: SentenceModel(text="factory", words=9)
        )
        client.enqueue(SentenceModel(text="queued", words=1))
        output = await _call(client)
        assert isinstance(output, SentenceModel)
        assert output.text == "queued"

    @pytest.mark.asyncio
    async def test_l10_factory_beats_default(self) -> None:
        client = FakeLlmClient(
            default_response=SentenceModel(text="default", words=1),
            response_factory=lambda call: SentenceModel(text="factory", words=2),
        )
        output = await _call(client)
        assert isinstance(output, SentenceModel)
        assert output.text == "factory"


class TestAssertions:
    """L11-L13: expected-model assertion and exact call recording."""

    @pytest.mark.asyncio
    async def test_l11_expected_model_mismatch_asserts(self) -> None:
        client = FakeLlmClient(
            expected_response_model=SentenceModel,
            default_response=SentenceModel(text="x", words=1),
        )
        with pytest.raises(AssertionError, match="expected response_model"):
            await _call(client, response_model=SummaryModel)

    @pytest.mark.asyncio
    async def test_l12_invalid_operation_name_rejected_before_hook(self) -> None:
        client = FakeLlmClient()
        with pytest.raises(ValueError, match="must not be blank"):
            await _call(client, operation_name="  ")
        # The base-class validation failed before the fake hook ran.
        assert client.call_count == 0

    @pytest.mark.asyncio
    async def test_l13_exact_call_recording(self) -> None:
        client = FakeLlmClient()
        client.enqueue(SentenceModel(text="one", words=1))
        await _call(client, operation_name="sources.one")
        client.enqueue(SentenceModel(text="wrong", words=1))
        with pytest.raises(LlmError):
            await _call(client, response_model=SummaryModel)

        assert client.call_count == 2
        first, second = client.calls
        assert first.operation_name == "sources.one"
        assert first.response_model is SentenceModel
        assert second.response_model is SummaryModel

    def test_reset_clears_queue_and_call_record(self) -> None:
        client = FakeLlmClient()
        client.enqueue(SentenceModel(text="x", words=1))
        client.reset()
        assert client.call_count == 0
        assert client.calls == ()
