# SPDX-License-Identifier: AGPL-3.0-only
"""Static architecture/security guards for PR 13 relationship extraction.

Proves that the production relationship application/domain modules never
import provider/network/browser/sandbox/DataStream/recon/Fake-World-truth
boundaries, contain no SQL, and use only static developer-controlled telemetry
attributes. Also pins the immutability of migrations 0001-0006 and forbids
graph/global-entity/source-analysis classes.
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

_RELATIONSHIP_MODULES = (
    _SRC / "app" / "relationship_extraction.py",
    _SRC / "domain" / "relationships.py",
)

_FORBIDDEN_IMPORTS: dict[str, str] = {
    "darkula.app.recon": "relationship extraction is not reconnaissance",
    "darkula.app.recon_agent": "relationship extraction is not agentic",
    "darkula.app.collection": "relationship extraction is not collection",
    "darkula.app.data_stream": "no DataStream in extraction",
    "darkula.app.source": "no SourceAnalyst/assessment behavior",
    "darkula.infrastructure": "application/domain must stay provider-neutral",
    "darkula.testing.fake_world": "production never imports Fake World truth",
    "darkula.crawler": "no crawler",
    "darkula.sandbox": "no sandbox",
    "openai": "no provider SDK",
    "langchain": "no LangChain",
    "anthropic": "no provider SDK",
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
        "RelationshipObservation",
        "RelationshipEvolution",
        "SourceAssessment",
        "SourceAnalyst",
    }
)

#: SHA-256 of the immutable 0001-0006 migration artifacts.
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
}


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


def _violations(path: Path) -> list[str]:
    found: list[str] = []
    for imported in _imports(path):
        for prefix, reason in _FORBIDDEN_IMPORTS.items():
            if imported == prefix or imported.startswith(prefix + "."):
                found.append(f"{imported}: {reason}")
    return found


class TestRelationshipImportGuards:
    @pytest.mark.parametrize("path", _RELATIONSHIP_MODULES, ids=lambda p: p.name)
    def test_rts1_rts6_no_forbidden_boundaries(self, path: Path) -> None:
        assert _violations(path) == []

    def test_rts2_only_darkula_llm_client(self) -> None:
        names = _imports(_SRC / "app" / "relationship_extraction.py")
        assert "darkula.app.llm" in names
        assert all(not name.startswith("openai") for name in names)

    @pytest.mark.parametrize("path", _RELATIONSHIP_MODULES, ids=lambda p: p.name)
    def test_rts7_no_sql_in_application_or_domain(self, path: Path) -> None:
        assert _SQL_PATTERN.search(path.read_text(encoding="utf-8")) is None

    def test_rts4_no_source_analyst(self) -> None:
        source = (_SRC / "app" / "relationship_extraction.py").read_text(
            encoding="utf-8"
        )
        assert "SourceAnalyst" not in source
        assert "SourceAssessment" not in source

    def test_rts5_no_datastream(self) -> None:
        source = (_SRC / "app" / "relationship_extraction.py").read_text(
            encoding="utf-8"
        )
        assert "DataStream" not in source


class TestRelationshipRepositoryBoundary:
    def test_rts8_repository_uses_versioned_functions_only(self) -> None:
        from tests.unit.infrastructure.persistence.postgresql.test_sql_boundary import (
            validate_module_source,
        )

        path = (
            _SRC
            / "infrastructure"
            / "persistence"
            / "postgresql"
            / "relationship_repository.py"
        )
        assert validate_module_source(path.read_text(encoding="utf-8")) == []


class TestRelationshipTelemetryExclusions:
    @pytest.mark.parametrize("path", _RELATIONSHIP_MODULES, ids=lambda p: p.name)
    def test_rts9_telemetry_uses_only_static_attributes(self, path: Path) -> None:
        assert "attributes=" not in path.read_text(encoding="utf-8")

    def test_rts10_telemetry_names_are_static_and_content_free(self) -> None:
        source = (_SRC / "app" / "relationship_extraction.py").read_text(
            encoding="utf-8"
        )
        names = re.findall(r'metric="([^"]+)"', source) + re.findall(
            r'span_name="([^"]+)"', source
        )
        assert names
        assert all(re.fullmatch(r"[a-z0-9_.]+", name) for name in names)
        assert all(
            name.startswith("darkula.relationship_extraction.")
            or name == "relationship_extraction.extract"
            for name in names
        )


class TestRelationshipScopeGuards:
    def test_rts11_migrations_0001_to_0006_unchanged(self) -> None:
        for filename, expected in _FROZEN_MIGRATION_HASHES.items():
            digest = hashlib.sha256((_MIGRATIONS / filename).read_bytes()).hexdigest()
            assert digest == expected, f"{filename} was modified"

    def test_rts12_no_graph_or_global_entity_classes(self) -> None:
        for path in _RELATIONSHIP_MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defined = {
                node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
            }
            assert defined.isdisjoint(_FORBIDDEN_CLASS_NAMES)

    def test_no_global_relationship_table_in_migration(self) -> None:
        migration = (_MIGRATIONS / "0007_relationships.sql").read_text(encoding="utf-8")
        lowered = migration.lower()
        for forbidden in (
            "global_entity",
            "canonical_entity",
            "graph_node",
            "graph_edge",
            "relationship_evolution",
            "source_assessment",
        ):
            assert forbidden not in lowered
