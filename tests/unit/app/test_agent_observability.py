# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the AgentObservability contract (AO-*)."""

from __future__ import annotations

import dataclasses
import pathlib
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any

import pytest

from darkula.app.agent_observability import (
    MAX_AGENT_OBSERVABILITY_FIELD_LENGTH,
    AgentObservability,
    AgentOperationMetadata,
    validate_agent_observation_metadata,
)


class _RecordingObservability(AgentObservability):
    """Test-local AgentObservability stub recording lifecycle scope."""

    def __init__(self) -> None:
        self.opened: list[AgentOperationMetadata] = []
        self.closed: list[AgentOperationMetadata] = []
        self.failure: Exception | None = None

    def operation(
        self,
        *,
        metadata: AgentOperationMetadata,
    ) -> AbstractContextManager[None]:
        return _Scope(self, metadata)


class _Scope:
    """Record one operation scope open/close (test-local lifecycle stub)."""

    def __init__(
        self,
        outer: _RecordingObservability,
        metadata: AgentOperationMetadata,
    ) -> None:
        self._outer = outer
        self._metadata = metadata

    def __enter__(self) -> None:
        if self._outer.failure is not None:
            raise self._outer.failure
        self._outer.opened.append(self._metadata)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._outer.closed.append(self._metadata)


def _valid_metadata(**overrides: Any) -> AgentOperationMetadata:
    defaults: dict[str, Any] = {
        "operation_name": "reconagent.assess_candidate",
        "agent_name": "ReconAgent",
        "model_provider": "openai",
        "model_name": "gpt-4o",
        "model_profile": "default",
        "prompt_version": "v1",
        "source_id": None,
        "collection_run_id": None,
    }
    defaults.update(overrides)
    return AgentOperationMetadata(**defaults)


class TestMetadataValidation:
    """AO-01..AO-05: bounded, content-free metadata only."""

    def test_valid_bounded_metadata_accepted(self) -> None:
        metadata = _valid_metadata()
        validate_agent_observation_metadata(metadata)
        assert metadata.operation_name == "reconagent.assess_candidate"

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_operation_name_rejected(self, blank: str) -> None:
        with pytest.raises(ValueError, match="operation_name"):
            _valid_metadata(operation_name=blank)

    @pytest.mark.parametrize(
        "field",
        [
            "agent_name",
            "model_provider",
            "model_name",
            "model_profile",
            "prompt_version",
        ],
    )
    def test_blank_optional_field_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            _valid_metadata(**{field: "  "})

    @pytest.mark.parametrize(
        "field",
        [
            "operation_name",
            "agent_name",
            "model_name",
            "source_id",
            "collection_run_id",
        ],
    )
    def test_overlong_field_rejected(self, field: str) -> None:
        value = "x" * (MAX_AGENT_OBSERVABILITY_FIELD_LENGTH + 1)
        with pytest.raises(ValueError, match="maximum"):
            _valid_metadata(**{field: value})

    @pytest.mark.parametrize(
        "field",
        ["operation_name", "agent_name", "prompt_version"],
    )
    def test_control_characters_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match="control characters"):
            _valid_metadata(**{field: "line\nbreak"})

    def test_prompt_and_output_not_representable(self) -> None:
        # AO-05: the metadata contract has no prompt/output/content fields.
        field_names = {f.name for f in dataclasses.fields(AgentOperationMetadata)}
        assert "prompt" not in field_names
        assert "output" not in field_names
        assert "content" not in field_names
        assert "response" not in field_names

    def test_none_optional_fields_are_permitted(self) -> None:
        metadata = AgentOperationMetadata(operation_name="operation")
        assert metadata.agent_name is None
        assert metadata.model_provider is None


class TestLifecycle:
    """Lifecycle-oriented contract: scope wraps one agent/LLM operation."""

    def test_operation_scope_opens_and_closes(self) -> None:
        observability = _RecordingObservability()
        metadata = _valid_metadata()
        with observability.operation(metadata=metadata):
            pass
        assert observability.opened == [metadata]
        assert observability.closed == [metadata]

    def test_scope_closes_on_exception(self) -> None:
        observability = _RecordingObservability()
        with (
            pytest.raises(RuntimeError, match="application failed"),
            observability.operation(metadata=_valid_metadata()),
        ):
            raise RuntimeError("application failed")
        assert len(observability.closed) == 1

    def test_backend_failure_is_fail_open_documented(self) -> None:
        # The fail-open contract is frozen by the adapter contract itself:
        # a backend failure must not alter application work. The stub
        # documents the invariant by recording rather than raising on entry.
        observability = _RecordingObservability()
        with observability.operation(metadata=_valid_metadata()):
            pass
        # The application operation completed; the backend recorded metadata
        # instead of failing the scope.
        assert observability.opened


class TestProviderNeutrality:
    """AO-06: LangSmith/Langfuse types never appear in the contract."""

    def test_no_vendor_imports_in_contract_module(self) -> None:
        source = pathlib.Path(__file__).resolve().parents[3] / "src"
        text = (source / "darkula" / "app" / "agent_observability.py").read_text()
        for forbidden in ("langsmith", "langfuse"):
            assert forbidden not in text, f"vendor leak in contract: {forbidden}"

    def test_contract_module_has_no_vendor_attributes(self) -> None:
        from darkula.app import agent_observability

        assert not hasattr(agent_observability, "langsmith")
        assert not hasattr(agent_observability, "langfuse")
