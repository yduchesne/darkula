# SPDX-License-Identifier: AGPL-3.0-only
"""LocalFileObjectStore tests (root confinement + streaming matrix)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path

import pytest

from darkula.app.object_store import (
    ContentType,
    InvalidObjectKeyError,
    ObjectNotFoundError,
)
from darkula.domain.identifiers import ObjectKey
from darkula.infrastructure.object_store import LocalFileObjectStore


async def _chunks(data: bytes, size: int = 3) -> AsyncIterator[bytes]:
    """Yield ``data`` in deterministic bounded chunks."""
    for index in range(0, len(data), size):
        yield data[index : index + size]


async def _collect(source: AsyncIterable[bytes]) -> bytes:
    """Collect an async byte iterable fully."""
    return b"".join([chunk async for chunk in source])


def _key(*segments: str) -> ObjectKey:
    return ObjectKey("/".join(segments))


def _store(root: Path) -> LocalFileObjectStore:
    return LocalFileObjectStore(root, chunk_size=4)


class TestRoundTrip:
    """Round trips, sizes, and bounded read chunks."""

    @pytest.mark.asyncio
    async def test_round_trip_with_bounded_read_chunks(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        payload = b"0123456789abcdefghij"
        stored = await store.put(
            key=_key("a", "b", "artifact.bin"), data=_chunks(payload)
        )
        assert stored.size_bytes == len(payload)

        read_chunks: list[bytes] = []
        async for chunk in await store.get(_key("a", "b", "artifact.bin")):
            assert len(chunk) <= 4
            read_chunks.append(chunk)
        assert b"".join(read_chunks) == payload

    @pytest.mark.asyncio
    async def test_nested_key_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await store.put(key=_key("deep", "nested", "path", "blob"), data=_chunks(b"x"))
        assert _key("deep", "nested", "path", "blob") in {
            ObjectKey("deep/nested/path/blob")
        }
        assert (
            await _collect(await store.get(_key("deep", "nested", "path", "blob")))
            == b"x"
        )
        assert (tmp_path / "deep" / "nested" / "path" / "blob").is_file()

    @pytest.mark.asyncio
    async def test_content_hash_matches_payload(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        payload = b"hash me"
        stored = await store.put(key=_key("blob"), data=_chunks(payload))
        assert stored.content_hash is not None
        assert stored.content_hash.digest_hex == hashlib.sha256(payload).hexdigest()


class TestMissingSemantics:
    """Frozen missing behavior."""

    @pytest.mark.asyncio
    async def test_stat_missing_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert await store.stat(_key("missing")) is None

    @pytest.mark.asyncio
    async def test_get_missing_raises_typed_not_found(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ObjectNotFoundError):
            await store.get(_key("missing"))

    @pytest.mark.asyncio
    async def test_delete_missing_is_idempotent_success(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await store.delete(_key("missing"))


class TestConfinement:
    """Traversal/absolute/backslash/root-escape rejection."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "key_value",
        ["a/../b", "../escape", "a//b", "a/./b", "a\\..\\b", "a/b/../../c"],
    )
    async def test_traversal_keys_rejected(
        self, tmp_path: Path, key_value: str
    ) -> None:
        store = _store(tmp_path)
        key = ObjectKey(key_value)
        with pytest.raises(InvalidObjectKeyError):
            await store.stat(key)

    @pytest.mark.asyncio
    async def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        link = tmp_path / "storage" / "link"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        store = _store(tmp_path / "storage")
        key = ObjectKey("link/secret.txt")
        with pytest.raises(InvalidObjectKeyError):
            await store.stat(key)

    @pytest.mark.asyncio
    async def test_absolute_key_rejected_at_value_boundary(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            ObjectKey("/etc/passwd")

    def test_root_is_frozen_absolute(self, tmp_path: Path) -> None:
        store = LocalFileObjectStore(tmp_path)
        assert store.root == tmp_path.resolve()


class TestWrites:
    """Overwrite, failed/cancelled write cleanup, delete."""

    @pytest.mark.asyncio
    async def test_overwrite_replaces_content(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await store.put(key=_key("blob"), data=_chunks(b"old content"))
        overwritten = await store.put(key=_key("blob"), data=_chunks(b"new"))
        assert overwritten.size_bytes == 3
        assert await _collect(await store.get(_key("blob"))) == b"new"

    @pytest.mark.asyncio
    async def test_failed_write_leaves_no_partial_object(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        async def failing_chunks() -> AsyncIterator[bytes]:
            yield b"partial"
            raise RuntimeError("upload failed")

        with pytest.raises(RuntimeError, match="upload failed"):
            await store.put(key=_key("blob"), data=failing_chunks())
        assert not (tmp_path / "blob").exists()
        assert _temporary_files(tmp_path) == []

    @pytest.mark.asyncio
    async def test_cancelled_write_cleans_temp_file(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        async def cancelled_chunks() -> AsyncIterator[bytes]:
            yield b"partial"
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await store.put(key=_key("blob"), data=cancelled_chunks())
        assert not (tmp_path / "blob").exists()
        assert _temporary_files(tmp_path) == []

    @pytest.mark.asyncio
    async def test_invalid_chunk_rejected_and_cleaned(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        async def bad_chunks() -> AsyncIterator[object]:
            yield b"ok"
            yield "not-bytes"

        with pytest.raises(ValueError, match="must be bytes"):
            await store.put(key=_key("blob"), data=bad_chunks())  # type: ignore[arg-type]
        assert _temporary_files(tmp_path) == []

    @pytest.mark.asyncio
    async def test_delete_removes_object(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await store.put(key=_key("blob"), data=_chunks(b"abc"))
        await store.delete(_key("blob"))
        assert not (tmp_path / "blob").exists()
        assert await store.stat(_key("blob")) is None


class TestIsolationAndMetadata:
    """Per-store isolation and in-memory metadata behavior."""

    @pytest.mark.asyncio
    async def test_roots_are_isolated(self, tmp_path: Path) -> None:
        first = _store(tmp_path / "one")
        second = _store(tmp_path / "two")
        await first.put(key=_key("blob"), data=_chunks(b"abc"))
        assert await second.stat(_key("blob")) is None
        assert await first.stat(_key("blob")) is not None

    @pytest.mark.asyncio
    async def test_content_type_preserved_via_recorded_metadata(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        content_type = ContentType("application/json")
        await store.put(
            key=_key("blob"),
            data=_chunks(b"{}"),
            content_type=content_type,
            metadata={"source": "test"},
        )
        stat = await store.stat(_key("blob"))
        assert stat is not None
        assert stat.content_type == content_type
        assert stat.metadata["source"] == "test"
        # A simulated restart loses only the custom metadata, not durability.
        restarted = _store(tmp_path)
        stat = await restarted.stat(_key("blob"))
        assert stat is not None
        assert stat.size_bytes == 2
        assert stat.content_type is None


def _temporary_files(root: Path) -> list[Path]:
    """Return leftover temporary files under ``root`` (best-effort)."""
    if not root.exists():
        return []
    return list(root.rglob("*.darkula-tmp-*"))
