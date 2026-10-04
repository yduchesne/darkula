# SPDX-License-Identifier: AGPL-3.0-only
"""V5: real Redpanda ownership regression against real Podman.

Runs under `./build.sh --intg` after `scripts/darkula_redpanda.sh start`.
Asserts, using exact-resource inspection only, that the running container
carries `darkula.owned=true` and that the host mapping remains
39092:9092. The PostgreSQL container/volume ownership regression lives in
tests/integration/persistence/test_podman_ownership.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, cast

import pytest

pytestmark = pytest.mark.integration

CONTAINER = "darkula-redpanda"
HOST_PORT = 39092
CONTAINER_PORT = 9092
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


@pytest.fixture(scope="module")
def podman_available() -> bool:
    return shutil.which("podman") is not None


class TestRedpandaOwnership:
    """Exact-resource ownership labels and port mapping after start."""

    def test_container_is_owned(self, podman_available: bool) -> None:
        if not podman_available:
            pytest.skip("podman not available")
        data = _container_inspect()
        labels = data["Config"]["Labels"]
        assert labels.get(OWNER_LABEL) == "true"

    def test_service_label_is_redpanda(self, podman_available: bool) -> None:
        if not podman_available:
            pytest.skip("podman not available")
        data = _container_inspect()
        labels = data["Config"]["Labels"]
        assert labels.get("darkula.service") == "redpanda"

    def test_host_mapping_is_39092_to_9092(self, podman_available: bool) -> None:
        if not podman_available:
            pytest.skip("podman not available")
        data = _container_inspect()
        ports = data["NetworkSettings"]["Ports"]
        assert f"{CONTAINER_PORT}/tcp" in ports
        assert any(
            binding.get("HostPort") == str(HOST_PORT)
            for binding in ports[f"{CONTAINER_PORT}/tcp"]
        )
