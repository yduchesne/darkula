# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""LangSmith agent-observability adapter tests (PR 15) — matrix AO15.

Uses a fake SDK boundary: no network, no LangSmith client is constructed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import pytest

from darkula.app.agent_observability import AgentOperationMetadata
from darkula.infrastructure.observability.langsmith import (
    LangSmithAgentObservability,
    LangSmithRun,
    build_langsmith_run_factory,
)


class FakeRun:
    def __init__(
        self, *, name: str, metadata: dict[str, Any], fail_on: str | None = None
    ) -> None:
        self.name = name
        self.metadata = metadata
        self.fail_on = fail_on
        self.posted = False
        self.ended = False
        self.patched = False

    def post(self, *, exclude_child_runs: bool = True) -> None:
        if self.fail_on == "post":
            raise RuntimeError("post boom")
        self.posted = True

    def end(
        self,
        *,
        end_time: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self.fail_on == "end":
            raise RuntimeError("end boom")
        self.ended = True

    def patch(self, *, exclude_inputs: bool | None = None) -> None:
        if self.fail_on == "patch":
            raise RuntimeError("patch boom")
        self.patched = True


class FakeFactory:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.runs: list[FakeRun] = []
        self.fail_on = fail_on

    def __call__(
        self, *, name: str, metadata: Mapping[str, Any], start_time: datetime
    ) -> LangSmithRun:
        self.calls.append({"name": name, "metadata": dict(metadata)})
        if self.fail_on == "factory":
            raise RuntimeError("factory boom")
        run = FakeRun(name=name, metadata=dict(metadata), fail_on=self.fail_on)
        self.runs.append(run)
        return run


def _metadata() -> AgentOperationMetadata:
    return AgentOperationMetadata(
        operation_name="recon.assess",
        agent_name="recon_agent",
        model_provider="openai",
        model_name="gpt-4o",
        model_profile="recon",
        prompt_version="recon-v1",
    )


class TestSafeMetadata:
    """AO15-1..AO15-3, AO15-12."""

    def test_ao15_1_only_safe_metadata_sent(self) -> None:
        factory = FakeFactory()
        observability = LangSmithAgentObservability(run_factory=factory)
        with observability.operation(metadata=_metadata()):
            pass
        assert factory.calls[0]["name"] == "recon.assess"
        metadata = factory.calls[0]["metadata"]
        assert metadata["darkula.operation_name"] == "recon.assess"
        assert metadata["darkula.agent_name"] == "recon_agent"
        assert metadata["darkula.model_provider"] == "openai"

    def test_ao15_2_prompt_unrepresentable(self) -> None:
        with pytest.raises(TypeError):
            AgentOperationMetadata(  # type: ignore[call-arg]
                operation_name="recon.assess", system_prompt="secret"
            )

    def test_ao15_3_output_unrepresentable(self) -> None:
        with pytest.raises(TypeError):
            AgentOperationMetadata(  # type: ignore[call-arg]
                operation_name="recon.assess", model_output="secret"
            )

    def test_ao15_12_metadata_bounds(self) -> None:
        with pytest.raises(ValueError):
            AgentOperationMetadata(operation_name="x" * 500)


class TestFailOpen:
    """AO15-4..AO15-7."""

    @pytest.mark.parametrize("fail_on", ["factory", "post", "end", "patch"])
    def test_ao15_4_5_backend_failures_fail_open(self, fail_on: str) -> None:
        factory = FakeFactory(fail_on=fail_on)
        observability = LangSmithAgentObservability(run_factory=factory)
        # Must not raise.
        with observability.operation(metadata=_metadata()):
            pass

    def test_ao15_6_application_exception_unchanged(self) -> None:
        factory = FakeFactory(fail_on="end")
        observability = LangSmithAgentObservability(run_factory=factory)
        with (
            pytest.raises(ValueError, match="app boom"),
            observability.operation(metadata=_metadata()),
        ):
            raise ValueError("app boom")

    def test_ao15_7_cancellation_unchanged(self) -> None:
        factory = FakeFactory()
        observability = LangSmithAgentObservability(run_factory=factory)
        with (
            pytest.raises(asyncio.CancelledError),
            observability.operation(metadata=_metadata()),
        ):
            raise asyncio.CancelledError

    def test_flush_fail_open(self) -> None:
        def boom() -> None:
            raise RuntimeError("flush boom")

        observability = LangSmithAgentObservability(
            run_factory=FakeFactory(), flush=boom
        )
        observability.flush()
        LangSmithAgentObservability(run_factory=FakeFactory()).flush()

    def test_successful_lifecycle_posts_ends_patches(self) -> None:
        factory = FakeFactory()
        observability = LangSmithAgentObservability(run_factory=factory)
        with observability.operation(metadata=_metadata()):
            pass
        run = factory.runs[0]
        assert run.posted and run.ended and run.patched


class TestFactory:
    """AO15-8/10: real factory imports the SDK only inside infrastructure."""

    def test_build_langsmith_run_factory_constructs_client(self) -> None:
        factory = build_langsmith_run_factory(
            project="darkula-eval", api_key="fake-key"
        )
        # Constructing the factory is offline; invoking it builds a RunTree.
        run = factory(
            name="op",
            metadata={"darkula.operation_name": "op"},
            start_time=datetime.now().astimezone(),
        )
        assert hasattr(run, "post")

    def test_ao15_8_langsmith_import_is_infrastructure_only(self) -> None:
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[4] / "src" / "darkula"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            if "infrastructure/observability/langsmith.py" in path.as_posix():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for child in ast.walk(tree):
                if isinstance(child, ast.Import):
                    names = [alias.name for alias in child.names]
                elif isinstance(child, ast.ImportFrom) and child.module:
                    names = [child.module]
                else:
                    continue
                if any(
                    name == "langsmith" or name.startswith("langsmith.")
                    for name in names
                ):
                    offenders.append(str(path.relative_to(src)))
        assert not offenders, f"LangSmith imported outside adapter: {offenders}"
