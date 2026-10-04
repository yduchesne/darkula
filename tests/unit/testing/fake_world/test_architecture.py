# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Fake World architecture/import-direction guards (PR 6) — matrix FW9.

Enforces:

- production packages (``darkula.domain``/``darkula.app``/``darkula.infrastructure``
  and the composition root) never import the Fake World or ``FakeWorldTruth``;
- the Fake World never imports production domain/application/infrastructure
  packages (no reverse leakage of Darkula architecture into the test world);
- importing production packages pulls no Fake World code into ``sys.modules``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_SRC = _PROJECT_ROOT / "src"
_FAKE_WORLD_PACKAGE = "darkula.testing.fake_world"
_PRODUCTION_PACKAGES = (
    "darkula.domain",
    "darkula.app",
    "darkula.infrastructure",
    "darkula.composition",
    "darkula.config",
    "darkula.telemetry",
)


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


class TestProductionNeverImportsFakeWorld:
    """FW9 — truth never enters the production pipeline."""

    def _production_files(self) -> list[Path]:
        return [
            path
            for path in _iter_python_files(_SRC / "darkula")
            if not path.is_relative_to(_SRC / "darkula" / "testing")
        ]

    def test_no_production_module_imports_fake_world(self) -> None:
        offenders: list[str] = []
        for path in self._production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for child in ast.walk(tree):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    names: list[str] = []
                    if isinstance(child, ast.Import):
                        names = [a.name for a in child.names]
                    elif child.module:
                        names = [child.module]
                    if any(
                        name == _FAKE_WORLD_PACKAGE
                        or name.startswith(_FAKE_WORLD_PACKAGE + ".")
                        for name in names
                    ):
                        offenders.append(f"{path.relative_to(_SRC)}: {names}")
        assert not offenders, f"production code imports Fake World: {offenders}"

    def test_no_production_module_mentions_fake_world_truth(self) -> None:
        offenders: list[str] = []
        for path in self._production_files():
            text = path.read_text(encoding="utf-8")
            if "FakeWorldTruth" in text or "fake_world" in text:
                offenders.append(str(path.relative_to(_SRC)))
        assert not offenders, f"production code mentions Fake World truth: {offenders}"

    def test_fresh_interpreter_imports_no_fake_world(self) -> None:
        # Production modules may legitimately import the PR 3 fakes
        # (FakeLlmClient/FakeDataStream) through composition, but never the
        # Fake World package or its truth.
        check = (
            "import sys;"
            "import darkula.domain, darkula.app, darkula.infrastructure;"
            "import darkula.composition, darkula.config;"
            "assert 'darkula.testing.fake_world' not in sys.modules;"
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", check],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"

    def test_truth_symbols_live_only_in_testing_namespace(self) -> None:
        # The truth module defines the FakeWorldTruth classes; nothing else
        # below src/darkula may export them.
        import darkula.testing.fake_world as fake_world
        import darkula.testing.fake_world.truth as truth

        assert hasattr(truth, "FakeWorldTruth")
        assert fake_world.FakeWorldTruth is truth.FakeWorldTruth


class TestFakeWorldNeverImportsProduction:
    """Reverse direction: the test world stays independent of Darkula."""

    def test_fake_world_imports_only_stdlib_and_sibling_modules(self) -> None:
        package = _SRC / "darkula" / "testing" / "fake_world"
        offenders: list[str] = []
        for path in _iter_python_files(package):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for child in ast.walk(tree):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        if (
                            alias.name == "darkula" or alias.name.startswith("darkula.")
                        ) and not alias.name.startswith("darkula.testing.fake_world"):
                            offenders.append(f"{path.relative_to(_SRC)}: {alias.name}")
                elif isinstance(child, ast.ImportFrom) and child.module:
                    module = child.module
                    if (module == "darkula" or module.startswith("darkula.")) and not (
                        module.startswith("darkula.testing.fake_world")
                        or module == "darkula.testing.fake_world"
                    ):
                        offenders.append(f"{path.relative_to(_SRC)}: {module}")
        assert not offenders, f"Fake World imports production code: {offenders}"

    def test_fake_world_rendering_imports_no_truth(self) -> None:
        rendering = _SRC / "darkula" / "testing" / "fake_world" / "rendering.py"
        tree = ast.parse(rendering.read_text(encoding="utf-8"))
        for child in ast.walk(tree):
            if isinstance(child, ast.ImportFrom) and child.module:
                assert "truth" not in child.module, child.lineno
                assert "faketruth" not in (child.module or "")


class TestNoNetworkOrFilesystemDependencies:
    def test_renderer_source_has_no_socket_or_fs_imports(self) -> None:
        path = _SRC / "darkula" / "testing" / "fake_world" / "rendering.py"
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        module_names: set[str] = set()
        for child in ast.walk(tree):
            if isinstance(child, ast.Import):
                module_names.update(a.name for a in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module:
                module_names.add(child.module)
        for name in sorted(module_names):
            top = name.split(".")[0]
            if top == "darkula":
                assert name.startswith("darkula.testing.fake_world"), (
                    f"renderer imports non-Fake-World darkula module: {name}"
                )
            else:
                assert top in sys.stdlib_module_names, (
                    f"renderer imports non-stdlib module: {name}"
                )
        # no live network/file access, period
        assert "urlopen(" not in text
        assert "tempfile" not in text
        assert "open(" not in text
        assert "Path(" not in text

    def test_fake_world_has_no_llm_datastream_objectstore_deps(self) -> None:
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in _iter_python_files(_SRC / "darkula" / "testing" / "fake_world")
        )
        for forbidden in ("FakeLlmClient", "FakeDataStream", "InMemoryObjectStore"):
            assert forbidden not in text, forbidden
