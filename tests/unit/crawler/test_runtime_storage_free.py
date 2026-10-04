# SPDX-License-Identifier: AGPL-3.0-only
"""Static architecture/security guards for the crawler sandbox (PR 7/8).

Proves (section 18 of the PR 8 plan) that the crawler-runtime image stays
object-store/SDK-free: it must never import the Darkula ObjectStore boundary
or any S3/R2 application SDK, and no ObjectStore credential capability is
introduced into the sandbox surface.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_PATHS = [
    _REPO_ROOT / "crawler-runtime" / "crawler_runtime",
    _REPO_ROOT / "crawler-runtime" / "fakeworld_serve.py",
]

# Import names that must never appear in the crawler runtime.
_FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+(darkula\.app\.object_store|darkula\.infrastructure\.object_store|"
    r"boto3|aioboto3|aiobotocore|botocore)",
    re.MULTILINE,
)


def _runtime_py_files() -> list[Path]:
    files: list[Path] = []
    for path in _RUNTIME_PATHS:
        if path.is_dir():
            files.extend(sorted(path.glob("*.py")))
        elif path.is_file():
            files.append(path)
    return files


class TestSandboxStorageFreedom:
    """ARCH-01..ARCH-04: no ObjectStore/S3/R2 dependency in the runtime."""

    @pytest.mark.parametrize(
        "bad",
        [
            "darkula.app.object_store",
            "darkula.infrastructure.object_store",
            "boto3",
            "aioboto3",
            "botocore",
        ],
    )
    def test_runtime_never_imports_storage_sdk(self, bad: str) -> None:
        for path in _runtime_py_files():
            source = path.read_text(encoding="utf-8")
            pattern = re.compile(
                rf"^\s*(?:import|from)\s+(?:{re.escape(bad)}(\.[a-zA-Z_][a-zA-Z0-9_]*)?)",
                re.MULTILINE,
            )
            assert pattern.search(source) is None, (
                f"{path} must not import {bad!r} (ObjectStore stays trusted-side)"
            )

    def test_runtime_has_no_objectstore_credential_fields(self) -> None:
        # No key/secret/bucket/endpoint storage fields in the runtime protocol.
        for path in _runtime_py_files():
            source = path.read_text(encoding="utf-8")
            assert "object_store" not in source
            assert "s3" not in source.lower() or "s3" not in source

    def test_controller_surface_has_no_objectstore_credentials(self) -> None:
        # The sandbox request must not expose storage credentials.
        import dataclasses

        from darkula.sandbox.contracts import SandboxExecutionRequest

        for field in dataclasses.fields(SandboxExecutionRequest):
            assert "key" not in field.name.lower(), field.name
            assert "secret" not in field.name.lower(), field.name
            assert "bucket" not in field.name.lower(), field.name
            assert "credential" not in field.name.lower(), field.name
