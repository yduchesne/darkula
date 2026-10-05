# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic lifecycle tests for ``scripts/darkula_observability.sh``.

Runs the real shell script against fake ``podman``/``ss`` shims on PATH. The
fake records calls and enforces the Darkula boundary (exact names, ownership
labels, pinned images, explicit ports, no broad/prune commands). No Podman
daemon, Collector, or network is required.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "darkula_observability.sh"

_NETWORK = "darkula-observability"
_COLLECTOR = "darkula-otel-collector"
_JAEGER = "darkula-jaeger"
_PROMETHEUS = "darkula-prometheus"
_CONTAINERS = (_COLLECTOR, _JAEGER, _PROMETHEUS)
_IMAGES = {
    "docker.io/otel/opentelemetry-collector-contrib:0.115.1",
    "docker.io/jaegertracing/all-in-one:1.62.0",
    "docker.io/prom/prometheus:v2.55.1",
}
_HEALTHY_ENV = "DARKULA_OBSERVABILITY_SKIP_READY_CHECK"

_FAKE_PODMAN_SOURCE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    WORLD_PATH = os.environ["FAKE_PODMAN_WORLD"]
    LOG_PATH = os.environ["FAKE_PODMAN_LOG"]
    WORLD = json.load(open(WORLD_PATH))

    NETWORK = "darkula-observability"
    CONTAINERS = ["darkula-otel-collector", "darkula-jaeger", "darkula-prometheus"]
    PINNED = {
        "docker.io/otel/opentelemetry-collector-contrib:0.115.1",
        "docker.io/jaegertracing/all-in-one:1.62.0",
        "docker.io/prom/prometheus:v2.55.1",
    }

    def save():
        with open(WORLD_PATH, "w") as fh:
            json.dump(WORLD, fh)

    def record():
        with open(LOG_PATH, "a") as fh:
            json.dump(sys.argv[1:], fh)
            fh.write("\\n")

    def die(msg):
        sys.stderr.write("fake-podman: " + msg + "\\n")
        sys.exit(2)

    record()
    args = sys.argv[1:]
    if not args:
        die("no command")

    if args[0] == "container":
        if len(args) != 3 or args[1] != "exists":
            die("unexpected container command: %r" % (args,))
        sys.exit(0 if args[2] in WORLD["containers"] else 1)

    if args[0] == "network":
        if args[1] == "exists":
            sys.exit(0 if WORLD["network"] else 1)
        if args[1] == "inspect":
            if not WORLD["network"]:
                sys.exit(1)
            print(json.dumps([{"labels": {"darkula.owned": "true"}}]))
            sys.exit(0)
        if args[1] == "create":
            text = " ".join(args)
            if "--label darkula.owned=true" not in text:
                die("network create missing owned label")
            WORLD["network"] = True
            save()
            sys.exit(0)
        if args[1] == "rm":
            WORLD["network"] = False
            save()
            sys.exit(0)
        die("unexpected network command: %r" % (args,))

    if args[0] == "image":
        if args[1] == "exists":
            sys.exit(0 if WORLD.get("image") else 1)
        if args[1] == "pull":
            if args[2] not in PINNED:
                die("pull of unpinned image: %r" % (args[2],))
            WORLD["image"] = True
            save()
            sys.exit(0)
        die("unexpected image command: %r" % (args,))

    if args[0] == "inspect":
        if args[1] == "--format":
            name = args[3]
            if name not in WORLD["containers"]:
                sys.exit(1)
            print(WORLD["containers"][name]["state"])
            sys.exit(0)
        name = args[1]
        if name not in WORLD["containers"]:
            sys.exit(1)
        print(json.dumps([{"Config": {"Labels": WORLD["containers"][name]["labels"]}}]))
        sys.exit(0)

    if args[0] == "run":
        text = " ".join(args)
        if "-d" not in args:
            die("run missing -d")
        name = args[args.index("--name") + 1]
        if name not in CONTAINERS:
            die("foreign container name: %r" % (name,))
        if name in WORLD["containers"]:
            die("container already exists")
        image = args[-1] if name == "darkula-jaeger" else None
        labels = {}
        for index, token in enumerate(args):
            if token == "--label":
                key, _, value = args[index + 1].partition("=")
                labels[key] = value
        if labels.get("darkula.owned") != "true":
            die("run missing owned label")
        for pinned in PINNED:
            if pinned in text:
                image = pinned
        if image is None or image not in PINNED:
            die("run uses unpinned or unknown image")
        WORLD["containers"][name] = {"state": "running", "labels": labels}
        save()
        sys.exit(0)

    if args[0] == "start":
        if args[1] not in WORLD["containers"]:
            sys.exit(1)
        WORLD["containers"][args[1]]["state"] = "running"
        save()
        sys.exit(0)

    if args[0] == "stop":
        if args[1] not in WORLD["containers"]:
            sys.exit(1)
        WORLD["containers"][args[1]]["state"] = "exited"
        save()
        sys.exit(0)

    if args[0] == "rm":
        if len(args) != 3 or args[1] != "-f":
            die("unexpected rm: %r" % (args,))
        WORLD["containers"].pop(args[2], None)
        save()
        sys.exit(0)

    if args[0] == "ps":
        sys.exit(0)

    die("broad or unknown podman command: %r" % (args,))
    """
)

_FAKE_SS_SOURCE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os

    WORLD = json.load(open(os.environ["FAKE_PODMAN_WORLD"]))
    for port in WORLD.get("listeners", []):
        print("LISTEN 0 4096 127.0.0.1:%s 0.0.0.0:*" % port)
    """
)


