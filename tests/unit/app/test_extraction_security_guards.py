# SPDX-License-Identifier: AGPL-3.0-only
"""Static architecture/security guards for PR 11 deterministic extraction.

Proves that the production extraction application/domain modules never import
LLM/provider/network/browser/sandbox/DataStream/Fake-World boundaries and
contain no SQL. Telemetry uses only static developer-controlled attributes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "darkula"

_EXTRACTION_MODULES = (
    _SRC / "app" / "extraction.py",
    _SRC / "app" / "extractors.py",
    _SRC / "domain" / "extraction.py",
)

_FORBIDDEN_IMPORTS: dict[str, str] = {
    "darkula.app.llm": "deterministic extraction is LLM-free",
    "darkula.app.recon": "deterministic extraction is not reconnaissance",
    "darkula.app.recon_agent": "deterministic extraction is not agentic",
    "darkula.infrastructure": "application extraction must stay provider-neutral",
    "darkula.testing.fake_world": "production never imports Fake World truth",
    "openai": "no provider SDK",
    "langchain": "no LangChain",
    "anthropic": "no provider SDK",
    "playwright": "no browser",
    "podman": "no sandbox",
    "aiokafka": "Kafka/Redpanda types never leak into application code",
    "boto3": "S3/R2 SDKs never appear in the application layer",
    "botocore": "S3/R2 SDKs never appear in the application layer",
    "requests": "no synchronous network client",
    "httpx": "no network client",
    "aiohttp": "no network client",
}

_SQL_PATTERN = re.compile(
    r"\b(SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
    r"DELETE\s+FROM|CREATE\s+TABLE|DROP\s+TABLE)\b",
    re.IGNORECASE,
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            names.append(node.module)
    return names


class TestExtractionImportGuards:
    @pytest.mark.parametrize("path", _EXTRACTION_MODULES, ids=lambda p: p.name)
    def test_ts6_no_forbidden_boundaries(self, path: Path) -> None:
        violations: list[str] = []
        for imported in _imports(path):
            for forbidden, reason in _FORBIDDEN_IMPORTS.items():
                if imported == forbidden or imported.startswith(forbidden + "."):
                    violations.append(f"{imported}: {reason}")
        assert violations == [], (
            f"{path.name} imports forbidden boundaries: {violations}"
        )

    @pytest.mark.parametrize("path", _EXTRACTION_MODULES, ids=lambda p: p.name)
    def test_ts7_no_sql_in_application_or_domain(self, path: Path) -> None:
        assert _SQL_PATTERN.search(path.read_text(encoding="utf-8")) is None

    def test_ts8_no_network_clients_in_extractors(self) -> None:
        forbidden = ("socket", "urllib.request", "http.client", "subprocess")
        names = _imports(_SRC / "app" / "extractors.py")
        assert not any(
            name == item or name.startswith(item)
            for name in names
            for item in forbidden
        )


class TestExtractionTelemetryExclusions:
    def test_ts1_ts2_telemetry_uses_only_static_attributes(self) -> None:
        source = (_SRC / "app" / "extraction.py").read_text(encoding="utf-8")
        # Decorators carry only bounded static metric/span names.
        assert "attributes=" not in source

    def test_ts5_no_values_or_identifiers_in_telemetry_names(self) -> None:
        source = (_SRC / "app" / "extraction.py").read_text(encoding="utf-8")
        names = re.findall(r'metric="([^"]+)"', source) + re.findall(
            r'span_name="([^"]+)"', source
        )
        assert names
        assert all(re.fullmatch(r"[a-z0-9_.]+", name) for name in names)
