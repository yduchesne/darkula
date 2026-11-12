# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic tests for ``install.sh`` (Podman bootstrap + mock mode).

The real installer runs against fake ``uv``/``podman``/``sudo``/``apt-get``/
``uname`` shims earlier on ``PATH``. No Podman daemon, package manager, or
network is required, and every fake mutating tool records invocations so the
tests can prove mock mode executes nothing.

Covered behavior:

- ``--help`` documents the supported options;
- ``--mock`` prints the Podman/uv/pre-commit plan and executes no mutating
  command (no package install, no ``uv sync``, no hook install);
- mock mode never fails when Podman is absent, and still verifies a present
  Podman;
- a too-old Podman fails closed with a bounded message;
- the real (non-mock) path installs Podman through the package manager,
  re-scans ``PATH`` and verifies the resulting binary;
- ``--no-podman`` skips Podman entirely for Python-toolchain-only hosts;
- unknown options are rejected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "install.sh"

_UV_SHIM = """\
#!/usr/bin/env python3
import os
import sys

with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")

if sys.argv[1:2] == ["--version"]:
    print("uv " + os.environ.get("FAKE_UV_VERSION", "0.13.7"))
sys.exit(0)
"""

_PODMAN_SHIM = """\
#!/usr/bin/env python3
import os
import sys

with open(os.environ["FAKE_PODMAN_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")

if sys.argv[1:] == ["--version"]:
    print("podman version " + os.environ.get("FAKE_PODMAN_VERSION", "5.4.1"))
sys.exit(0)
"""

_SUDO_SHIM = """\
#!/usr/bin/env python3
import os
import sys

with open(os.environ["FAKE_SUDO_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")

os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
"""

_APT_GET_SHIM = """\
#!/usr/bin/env python3
import os
import sys

with open(os.environ["FAKE_APT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")

if sys.argv[1:2] == ["install"]:
    target = os.path.join(os.environ["FAKE_BIN_DIR"], "podman")
    if not os.path.exists(target):
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/usr/bin/env python3\\n"
                "import sys\\n"
                "print('podman version 5.4.1')\\n"
                "sys.exit(0)\\n"
            )
        os.chmod(target, 0o755)
sys.exit(0)
"""

_UNAME_SHIM = """\
#!/usr/bin/env python3
import sys

if sys.argv[1:2] == ["-s"]:
    print("Linux")
sys.exit(0)
"""

_GIT_SHIM = """\
#!/usr/bin/env python3
import sys

sys.exit(0)
"""

#: Real tools the installer needs, linked into the isolated ``sysbin`` so the
#: sandbox never sees host package managers or a host ``podman``.
_REAL_TOOLS = ("bash", "python3", "dirname", "awk", "sort", "head", "cat")


def _write_shim(path: Path, body: str) -> None:
    """Write one executable fake CLI."""
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _link_real(sysbin: Path, tool: str) -> None:
    """Link one required real tool into the isolated ``PATH``."""
    real = shutil.which(tool)
    if real is not None:
        os.symlink(real, sysbin / tool)


