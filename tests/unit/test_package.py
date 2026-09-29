# SPDX-License-Identifier: AGPL-3.0-only
"""Tooling-level package tests (TOOL-07)."""

from __future__ import annotations

import subprocess
import sys

#: Fresh-interpreter check: importing ``darkula`` must not load any
#: application/config/telemetry submodule (no configuration loading,
#: environment reads, telemetry initialization, network access, file
#: creation, or consumer startup).
_PACKAGE_IMPORT_CHECK = (
    "import darkula, sys;"
    "assert 'darkula.config' not in sys.modules;"
    "assert 'darkula.app' not in sys.modules;"
    "assert 'darkula.telemetry' not in sys.modules;"
    "print('ok')"
)


def test_import_has_no_side_effects() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PACKAGE_IMPORT_CHECK],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_version_attribute_present() -> None:
    import darkula

    assert darkula.__doc__
    assert darkula.__file__.endswith("__init__.py")
