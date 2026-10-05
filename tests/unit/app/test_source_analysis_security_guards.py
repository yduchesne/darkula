# SPDX-License-Identifier: AGPL-3.0-only
"""Static architecture/security guards for PR 14 source analysis.

Proves that the production source-analysis modules never import
provider/network/browser/sandbox/DataStream/recon/Fake-World-truth boundaries,
contain no SQL, and use only static developer-controlled telemetry attributes.
The analyst module specifically must never receive persistence, ObjectStore,
DataStream, collection execution, or recon capability. Also pins migrations
0001-0007 byte-identical and forbids global graph/truth classes.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "darkula"
_MIGRATIONS = _REPO_ROOT / "migrations"

_SERVICE_MODULE = _SRC / "app" / "source_analysis.py"
_ANALYST_MODULE = _SRC / "app" / "source_analyst.py"

_SERVICE_FORBIDDEN: dict[str, str] = {
    "darkula.app.recon": "source analysis is not reconnaissance",
    "darkula.app.recon_agent": "source analysis is not agentic",
    "darkula.app.data_stream": "no DataStream in source analysis",
    "darkula.app.object_store": "no ObjectStore in source analysis",
    "darkula.crawler": "no crawler in source analysis",
    "darkula.sandbox": "no sandbox in source analysis",
    "darkula.infrastructure": "application must stay provider-neutral",
    "darkula.testing.fake_world": "production never imports Fake World truth",
    "openai": "no provider SDK",
    "anthropic": "no provider SDK",
    "langchain": "no LangChain",
    "deepagents": "no agent framework",
    "playwright": "no browser",
    "podman": "no sandbox",
    "aiokafka": "Kafka/Redpanda types never leak into application code",
    "boto3": "S3/R2 SDKs never appear in the application layer",
    "botocore": "S3/R2 SDKs never appear in the application layer",
    "requests": "no synchronous network client",
    "httpx": "no network client",
    "aiohttp": "no network client",
}

_ANALYST_FORBIDDEN: dict[str, str] = {
    **_SERVICE_FORBIDDEN,
    "darkula.app.persistence": "the analyst has no persistence capability",
    "darkula.app.collection": "the analyst has no collection capability",
    "darkula.app.repositories": "the analyst has no repository capability",
    "darkula.app.outbox": "the analyst has no persistence capability",
}

_SQL_PATTERN = re.compile(
    r"\b(SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
    r"DELETE\s+FROM|CREATE\s+TABLE|DROP\s+TABLE)\b",
    re.IGNORECASE,
)

_FORBIDDEN_CLASS_NAMES = frozenset(
    {
        "GlobalEntity",
        "CanonicalEntity",
        "GlobalRelationship",
        "GraphNode",
        "GraphEdge",
        "RelationshipEvolution",
    }
)

_FROZEN_MIGRATION_HASHES = {
    "0001_initial.sql": (
        "819cffa93c9410beaa37046e0666b51a9e05bf9414a648102b71aab061e7c13b"
    ),
    "0002_message_outbox.sql": (
        "d6f42fe749b6a75b91074e795e751aabfec7fdd1cec371129747ec3e28e60fe4"
    ),
    "0003_content.sql": (
        "43f851daa0bd9d1aa03d34d3c9cfdc6fe5d8ab4ea09c28c636d568db984c48ac"
    ),
    "0004_collection.sql": (
        "928c2df91d09b1880ad97e20172062832cda547bf90e53bd1b673d21c8d1894c"
    ),
    "0005_extraction.sql": (
        "ca3159c602fd32aebc21d41aa1905df0fa67e6ad8a225325cc7f12e356493e79"
    ),
    "0006_semantic_geography.sql": (
        "7fdd38fc7a6773533589dc57d336143f8fad38713ab324b8b395088ead298aab"
    ),
    "0007_relationships.sql": (
        "75a267313dcc2dc5c92e593502d23c149b6ac96f42cab6464c31b0cb58604dfb"
    ),
}


def _imports(path: Path) -> list[str]:
    """Return runtime module imports, ignoring ``TYPE_CHECKING``-only imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    checked_nodes: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            for child in ast.walk(node):
                checked_nodes.add(id(child))
    for node in ast.walk(tree):
        if id(node) in checked_nodes:
            continue
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            names.append(node.module)
    return names