class _Installer:
    """Runs the real installer against isolated fake commands."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        self.sysbin = tmp_path / "sysbin"
        self.sysbin.mkdir()
        for tool in _REAL_TOOLS:
            _link_real(self.sysbin, tool)
        self.log_dir = tmp_path / "logs"
        self.log_dir.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self._env = dict(os.environ)
        self._env.update(
            {
                # Only the fake commands and a minimal real tool set: the host
                # podman/package managers must never be visible.
                "PATH": str(self.bin_dir) + os.pathsep + str(self.sysbin),
                "HOME": str(self.home),
                "FAKE_BIN_DIR": str(self.bin_dir),
                "FAKE_UV_LOG": str(self.log_dir / "uv.log"),
                "FAKE_PODMAN_LOG": str(self.log_dir / "podman.log"),
                "FAKE_SUDO_LOG": str(self.log_dir / "sudo.log"),
                "FAKE_APT_LOG": str(self.log_dir / "apt.log"),
            }
        )
        _write_shim(self.bin_dir / "uv", _UV_SHIM)
        _write_shim(self.bin_dir / "sudo", _SUDO_SHIM)
        _write_shim(self.bin_dir / "apt-get", _APT_GET_SHIM)
        _write_shim(self.bin_dir / "uname", _UNAME_SHIM)
        _write_shim(self.bin_dir / "git", _GIT_SHIM)

    def add_podman(self, version: str = "5.4.1") -> None:
        """Install the fake podman CLI at the requested version."""
        self._env["FAKE_PODMAN_VERSION"] = version
        _write_shim(self.bin_dir / "podman", _PODMAN_SHIM)

    def log(self, name: str) -> str:
        """Return one fake command's recorded invocations (empty if unused)."""
        path = self.log_dir / f"{name}.log"
        return "" if not path.exists() else path.read_text(encoding="utf-8")

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Execute the real installer with the isolated environment."""
        return subprocess.run(
            [str(self.sysbin / "bash"), str(_SCRIPT), *arguments],
            cwd=_REPO_ROOT,
            env=self._env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


@pytest.fixture
def installer(tmp_path: Path) -> Iterator[_Installer]:
    """Return an installer sandbox with fake commands and empty logs."""
    yield _Installer(tmp_path)


class TestOptions:
    def test_help_documents_options(self, installer: _Installer) -> None:
        result = installer.run("--help")
        assert result.returncode == 0
        assert "--mock" in result.stdout
        assert "--no-podman" in result.stdout
        assert installer.log("uv") == ""

    def test_unknown_option_is_rejected(self, installer: _Installer) -> None:
        result = installer.run("--not-an-option")
        assert result.returncode == 2
        assert "unknown option" in result.stderr


class TestMockMode:
    def test_mock_plans_podman_install_without_executing(
        self, installer: _Installer
    ) -> None:
        result = installer.run("--mock")
        assert result.returncode == 0, result.stderr
        assert "mock mode mandated" in result.stdout
        assert "[mock] sudo apt-get update" in result.stdout
        assert "[mock] sudo apt-get install -y podman" in result.stdout
        assert "[mock] uv sync --locked" in result.stdout
        assert "[mock] uv run pre-commit install" in result.stdout
        assert "no installation, synchronization, or hook changes" in result.stdout
        # Nothing mutating executed: no package manager, no privilege
        # escalation, no environment sync, no hook installation.
        assert installer.log("apt") == ""
        assert installer.log("sudo") == ""
        assert "sync" not in installer.log("uv")
        assert "pre-commit" not in installer.log("uv")

    def test_mock_verifies_present_podman(self, installer: _Installer) -> None:
        installer.add_podman("5.4.1")
        result = installer.run("--mock")
        assert result.returncode == 0, result.stderr
        assert "== podman 5.4.1 ==" in result.stdout
        assert "apt-get" not in result.stdout
        assert installer.log("apt") == ""

    def test_mock_accepts_help_alias(self, installer: _Installer) -> None:
        result = installer.run("-h")
        assert result.returncode == 0
        assert "Usage:" in result.stdout


class TestPodmanVersionGate:
    def test_too_old_podman_fails_closed(self, installer: _Installer) -> None:
        installer.add_podman("3.4.2")
        result = installer.run()
        assert result.returncode == 1
        assert "podman >= 4.0.0 is required; found 3.4.2" in result.stderr
        assert "sync" not in installer.log("uv")


class TestRealInstallationPath:
    def test_missing_podman_installs_via_package_manager(
        self, installer: _Installer
    ) -> None:
        result = installer.run()
        assert result.returncode == 0, result.stderr
        assert "podman not found: installing" in result.stdout
        assert "== podman 5.4.1 ==" in result.stdout
        assert "update" in installer.log("apt")
        assert "install -y podman" in installer.log("apt")
        assert "apt-get update" in installer.log("sudo")
        # The locked environment and hooks are installed only after Podman.
        assert "sync --locked" in installer.log("uv")
        assert "run pre-commit install" in installer.log("uv")

    def test_no_podman_skips_installation(self, installer: _Installer) -> None:
        result = installer.run("--no-podman")
        assert result.returncode == 0, result.stderr
        assert "podman installation skipped (--no-podman)" in result.stdout
        assert installer.log("apt") == ""
        assert installer.log("sudo") == ""
        assert "sync --locked" in installer.log("uv")
