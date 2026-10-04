# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic lifecycle tests for ``scripts/darkula_redpanda.sh``.

Runs the real shell script against a fake ``podman``/``ss`` earlier on
``PATH`` (I1-I10). The fake CLI records every requested operation and
statically enforces the Darkula boundary: any command naming a resource
outside the exact Darkula-owned set, or any broad discovery/prune command,
fails closed. The tests assert lifecycle policy (create-with-label, verify
before reuse/delete, fail closed on ambiguity, exact 39092:9092 mapping)
-- not Podman internals, and no live Podman daemon is required.
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
_SCRIPT = _REPO_ROOT / "scripts" / "darkula_redpanda.sh"

_CONTAINER = "darkula-redpanda"
_IMAGE = "docker.io/redpandadata/redpanda:v24.3.8"

_FAKE_PODMAN_SOURCE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    WORLD_PATH = os.environ["FAKE_PODMAN_WORLD"]
    LOG_PATH = os.environ["FAKE_PODMAN_LOG"]
    WORLD = json.load(open(WORLD_PATH))

    CONTAINER = "darkula-redpanda"
    IMAGE = "docker.io/redpandadata/redpanda:v24.3.8"

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

    def absent():
        return {"exists": False, "state": "exited", "labels": None, "rpk_ready": False}

    record()
    args = sys.argv[1:]
    if not args:
        die("no command")

    if args[0] == "container":
        if len(args) != 3 or args[1] != "exists" or args[2] != CONTAINER:
            die("unexpected container command: %r" % (args,))
        sys.exit(0 if WORLD["container"]["exists"] else 1)

    if args[0] == "image":
        if len(args) != 3:
            die("unexpected image command: %r" % (args,))
        sub, name = args[1], args[2]
        if name != IMAGE:
            die("foreign image name: %r" % (name,))
        if sub == "exists":
            sys.exit(0 if WORLD["image"] else 1)
        if sub == "pull":
            if not WORLD["image"]:
                WORLD["image"] = True
                save()
            sys.exit(0)
        die("unexpected image command: %r" % (args,))

    if args[0] == "inspect":
        if args[1] == "--format":
            if len(args) != 4 or args[3] != CONTAINER:
                die("unexpected inspect --format: %r" % (args,))
            if not WORLD["container"]["exists"]:
                sys.exit(1)
            print(WORLD["container"]["state"])
            sys.exit(0)
        if len(args) != 2 or args[1] != CONTAINER:
            die("unexpected inspect: %r" % (args,))
        if not WORLD["container"]["exists"]:
            sys.exit(1)
        if WORLD["container"].get("inspect") == "fail":
            sys.exit(2)
        if WORLD["container"].get("inspect") == "garbage":
            print("not-json")
            sys.exit(0)
        print(
            json.dumps(
                [{"Config": {"Labels": WORLD["container"]["labels"] or {}}}]
            )
        )
        sys.exit(0)

    if args[0] == "run":
        text = " ".join(args)
        required = [
            "-d",
            "--name " + CONTAINER,
            "--hostname " + CONTAINER,
            "--label darkula.owned=true",
            "--label darkula.service=redpanda",
            "-p 39092:9092",
            IMAGE,
            "redpanda start --overprovisioned",
            "--kafka-addr 0.0.0.0:9092",
            "--advertise-kafka-addr 127.0.0.1:39092",
        ]
        missing = [tok for tok in required if tok not in text]
        if missing:
            die("run missing expected tokens: %r" % (missing,))
        if args[-1] != "--advertise-kafka-addr":
            # last token is the advertised address value in our invocation
            pass
        if WORLD["container"]["exists"]:
            die("container already exists")
        WORLD["container"] = {
            "exists": True,
            "state": "running",
            "labels": {"darkula.owned": "true", "darkula.service": "redpanda"},
            "rpk_ready": True,
        }
        save()
        sys.exit(0)

    if args[0] == "start":
        if args != ["start", CONTAINER]:
            die("unexpected start: %r" % (args,))
        WORLD["container"]["state"] = "running"
        save()
        sys.exit(0)

    if args[0] == "stop":
        if args != ["stop", CONTAINER]:
            die("unexpected stop: %r" % (args,))
        WORLD["container"]["state"] = "exited"
        save()
        sys.exit(0)

    if args[0] == "rm":
        if args != ["rm", "-f", CONTAINER]:
            die("unexpected rm: %r" % (args,))
        WORLD["container"] = absent()
        save()
        sys.exit(0)

    if args[0] == "exec":
        if args[1] != CONTAINER or args[2] != "rpk":
            die("unexpected exec: %r" % (args,))
        sys.exit(0 if WORLD["container"].get("rpk_ready") else 1)

    die("broad or unknown podman command: %r" % (args,))
    """
)

_FAKE_SS_SOURCE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os

    WORLD = json.load(open(os.environ["FAKE_PODMAN_WORLD"]))
    for port in WORLD["ss_listeners"]:
        print("LISTEN 0 4096 127.0.0.1:%s 0.0.0.0:*" % port)
    """
)


def _world(
    *,
    container: dict[str, Any] | None = None,
    image: bool = True,
    ss_listeners: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "container": container
        or {"exists": False, "state": "exited", "labels": None, "rpk_ready": False},
        "image": image,
        "ss_listeners": list(ss_listeners),
    }