def _violations(path: Path, forbidden: dict[str, str]) -> list[str]:
    found: list[str] = []
    for imported in _imports(path):
        for prefix, reason in forbidden.items():
            if imported == prefix or imported.startswith(prefix + "."):
                found.append(f"{imported}: {reason}")
    return found


class TestImportGuards:
    def test_sa_service_no_forbidden_boundaries(self) -> None:
        assert _violations(_SERVICE_MODULE, _SERVICE_FORBIDDEN) == []

    def test_analyst_no_persistence_or_collection(self) -> None:
        assert _violations(_ANALYST_MODULE, _ANALYST_FORBIDDEN) == []

    def test_analyst_uses_only_darkula_llm(self) -> None:
        names = _imports(_ANALYST_MODULE)
        assert "darkula.app.llm" in names
        assert all(not name.startswith("openai") for name in names)

    @pytest.mark.parametrize(
        "path", [_SERVICE_MODULE, _ANALYST_MODULE], ids=lambda p: p.name
    )
    def test_no_sql_in_application(self, path: Path) -> None:
        assert _SQL_PATTERN.search(path.read_text(encoding="utf-8")) is None

    def test_source_repository_uses_versioned_functions_only(self) -> None:
        from tests.unit.infrastructure.persistence.postgresql.test_sql_boundary import (
            validate_module_source,
        )

        path = (
            _SRC
            / "infrastructure"
            / "persistence"
            / "postgresql"
            / "source_repository.py"
        )
        assert validate_module_source(path.read_text(encoding="utf-8")) == []

    def test_collection_repository_uses_versioned_functions_only(self) -> None:
        from tests.unit.infrastructure.persistence.postgresql.test_sql_boundary import (
            validate_module_source,
        )

        path = (
            _SRC
            / "infrastructure"
            / "persistence"
            / "postgresql"
            / "collection_repository.py"
        )
        assert validate_module_source(path.read_text(encoding="utf-8")) == []

    def test_content_repository_uses_versioned_functions_only(self) -> None:
        from tests.unit.infrastructure.persistence.postgresql.test_sql_boundary import (
            validate_module_source,
        )

        path = (
            _SRC
            / "infrastructure"
            / "persistence"
            / "postgresql"
            / "content_repository.py"
        )
        assert validate_module_source(path.read_text(encoding="utf-8")) == []


class TestScopeGuards:
    def test_migrations_0001_to_0007_unchanged(self) -> None:
        for filename, expected in _FROZEN_MIGRATION_HASHES.items():
            digest = hashlib.sha256((_MIGRATIONS / filename).read_bytes()).hexdigest()
            assert digest == expected, f"{filename} was modified"

    @pytest.mark.parametrize(
        "path", [_SERVICE_MODULE, _ANALYST_MODULE], ids=lambda p: p.name
    )
    def test_no_global_graph_or_entity_classes(self, path: Path) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        }
        assert defined.isdisjoint(_FORBIDDEN_CLASS_NAMES)

    def test_analysis_does_not_mutate_source_status(self) -> None:
        assert "SourceStatus" not in _SERVICE_MODULE.read_text(encoding="utf-8")
        assert "SourceStatus" not in _ANALYST_MODULE.read_text(encoding="utf-8")

    def test_no_fake_world_truth_import(self) -> None:
        for path in (_SERVICE_MODULE, _ANALYST_MODULE):
            assert "darkula.testing.fake_world" not in _imports(path)

    def test_analysis_produces_no_global_truth_table(self) -> None:
        migration = (_MIGRATIONS / "0008_source_analysis.sql").read_text(
            encoding="utf-8"
        )
        lowered = migration.lower()
        for forbidden in (
            "global_entity",
            "canonical_entity",
            "graph_node",
            "graph_edge",
        ):
            assert forbidden not in lowered


class TestTelemetryExclusions:
    @pytest.mark.parametrize(
        "path", [_SERVICE_MODULE, _ANALYST_MODULE], ids=lambda p: p.name
    )
    def test_only_static_telemetry_attributes(self, path: Path) -> None:
        assert "attributes=" not in path.read_text(encoding="utf-8")

    def test_metric_and_span_names_are_static_and_content_free(self) -> None:
        source = _SERVICE_MODULE.read_text(encoding="utf-8")
        metrics = re.findall(r'"(darkula\.[a-z0-9_.]+)"', source)
        assert metrics
        assert all(name.startswith("darkula.source_analysis.") for name in metrics)
        assert '"source_analysis.analyze"' in source