def _world(
    *,
    containers: dict[str, Any] | None = None,
    network: bool = False,
    image: bool = True,
    listeners: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "containers": containers or {},
        "network": network,
        "image": image,
        "listeners": list(listeners),
    }


def _owned(service: str) -> dict[str, Any]:
    return {
        "state": "running",
        "labels": {"darkula.owned": "true", "darkula.service": service},
    }


class FakePodman:
    def __init__(self, tmp_path: Path, bin_dir: Path, world: dict[str, Any]) -> None:
        self.world_path = tmp_path / "world.json"
        self.log_path = tmp_path / "calls.jsonl"
        self.bin_dir = bin_dir
        self.world_path.write_text(json.dumps(world), encoding="utf-8")
        self.log_path.write_text("", encoding="utf-8")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + os.pathsep + env["PATH"]
        env["FAKE_PODMAN_WORLD"] = str(self.world_path)
        env["FAKE_PODMAN_LOG"] = str(self.log_path)
        env[_HEALTHY_ENV] = "true"
        return env

    def run(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(_SCRIPT), *command],
            env=self._env(),
            capture_output=True,
            text=True,
        )

    def world(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.world_path.read_text()))

    def calls(self) -> list[list[str]]:
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]


@pytest.fixture
def faker(tmp_path: Path) -> Iterator[Callable[[dict[str, Any]], FakePodman]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, source in (("podman", _FAKE_PODMAN_SOURCE), ("ss", _FAKE_SS_SOURCE)):
        shim = bin_dir / name
        shim.write_text(source, encoding="utf-8")
        shim.chmod(0o755)

    def make(world: dict[str, Any]) -> FakePodman:
        return FakePodman(tmp_path, bin_dir, world)

    yield make


class TestStatus:
    def test_status_all_absent(self, faker: Callable[..., FakePodman]) -> None:
        result = faker(_world()).run("status")
        assert result.returncode == 0
        assert "network absent" in result.stdout
        assert "darkula-otel-collector absent" in result.stdout


class TestStart:
    def test_start_provisions_owned_pinned_resources(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world())
        result = fake.run("start")
        assert result.returncode == 0, result.stderr
        world = fake.world()
        assert set(world["containers"]) == set(_CONTAINERS)
        for service, name in (
            ("otel-collector", _COLLECTOR),
            ("jaeger", _JAEGER),
            ("prometheus", _PROMETHEUS),
        ):
            assert world["containers"][name]["labels"]["darkula.owned"] == "true"
            assert world["containers"][name]["labels"]["darkula.service"] == service
        run_calls = [call for call in fake.calls() if call[0] == "run"]
        joined = [" ".join(call) for call in run_calls]
        assert any("34318:4318" in text for text in joined)
        assert any("31686:16686" in text for text in joined)
        assert any("39090:9090" in text for text in joined)
        for text in joined:
            assert ":latest" not in text

    def test_start_reuses_owned_and_does_not_reprovision(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        world = _world(
            network=True,
            containers={
                _COLLECTOR: _owned("otel-collector"),
                _JAEGER: _owned("jaeger"),
                _PROMETHEUS: _owned("prometheus"),
            },
        )
        fake = faker(world)
        result = fake.run("start")
        assert result.returncode == 0, result.stderr
        assert [call for call in fake.calls() if call[0] == "run"] == []

    def test_start_refuses_foreign_unlabeled_container(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        foreign = {"state": "running", "labels": {}}
        fake = faker(_world(containers={_COLLECTOR: foreign}))
        result = fake.run("start")
        assert result.returncode != 0
        assert "NOT Darkula-owned" in result.stderr
        assert [call for call in fake.calls() if call[0] == "run"] == []

    def test_start_fails_closed_on_occupied_port(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(listeners=("34318",)))
        result = fake.run("start")
        assert result.returncode != 0
        assert "occupied by a non-Darkula listener" in result.stderr
        assert [call for call in fake.calls() if call[0] == "run"] == []


class TestClean:
    def test_clean_removes_owned_resources(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        world = _world(
            network=True,
            containers={
                _COLLECTOR: _owned("otel-collector"),
                _JAEGER: _owned("jaeger"),
                _PROMETHEUS: _owned("prometheus"),
            },
        )
        fake = faker(world)
        result = fake.run("clean")
        assert result.returncode == 0, result.stderr
        after = fake.world()
        assert after["containers"] == {}
        assert after["network"] is False

    def test_clean_absent_is_safe_noop(self, faker: Callable[..., FakePodman]) -> None:
        fake = faker(_world())
        result = fake.run("clean")
        assert result.returncode == 0
        assert [call for call in fake.calls() if call[0] == "rm"] == []

    def test_clean_refuses_foreign_container(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        foreign = {"state": "running", "labels": {"app": "other"}}
        fake = faker(_world(containers={_COLLECTOR: foreign}))
        result = fake.run("clean")
        assert result.returncode != 0
        assert "NOT Darkula-owned" in result.stderr
        assert _COLLECTOR in fake.world()["containers"]
