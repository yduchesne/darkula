# SPDX-License-Identifier: AGPL-3.0-only
"""PR 20 static boundary guards (U20-48)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "darkula"
RUNTIME = ROOT / "crawler-runtime"

FORBIDDEN_CONTROLLER = {
    "httpx",
    "requests",
    "urllib.request",
    "socket",
    "playwright",
    "aiohttp",
}
FORBIDDEN_GATEWAY = {
    "playwright",
    "httpx",
    "requests",
    "aiohttp",
    "boto3",
    "psycopg",
    "aiokafka",
}
ALLOWED_GATEWAY_DARKULA = {
    "darkula.crawler",
    "darkula.crawler.public_https_policy",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_u20_48_controller_has_no_http_dns_or_browser_import() -> None:
    imports = _imports(SRC / "crawler" / "controller.py")
    for forbidden in FORBIDDEN_CONTROLLER:
        assert not any(
            name == forbidden or name.startswith(f"{forbidden}.") for name in imports
        ), forbidden


def test_u20_48_shared_policy_is_stdlib_only() -> None:
    imports = _imports(SRC / "crawler" / "public_https_policy.py")
    allowed = {"__future__", "ipaddress", "re", "string", "urllib.parse"}
    assert imports <= allowed


def test_u20_48_gateway_has_no_app_browser_or_cloud_import() -> None:
    for path in (RUNTIME / "https_egress").glob("*.py"):
        imports = _imports(path)
        for forbidden in FORBIDDEN_GATEWAY:
            assert not any(
                name == forbidden or name.startswith(f"{forbidden}.")
                for name in imports
            ), (path.name, forbidden)
        assert not any(
            name.startswith("darkula") and name not in ALLOWED_GATEWAY_DARKULA
            for name in imports
        ), path.name


def test_u20_48_runtime_modules_do_not_import_fake_world() -> None:
    guarded = (
        list((SRC / "crawler").glob("*.py"))
        + list((SRC / "sandbox").glob("*.py"))
        + list((SRC / "infrastructure" / "sandbox").glob("*.py"))
        + list((RUNTIME / "crawler_runtime").glob("*.py"))
        + list((RUNTIME / "https_egress").glob("*.py"))
    )
    for path in guarded:
        source = path.read_text(encoding="utf-8")
        assert "darkula.testing.fake_world" not in source, path


def test_u20_48_no_permissive_tls_in_live_runtime() -> None:
    for name in ("__main__.py", "live_policy.py", "playwright_adapter.py"):
        source = (RUNTIME / "crawler_runtime" / name).read_text(encoding="utf-8")
        assert "ignore_https_errors=True" not in source
        assert '"ignore_https_errors": True' not in source
        assert "--ignore-certificate-errors" not in source
