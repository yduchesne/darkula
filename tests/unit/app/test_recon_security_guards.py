# SPDX-License-Identifier: AGPL-3.0-only
"""Static architecture guards for the PR 10 recon application layer.

Section 13 (TS matrix) of the PR 10 plan requires that the recon
application layer:

- never instantiates Playwright/Podman or runs shell commands (TS6);
- never reaches PostgreSQL adapters or SQL (TS7);
- never imports provider SDKs (TS10/LA10);
- never imports Fake World truth; and
- the crawler runtime stays free of LLM/agent imports (TS8).

These positive import guards make the structural discipline explicit and
machine-checked, mirroring the PR 9 collection guards.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "darkula"
_RUNTIME = _REPO_ROOT / "crawler-runtime"

#: Modules the recon app layer must never import. Keys are exact top-level
#: dotted module prefixes; values are the documented guard reason.
_FORBIDDEN_APP_IMPORTS: dict[str, str] = {
    "darkula.infrastructure.sandbox": (
        "recon orchestrates the Crawler; the sandbox is the PR 7 controller "
        "boundary and must never be constructed here"
    ),
    "darkula.infrastructure.persistence.postgresql": (
        "recon reaches persistence only through DarkulaSpi/UnitOfWork"
    ),
    "darkula.infrastructure.object_store": (
        "recon evidence is in-memory bounded observations; artifacts are "
        "never read or written here"
    ),
    "darkula.infrastructure.data_stream": (
        "recon is a synchronous Coordinator workflow; no DataStream use (PR 10)"
    ),
    "darkula.testing.fake_world": ("production packages never import Fake World truth"),
    "playwright": "recon never instantiates a browser",
    "podman": "recon never runs podman directly",
    "openai": "provider SDKs never appear in the application layer",
    "langchain": "provider SDKs never appear in the application layer",
    "anthropic": "provider SDKs never appear in the application layer",
    "langsmith": "agent observability adapters are PR 15 and never import here",
    "langfuse": "agent observability adapters are PR 15 and never import here",
}

_RECON_MODULES = ("recon.py", "recon_agent.py")


class TestReconImportGuards:
    """TS6/TS7/TS10: recon app modules stay boundary-clean statically."""

    @pytest.mark.parametrize("module", _RECON_MODULES)
    def test_recon_modules_never_import_forbidden_boundaries(self, module: str) -> None:
        path = _SRC / "app" / module
        assert path.exists(), f"missing module {path}"
        violations = _find_violations(path, _FORBIDDEN_APP_IMPORTS)
        assert violations == [], f"{module} imports forbidden boundaries: {violations}"

    def test_adapter_is_the_only_openai_importer_in_src(self) -> None:
        importers: list[str] = []
        for path in _SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "import openai" in text or "from openai" in text:
                importers.append(str(path.relative_to(_SRC)))
        assert importers == ["infrastructure/llm/openai.py"]

    def test_no_sql_in_recon_application_modules(self) -> None:
        for module in _RECON_MODULES:
            text = (_SRC / "app" / module).read_text(encoding="utf-8")
            assert "SELECT" not in text, f"{module} must never contain SQL"
            assert "INSERT" not in text and "UPDATE" not in text


class TestCrawlerRuntimeStaysAgentFree:
    """TS8: the crawler runtime never imports LLM/agent/recon code."""

    @pytest.mark.parametrize(
        "path",
        sorted((_RUNTIME / "crawler_runtime").glob("*.py"))
        if (_RUNTIME / "crawler_runtime").exists()
        else [],
    )
    def test_runtime_never_imports_llm_or_agent_boundaries(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "darkula.app.llm",
            "darkula.app.recon",
            "darkula.app.recon_agent",
            "darkula.app.agent_observability",
            "openai",
            "langchain",
        ):
            assert forbidden not in text, (
                f"{path.name} imports forbidden boundary {forbidden}"
            )

    def test_runtime_directory_has_modules(self) -> None:
        runtime_dir = _RUNTIME / "crawler_runtime"
        assert runtime_dir.is_dir(), "crawler runtime directory is missing"
        assert list(runtime_dir.glob("*.py")), "crawler runtime has no python modules"


def _find_violations(path: Path, forbidden: dict[str, str]) -> list[str]:
    """Return every forbidden import found in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            imports.append(node.module)
    violations: list[str] = []
    for imp in imports:
        for forbidden_name, reason in forbidden.items():
            if imp == forbidden_name or imp.startswith(forbidden_name + "."):
                violations.append(f"{imp}: {reason}")
    return violations
