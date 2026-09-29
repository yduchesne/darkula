# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the LlmClient contract (LLM-*)."""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest
from pydantic import BaseModel

from darkula.app.llm import LlmClient, LlmError, LlmErrorCode, validate_operation_name


class SentenceModel(BaseModel):
    """A small Darkula-owned Pydantic structured-output model."""

    text: str
    words: int


class _StubLlmClient(LlmClient):
    """Test-local deterministic LlmClient stub for contract tests."""

    def __init__(self, result: BaseModel | None = None) -> None:
        self.result = result
        self.invocations: list[tuple[str, str]] = []
        self.failure: BaseException | None = None

    async def _generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[Any],
        operation_name: str,
    ) -> Any:
        self.invocations.append((system_prompt, user_prompt))
        if self.failure is not None:
            raise self.failure
        assert self.result is not None
        return self.result


def _valid_keywords() -> dict[str, str]:
    return {
        "system_prompt": "Reply with the requested model only.",
        "user_prompt": "What is in this record?",
        "operation_name": "sources.describe_record",
    }


class TestGenerateStructured:
    """LLM-01/LLM-02: structured-output-only contract."""

    @pytest.mark.asyncio
    async def test_returns_requested_pydantic_model(self) -> None:
        client = _StubLlmClient(result=SentenceModel(text="hello world", words=2))
        output = await client.generate_structured(
            response_model=SentenceModel, **_valid_keywords()
        )
        assert isinstance(output, SentenceModel)
        assert output.text == "hello world"
        assert output.words == 2

    @pytest.mark.asyncio
    async def test_prompts_passed_through_as_plain_content(self) -> None:
        client = _StubLlmClient(result=SentenceModel(text="x", words=1))
        await client.generate_structured(
            response_model=SentenceModel, **_valid_keywords()
        )
        assert client.invocations == [
            ("Reply with the requested model only.", "What is in this record?")
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    async def test_blank_operation_name_rejected_before_call(self, blank: str) -> None:
        client = _StubLlmClient(result=SentenceModel(text="x", words=1))
        with pytest.raises(ValueError, match="must not be blank"):
            await client.generate_structured(
                response_model=SentenceModel,
                system_prompt="s",
                user_prompt="u",
                operation_name=blank,
            )
        assert client.invocations == []

    @pytest.mark.asyncio
    async def test_validate_operation_name_rejects_blank(self) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            validate_operation_name("  ")

    def test_validate_operation_name_rejects_overlong(self) -> None:
        with pytest.raises(ValueError, match="exceed"):
            validate_operation_name("x" * 201)


class TestLlmError:
    """LLM-03/LLM-04: typed, bounded, provider-neutral failures."""

    def test_stable_error_codes_exist(self) -> None:
        assert LlmErrorCode.TIMEOUT.value == "timeout"
        assert LlmErrorCode.PROVIDER_FAILURE.value == "provider_failure"
        assert (
            LlmErrorCode.INVALID_STRUCTURED_OUTPUT.value == "invalid_structured_output"
        )
        assert LlmErrorCode.CONFIGURATION_ERROR.value == "configuration_error"

    def test_error_carries_code_and_retryable_flag(self) -> None:
        error = LlmError(LlmErrorCode.TIMEOUT, retryable=False)
        assert error.code is LlmErrorCode.TIMEOUT
        assert error.retryable is False
        retryable = LlmError(LlmErrorCode.INVALID_STRUCTURED_OUTPUT, retryable=True)
        assert retryable.retryable is True

    def test_error_message_is_bounded_and_safe(self) -> None:
        error = LlmError(LlmErrorCode.PROVIDER_FAILURE)
        message = str(error)
        assert message == "LLM operation failed: provider_failure"
        # LLM-04: never echoes prompts, model output, or raw provider text.
        assert "prompt" not in message
        assert "hello world" not in message
        assert "traceback" not in message
        assert "secret" not in message


class TestCancellation:
    """LLM-05: asyncio.CancelledError propagates unchanged."""

    @pytest.mark.asyncio
    async def test_cancellation_from_stub_propagates(self) -> None:
        client = _StubLlmClient(result=SentenceModel(text="x", words=1))
        client.failure = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await client.generate_structured(
                response_model=SentenceModel, **_valid_keywords()
            )


class TestProviderNeutrality:
    """LLM-06: no provider-specific type in the app contract."""

    def test_no_provider_sdk_imports_in_contract_module(self) -> None:
        source = pathlib.Path(__file__).resolve().parents[3] / "src"
        module_path = source / "darkula" / "app" / "llm.py"
        text = module_path.read_text()
        for forbidden in ("aiokafka", "openai", "langchain", "anthropic", "boto3"):
            assert forbidden not in text, f"provider import leaked: {forbidden}"

    def test_contract_module_exports_only_darkula_types(self) -> None:
        from darkula.app import llm

        assert not hasattr(llm, "openai")
        assert not hasattr(llm, "langchain")