OWNED = {
    "exists": True,
    "state": "running",
    "labels": {"darkula.owned": "true", "darkula.service": "redpanda"},
    "rpk_ready": True,
}
UNLABELED = {"exists": True, "state": "running", "labels": {}, "rpk_ready": True}
WRONG_LABEL = {
    "exists": True,
    "state": "running",
    "labels": {"app": "other"},
    "rpk_ready": True,
}


class FakePodman:
    """Executes the real script with a fake podman/ss on PATH."""

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
        lines = [
            line
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        return [json.loads(line) for line in lines]


@pytest.fixture
def faker(tmp_path: Path) -> Iterator[Callable[[dict[str, Any]], FakePodman]]:
    """Install fake podman/ss shims and return a factory for world states."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, source in (("podman", _FAKE_PODMAN_SOURCE), ("ss", _FAKE_SS_SOURCE)):
        shim = bin_dir / name
        shim.write_text(source, encoding="utf-8")
        shim.chmod(0o755)

    def make(world: dict[str, Any]) -> FakePodman:
        return FakePodman(tmp_path, bin_dir, world)

    yield make


class TestStartProvisioning:
    """I1/I2: absent -> provision labeled; owned -> reuse, no mutation."""

    def test_start_provisions_labeled_exact_resource(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world())
        result = fake.run("start")
        assert result.returncode == 0, result.stderr
        world = fake.world()
        assert world["container"]["labels"] == {
            "darkula.owned": "true",
            "darkula.service": "redpanda",
        }
        run_call = next(c for c in fake.calls() if c[0] == "run")
        assert "-p" in run_call
        assert "39092:9092" in run_call
        assert _IMAGE in run_call

    def test_start_reuses_owned_container(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=OWNED))
        result = fake.run("start")
        assert result.returncode == 0, result.stderr
        assert "already running" in result.stdout
        assert [c for c in fake.calls() if c[0] == "run"] == []


class TestStartFailClosed:
    """I3/I4/I5/I9: refuse unverified resources or foreign port listeners."""

    def test_start_refuses_unlabeled_container(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=UNLABELED))
        result = fake.run("start")
        assert result.returncode != 0
        assert "NOT Darkula-owned" in result.stderr
        assert fake.world()["container"] == UNLABELED
        assert [c for c in fake.calls() if c[0] == "run"] == []

    def test_start_refuses_wrongly_labeled_container(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=WRONG_LABEL))
        result = fake.run("start")
        assert result.returncode != 0
        assert "NOT Darkula-owned" in result.stderr
        assert [c for c in fake.calls() if c[0] == "run"] == []

    def test_start_fails_closed_on_unverifiable_inspect(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        for mode in ("fail", "garbage"):
            container = dict(OWNED)
            container["inspect"] = mode
            fake = faker(_world(container=container))
            result = fake.run("start")
            assert result.returncode != 0
            assert "could not be verified" in result.stderr
            assert [c for c in fake.calls() if c[0] == "run"] == []

    def test_start_refuses_foreign_port_listener(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(ss_listeners=("39092",)))
        result = fake.run("start")
        assert result.returncode != 0
        assert "39092" in result.stderr
        assert "non-Darkula listener" in result.stderr
        assert fake.world()["container"]["exists"] is False
        assert [c for c in fake.calls() if c[0] == "run"] == []


class TestClean:
    """I6/I7/I10: preflight before removal; absent is a safe no-op."""

    def test_clean_removes_owned_container(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=OWNED))
        result = fake.run("clean")
        assert result.returncode == 0, result.stderr
        assert fake.world()["container"]["exists"] is False
        assert ["rm", "-f", "darkula-redpanda"] in fake.calls()

    def test_clean_refuses_unlabeled_container(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=UNLABELED))
        result = fake.run("clean")
        assert result.returncode != 0
        assert "refusing destructive cleanup" in result.stderr
        assert ["rm", "-f", "darkula-redpanda"] not in fake.calls()

    def test_clean_absent_is_safe_noop(self, faker: Callable[..., FakePodman]) -> None:
        fake = faker(_world())
        result = fake.run("clean")
        assert result.returncode == 0, result.stderr
        assert fake.calls() == [["container", "exists", "darkula-redpanda"]]
        assert fake.world() == _world()


class TestForeignResources:
    """I8: only the exact Darkula-owned set is ever referenced."""

    def test_only_exact_darkula_names_are_ever_referenced(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(ss_listeners=("8080", "9000")))
        result = fake.run("start")
        assert result.returncode == 0, result.stderr
        allowed = {_CONTAINER, _IMAGE}
        for call in fake.calls():
            referenced: set[str] = set()
            for token in call:
                if token == _IMAGE or _IMAGE.split(":")[0] in token:
                    referenced.add(_IMAGE)
                elif token.startswith("darkula-"):
                    referenced.add(token.split(":")[0].split("/")[0])
            assert referenced <= allowed
        clean = fake.run("clean")
        assert clean.returncode == 0, clean.stderr
        for call in fake.calls():
            referenced = set()
            for token in call:
                if token == _IMAGE:
                    referenced.add(_IMAGE)
                elif token.startswith("darkula-"):
                    referenced.add(token.split(":")[0].split("/")[0])
            assert referenced <= allowed


class TestInspectBoundary:
    """The fake itself fails closed on foreign/broad commands; a green run
    proves lifecycle policy never needed them."""

    def test_fake_rejects_foreign_resource_commands(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world())
        result = fake.run("start")
        assert result.returncode == 0, result.stderr
        run_call = next(c for c in fake.calls() if c[0] == "run")
        assert "--advertise-kafka-addr" in run_call
        assert run_call[-1] == "127.0.0.1:39092"
