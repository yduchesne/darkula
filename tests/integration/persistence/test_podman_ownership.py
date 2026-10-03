# SPDX-License-Identifier: AGPL-3.0-only
"""PR 5B integration regression: real Podman resources are Darkula-owned.

Runs under `./build.sh --intg` after `scripts/darkula_postgres.sh start`.
Asserts, using exact-resource inspection only, that the running container
and the data volume both carry `darkula.owned=true` and that the host
mapping remains 35432:5432. Persistence behavior is covered by the
migrations/transactions/vertical-slice suites.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, cast

import pytest

pytestmark = pytest.mark.integration

CONTAINER = "darkula-postgres"
VOLUME = "darkula-postgres-data"
HOST_PORT = 35432
CONTAINER_PORT = 5432
OWNER_LABEL = "darkula.owned"


def _podman(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["podman", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _container_inspect() -> dict[str, Any]:
    result = _podman("inspect", CONTAINER)
    return cast(dict[str, Any], json.loads(result.stdout)[0])


def _volume_inspect() -> dict[str, Any]:
    result = _podman("volume", "inspect", VOLUME)
    return cast(dict[str, Any], json.loads(result.stdout)[0])


@pytest.fixture(scope="module")
def podman_available() -> bool:
    return shutil.which("podman") is not None


class TestPodmanOwnership:
    """Exact-resource ownership labels after the Darkula start."""

    def test_container_is_owned(self, podman_available: bool) -> None:
        if not podman_available:
            pytest.skip("podman not available")
        data = _container_inspect()
        labels = data["Config"]["Labels"]
        assert labels.get(OWNER_LABEL) == "true"

    def test_volume_is_owned(self, podman_available: bool) -> None:
        if not podman_available:
            pytest.skip("podman not available")
        data = _volume_inspect()
        labels = data.get("Labels", data.get("labels", {})) or {}
        assert labels.get(OWNER_LABEL) == "true"

    def test_host_mapping_is_35432_to_5432(self, podman_available: bool) -> None:
        if not podman_available:
            pytest.skip("podman not available")
        data = _container_inspect()
        ports = data["NetworkSettings"]["Ports"]
        assert f"{CONTAINER_PORT}/tcp" in ports
        assert any(
            binding.get("HostPort") == str(HOST_PORT)
            for binding in ports[f"{CONTAINER_PORT}/tcp"]
        )
