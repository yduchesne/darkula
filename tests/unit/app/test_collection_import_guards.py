# SPDX-License-Identifier: AGPL-3.0-only
"""Static architecture guards for the PR 9 collection application layer.

Section 19 of the PR 9 plan requires that collection orchestration never
instantiates Podman/Playwright or reaches ObjectStore directly, and that the
sandbox remains free of DB/stream/store/LLM credentials. These positive
import guards make that structural discipline explicit and machine-checked.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP = _REPO_ROOT / "src" / "darkula" / "app"

#: Modules the collection app layer must never import (keys are the exact
#: top-level dotted module prefixes; values are the documented guard reason).
_FORBIDDEN_IMPORTS: dict[str, str] = {
    "darkula.infrastructure.sandbox": (
        "collection orchestrates the Crawler; the sandbox is the PR 7 "
        "controller boundary and must never be constructed here"
    ),
    "darkula.infrastructure.object_store": (
        "artifacts are written only through ContentIngestService (PR 8)"
    ),
    "darkula.infrastructure.data_stream.redpanda": (
        "the DataStream boundary is the only transport interface"
    ),
    "darkula.testing.fake_world": ("production packages never import Fake World truth"),
    "aiokafka": "Kafka/Redpanda types never leak into application code",
    "boto3": "S3/R2 SDKs never appear in the application layer",
    "botocore": "S3/R2 SDKs never appear in the application layer",
    "playwright": "collection never instantiates a browser",
    "podman": "collection never runs podman directly",
}

_COLLECTION_MODULES = (
    "collection.py",
    "collection_scheduler.py",
    "collection_worker.py",
)


class TestCollectionImportGuards:
    """Collection app modules stay boundary-clean statically."""

    @pytest.mark.parametrize("module", _COLLECTION_MODULES)
    def test_collection_modules_never_import_forbidden_boundaries(
        self, module: str
    ) -> None:
        path = _APP / module
        assert path.exists(), f"missing module {path}"
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
            for forbidden, reason in _FORBIDDEN_IMPORTS.items():
                if imp == forbidden or imp.startswith(forbidden + "."):
                    violations.append(f"{imp}: {reason}")
        assert violations == [], f"{module} imports forbidden boundaries: {violations}"
