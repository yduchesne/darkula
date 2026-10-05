# SPDX-License-Identifier: AGPL-3.0-only
"""Static architecture/security guards for PR 11/PR 12 extraction.

Proves that the production deterministic/semantic extraction and geographic
resolution application/domain modules never import LLM-provider, network,
browser, sandbox, DataStream, ReconAgent, or Fake-World-truth boundaries and
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
    _SRC / "app" / "canonical_input.py",
    _SRC / "domain" / "extraction.py",
)

_SEMANTIC_GEOGRAPHY_MODULES = (
    _SRC / "app" / "semantic_extraction.py",
    _SRC / "app" / "geography.py",
    _SRC / "domain" / "geography.py",
)

_FORBIDDEN_IMPORTS: dict[str, str] = {
    "darkula.app.llm": "deterministic extraction is LLM-free",
    "darkula.app.recon": "extraction is not reconnaissance",
    "darkula.app.recon_agent": "extraction is not agentic",
    "darkula.infrastructure": "application extraction must stay provider-neutral",
    "darkula.testing.fake_world": "production never imports Fake World truth",
    "openai": "no provider SDK",
    "langchain": "no LangChain",
    "anthropic": "no provider SDK",
    "deepagents": "no agent framework",
    "playwright": "no browser",
    "podman": "no sandbox",
    "aiokafka": "Kafka/Redpanda types never leak into application code",
    "boto3": "S3/R2 SDKs never appear in the application layer",
    "botocore": "S3/R2 SDKs never appear in the application layer",
    "googlemaps": "no concrete geocoder SDK",
    "geopy": "no concrete geocoder SDK",
    "nominatim": "no concrete geocoder SDK",
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


def _violations(path: Path, *, allow_llm: bool) -> list[str]:
    forbidden = dict(_FORBIDDEN_IMPORTS)
    if allow_llm:
        forbidden.pop("darkula.app.llm", None)
    found: list[str] = []
    for imported in _imports(path):
        for prefix, reason in forbidden.items():
            if imported == prefix or imported.startswith(prefix + "."):
                found.append(f"{imported}: {reason}")
    return found


class TestExtractionImportGuards:
    @pytest.mark.parametrize("path", _EXTRACTION_MODULES, ids=lambda p: p.name)
    def test_ts6_no_forbidden_boundaries(self, path: Path) -> None:
        assert _violations(path, allow_llm=False) == []

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


class TestSemanticGeographyImportGuards:
    @pytest.mark.parametrize("path", _SEMANTIC_GEOGRAPHY_MODULES, ids=lambda p: p.name)
    def test_ts12_1_no_provider_or_forbidden_boundaries(self, path: Path) -> None:
        # The semantic extractor is allowed to depend on the Darkula LlmClient
        # contract; geography is not (it uses the provider-neutral SPI).
        allow_llm = path.name == "semantic_extraction.py"
        violations = _violations(path, allow_llm=allow_llm)
        if not allow_llm:
            for imported in _imports(path):
                if imported == "darkula.app.llm" or imported.startswith(
                    "darkula.app.llm."
                ):
                    violations.append("darkula.app.llm: geography must not use an LLM")
        assert violations == [], (
            f"{path.name} imports forbidden boundaries: {violations}"
        )

    @pytest.mark.parametrize("path", _SEMANTIC_GEOGRAPHY_MODULES, ids=lambda p: p.name)
    def test_ts12_4_no_sql(self, path: Path) -> None:
        assert _SQL_PATTERN.search(path.read_text(encoding="utf-8")) is None

    def test_ts12_3_no_crawler_or_sandbox_imports(self) -> None:
        for path in _SEMANTIC_GEOGRAPHY_MODULES:
            names = _imports(path)
            assert not any(
                name == "darkula.crawler" or name.startswith("darkula.crawler.")
                for name in names
            )
            assert not any(
                name == "darkula.sandbox" or name.startswith("darkula.sandbox.")
                for name in names
            )

    def test_ts12_10_semantic_extraction_does_not_use_recon(self) -> None:
        names = _imports(_SRC / "app" / "semantic_extraction.py")
        assert not any(name.startswith("darkula.app.recon") for name in names)


class TestExtractionTelemetryExclusions:
    @pytest.mark.parametrize(
        "path",
        (
            _SRC / "app" / "extraction.py",
            _SRC / "app" / "semantic_extraction.py",
            _SRC / "app" / "geography.py",
        ),
        ids=lambda p: p.name,
    )
    def test_ts1_ts2_telemetry_uses_only_static_attributes(self, path: Path) -> None:
        # Decorators carry only bounded static metric/span names.
        assert "attributes=" not in path.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "path",
        (
            _SRC / "app" / "semantic_extraction.py",
            _SRC / "app" / "geography.py",
        ),
        ids=lambda p: p.name,
    )
    def test_ts12_7_no_values_in_telemetry_names(self, path: Path) -> None:
        source = path.read_text(encoding="utf-8")
        names = re.findall(r'metric="([^"]+)"', source) + re.findall(
            r'span_name="([^"]+)"', source
        )
        assert names
        assert all(re.fullmatch(r"[a-z0-9_.]+", name) for name in names)
