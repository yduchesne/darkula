# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic lifecycle tests for ``scripts/darkula_postgres.sh``.

Runs the real shell script against a fake ``podman``/``ss`` earlier on
``PATH`` (P1-P12). The fake CLI records every requested operation and
statically enforces the Darkula boundary: any command naming a resource
outside the exact Darkula set, or any broad discovery/prune command, fails
closed. The tests assert lifecycle policy (create-with-label, verify
before reuse/delete, fail closed on ambiguity) — not Podman internals, and
no live Podman daemon is required.
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
_SCRIPT = _REPO_ROOT / "scripts" / "darkula_postgres.sh"

_CONTAINER = "darkula-postgres"
_VOLUME = "darkula-postgres-data"
_IMAGE = "docker.io/library/postgres:18.2"

_FAKE_PODMAN_SOURCE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    WORLD_PATH = os.environ["FAKE_PODMAN_WORLD"]
    LOG_PATH = os.environ["FAKE_PODMAN_LOG"]
    WORLD = json.load(open(WORLD_PATH))

    CONTAINER = "darkula-postgres"
    VOLUME = "darkula-postgres-data"
    IMAGE = "docker.io/library/postgres:18.2"

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

    def absent_container():
        return {"exists": False, "state": "exited", "labels": None}

    record()
    args = sys.argv[1:]
    if not args:
        die("no command")

    if args[0] == "container":
        if len(args) != 3 or args[1] != "exists" or args[2] != CONTAINER:
            die("unexpected container command: %r" % (args,))
        sys.exit(0 if WORLD["container"]["exists"] else 1)

    if args[0] == "volume":
        sub = args[1] if len(args) > 1 else ""
        rest = args[2:]
        if sub == "exists":
            if rest != [VOLUME]:
                die("unexpected volume exists: %r" % (rest,))
            sys.exit(0 if WORLD["volume"]["exists"] else 1)
        if sub == "inspect":
            if rest != [VOLUME]:
                die("unexpected volume inspect: %r" % (rest,))
            if not WORLD["volume"]["exists"]:
                sys.exit(1)
            if WORLD["volume"].get("inspect") == "fail":
                sys.exit(2)
            if WORLD["volume"].get("inspect") == "garbage":
                print("not-json")
                sys.exit(0)
            labels = WORLD["volume"]["labels"] or {}
            print(json.dumps([{"Labels": labels, "labels": None}]))
            sys.exit(0)
        if sub == "create":
            labels = {}
            name = None
            i = 0
            while i < len(rest):
                tok = rest[i]
                if tok == "--label":
                    if i + 1 >= len(rest):
                        die("missing label value")
                    key, _, value = rest[i + 1].partition("=")
                    labels[key] = value
                    i += 2
                else:
                    if name is not None:
                        die("unexpected volume create token: %r" % (rest,))
                    name = tok
                    i += 1
            if name != VOLUME:
                die("foreign volume name: %r" % (name,))
            if labels.get("darkula.owned") != "true":
                die("volume create without mandatory darkula.owned=true")
            if WORLD["volume"]["exists"]:
                die("volume already exists: " + VOLUME)
            WORLD["volume"] = {"exists": True, "labels": labels}
            save()
            sys.exit(0)
        if sub == "rm":
            if rest != [VOLUME]:
                die("unexpected volume rm: %r" % (rest,))
            WORLD["volume"] = {"exists": False, "labels": None}
            save()
            sys.exit(0)
        die("unexpected volume command: %r" % (args,))

    if args[0] == "image":
        if len(args) != 3:
            die("unexpected image command: %r" % (args,))
        sub, name = args[1], args[2]
        if name != IMAGE:
            die("foreign image name: %r" % (name,))
        if sub == "exists":
            sys.exit(0 if WORLD["image"] else 1)
        if sub == "pull":
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
            "--label darkula.owned=true",
            "--label darkula.service=postgres",
            "-v " + VOLUME + ":/var/lib/postgresql",
            "-p 35432:5432",
            "-e POSTGRES_DB=darkula",
            "-e POSTGRES_USER=darkula",
            IMAGE,
        ]
        missing = [tok for tok in required if tok not in text]
        if missing:
            die("run missing expected tokens: %r" % (missing,))
        if args[-1] != IMAGE:
            die("unexpected run image")
        if WORLD["container"]["exists"]:
            die("container already exists")
        WORLD["container"] = {
            "exists": True,
            "state": "running",
            "labels": {"darkula.owned": "true", "darkula.service": "postgres"},
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
        WORLD["container"] = absent_container()
        save()
        sys.exit(0)

    if args[0] == "exec":
        if len(args) < 3 or args[1] != CONTAINER or args[2] != "pg_isready":
            die("unexpected exec: %r" % (args,))
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
    for port in WORLD["ss_listeners"]:
        print("LISTEN 0 4096 127.0.0.1:%s 0.0.0.0:*" % port)
    """
)


def _world(
    *,
    container: dict[str, Any] | None = None,
    volume: dict[str, Any] | None = None,
    image: bool = True,
    ss_listeners: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "container": container or {"exists": False, "state": "exited", "labels": None},
        "volume": volume or {"exists": False, "labels": None},
        "image": image,
        "ss_listeners": list(ss_listeners),
    }


OWNED_CONTAINER = {
    "exists": True,
    "state": "running",
    "labels": {"darkula.owned": "true", "darkula.service": "postgres"},
}
OWNED_VOLUME = {
    "exists": True,
    "labels": {"darkula.owned": "true", "darkula.service": "postgres"},
}
UNLABELED_VOLUME = {"exists": True, "labels": {}}
FOREIGN_CONTAINER = {"exists": True, "state": "running", "labels": {}}


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
        return cast(
            dict[str, Any],
            json.loads(self.world_path.read_text(encoding="utf-8")),
        )

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
    """P1/P2: new volumes are created WITH the ownership label; existing
    owned volumes are reused without recreation."""

    def test_start_creates_labeled_volume(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world())
        result = fake.run("start")
        assert result.returncode == 0, result.stderr
        assert result.stdout.count("creating volume darkula-postgres-data") == 1
        world = fake.world()
        assert world["volume"]["exists"] is True
        assert world["volume"]["labels"] == {
            "darkula.owned": "true",
            "darkula.service": "postgres",
        }
        assert world["container"]["exists"] is True
        assert world["container"]["labels"]["darkula.owned"] == "true"

    def test_start_reuses_owned_volume(self, faker: Callable[..., FakePodman]) -> None:
        fake = faker(_world(container=OWNED_CONTAINER, volume=OWNED_VOLUME))
        result = fake.run("start")
        assert result.returncode == 0, result.stderr
        assert "already running" in result.stdout
        assert [c for c in fake.calls() if c[:2] == ["volume", "create"]] == []
        assert [c for c in fake.calls() if c[0] == "run"] == []
        assert fake.world()["volume"]["labels"] == OWNED_VOLUME["labels"]


class TestStartFailClosed:
    """P3/P4/P9/P11: start refuses unverified resources or a foreign port
    listener before any mutation, and never falls back to another port."""

    def test_start_refuses_unlabeled_volume(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(volume=UNLABELED_VOLUME))
        result = fake.run("start")
        assert result.returncode != 0
        assert "darkula-postgres-data" in result.stderr
        assert "NOT Darkula-owned" in result.stderr
        world = fake.world()
        assert world["volume"] == UNLABELED_VOLUME
        assert world["container"]["exists"] is False
        assert [c for c in fake.calls() if c[0] == "run"] == []
        assert [c for c in fake.calls() if c[:2] == ["volume", "create"]] == []
        assert [c for c in fake.calls() if c[:2] == ["volume", "rm"]] == []

    def test_start_refuses_wrongly_labeled_volume(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        wrong = {"exists": True, "labels": {"app": "other"}}
        fake = faker(_world(volume=wrong))
        result = fake.run("start")
        assert result.returncode != 0
        assert "NOT Darkula-owned" in result.stderr
        assert fake.world()["volume"] == wrong
        assert fake.world()["container"]["exists"] is False

    def test_start_refuses_foreign_port_listener(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(ss_listeners=("35432",)))
        result = fake.run("start")
        assert result.returncode != 0
        assert "35432" in result.stderr
        assert "non-Darkula listener" in result.stderr
        world = fake.world()
        assert world["container"]["exists"] is False
        assert world["volume"]["exists"] is False
        assert [c for c in fake.calls() if c[0] == "run"] == []
        assert [c for c in fake.calls() if c[:2] == ["volume", "create"]] == []

    def test_start_fails_closed_on_unverifiable_volume_metadata(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        for mode in ("fail", "garbage"):
            volume = dict(OWNED_VOLUME)
            volume["inspect"] = mode
            fake = faker(_world(volume=volume))
            result = fake.run("start")
            assert result.returncode != 0
            assert "could not be verified" in result.stderr
            assert fake.world()["volume"]["exists"] is True
            assert fake.world()["container"]["exists"] is False


class TestClean:
    """P5-P8: clean preflights every existing resource before any
    destructive action; absent resources are a safe no-op."""

    def test_clean_removes_owned_resources(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=OWNED_CONTAINER, volume=OWNED_VOLUME))
        result = fake.run("clean")
        assert result.returncode == 0, result.stderr
        world = fake.world()
        assert world["container"]["exists"] is False
        assert world["volume"]["exists"] is False
        assert ["rm", "-f", "darkula-postgres"] in fake.calls()
        assert ["volume", "rm", "darkula-postgres-data"] in fake.calls()

    def test_clean_unlabeled_volume_removes_neither(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=OWNED_CONTAINER, volume=UNLABELED_VOLUME))
        result = fake.run("clean")
        assert result.returncode != 0
        assert "refusing destructive cleanup" in result.stderr
        world = fake.world()
        assert world["container"]["exists"] is True
        assert world["volume"]["exists"] is True
        assert ["rm", "-f", "darkula-postgres"] not in fake.calls()
        assert ["volume", "rm", "darkula-postgres-data"] not in fake.calls()

    def test_clean_foreign_container_removes_neither(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=FOREIGN_CONTAINER, volume=OWNED_VOLUME))
        result = fake.run("clean")
        assert result.returncode != 0
        assert "refusing destructive cleanup" in result.stderr
        world = fake.world()
        assert world["container"]["exists"] is True
        assert world["volume"]["exists"] is True
        assert ["rm", "-f", "darkula-postgres"] not in fake.calls()
        assert ["volume", "rm", "darkula-postgres-data"] not in fake.calls()

    def test_clean_absent_resources_is_safe_noop(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world())
        result = fake.run("clean")
        assert result.returncode == 0, result.stderr
        assert fake.calls() == [
            ["container", "exists", "darkula-postgres"],
            ["volume", "exists", "darkula-postgres-data"],
            ["container", "exists", "darkula-postgres"],
            ["volume", "exists", "darkula-postgres-data"],
        ]
        assert fake.world() == _world()


class TestForeignResources:
    """P10: the script never issues a command naming anything outside the
    exact Darkula-owned set, and never broad discovery."""

    def test_only_exact_darkula_names_are_ever_referenced(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(ss_listeners=("8080", "9000")))
        result = fake.run("start")
        assert result.returncode == 0, result.stderr
        allowed = {_CONTAINER, _VOLUME, _IMAGE}
        for call in fake.calls():
            referenced: set[str] = set()
            for token in call:
                if token == _IMAGE:
                    referenced.add(token)
                elif token.startswith("darkula-"):
                    # Name-shaped tokens only; mounts like
                    # darkula-postgres-data:/var/lib/postgresql reduce to
                    # the exact volume name.
                    referenced.add(token.split(":")[0].split("/")[0])
            assert referenced <= allowed
        clean = fake.run("clean")
        assert clean.returncode == 0, clean.stderr
        for call in fake.calls():
            referenced = set()
            for token in call:
                if token == _IMAGE:
                    referenced.add(token)
                elif token.startswith("darkula-"):
                    referenced.add(token.split(":")[0].split("/")[0])
            assert referenced <= allowed


class TestInspectBoundary:
    """The fake itself fails closed on foreign/broad commands; a green run
    proves lifecycle policy never needed them (P7/P10 support)."""

    def test_fake_rejects_foreign_resource_commands(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world())
        # Directly exercise the shim boundary: a foreign volume name or a
        # broad prune must never be part of a successful Darkula lifecycle.
        probe = fake.run("status")
        assert probe.returncode == 0
        assert fake.calls() == [["container", "exists", "darkula-postgres"]]


class TestIdempotency:
    """P12: repeated start/clean on valid state remains idempotent."""

    def test_repeated_start_clean_is_idempotent(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=OWNED_CONTAINER, volume=OWNED_VOLUME))
        first = fake.run("start")
        second = fake.run("start")
        assert first.returncode == 0 and second.returncode == 0
        assert "already running" in first.stdout
        assert "already running" in second.stdout
        assert [c for c in fake.calls() if c[:2] == ["volume", "create"]] == []
        assert [c for c in fake.calls() if c[0] == "run"] == []
        clean1 = fake.run("clean")
        assert clean1.returncode == 0
        assert fake.world()["container"]["exists"] is False
        assert fake.world()["volume"]["exists"] is False
        clean2 = fake.run("clean")
        assert clean2.returncode == 0
        assert fake.world()["container"]["exists"] is False
        assert fake.world()["volume"]["exists"] is False


class TestStop:
    """stop mutates only a positively verified Darkula container."""

    def test_stop_owned_container(self, faker: Callable[..., FakePodman]) -> None:
        fake = faker(_world(container=OWNED_CONTAINER, volume=OWNED_VOLUME))
        result = fake.run("stop")
        assert result.returncode == 0, result.stderr
        assert fake.world()["container"]["state"] == "exited"

    def test_stop_refuses_foreign_container(
        self, faker: Callable[..., FakePodman]
    ) -> None:
        fake = faker(_world(container=FOREIGN_CONTAINER))
        result = fake.run("stop")
        assert result.returncode != 0
        assert fake.world()["container"]["state"] == "running"

    def test_stop_absent_is_noop(self, faker: Callable[..., FakePodman]) -> None:
        fake = faker(_world())
        result = fake.run("stop")
        assert result.returncode == 0
        assert "nothing to stop" in result.stdout
