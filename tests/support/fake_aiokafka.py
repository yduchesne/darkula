# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic fake aiokafka client for offline adapter tests.

Implements exactly the aiokafka surface the RedpandaDataStream uses
(producer send/flush/start/stop, consumer start/stop/getmany/commit,
``TopicPartition``, ``ConsumerRecord``) so DS-* unit tests never require a
live broker. The fakes record every call and support scripted failures,
including ``asyncio.CancelledError`` propagation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FakeTopicPartition:
    """Deterministic stand-in for aiokafka ``TopicPartition``."""

    topic: str
    partition: int


@dataclass(frozen=True, slots=True)
class FakeConsumerRecord:
    """Deterministic stand-in for aiokafka ``ConsumerRecord``."""

    topic: str
    partition: int
    offset: int
    value: bytes
    headers: Sequence[tuple[str, bytes]] = ()


class FakeProducer:
    """Records sends; supports scripted start/flush/send failures."""

    def __init__(self) -> None:
        self.started = False
        self.sent: list[dict[str, Any]] = []
        self.start_failure: BaseException | None = None
        self.send_failure: BaseException | None = None
        #: Fail every send once at least this many sends succeeded.
        self.send_fail_from: int | None = None
        self.flush_failure: BaseException | None = None

    async def start(self) -> None:
        if self.start_failure is not None:
            raise self.start_failure
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send(
        self,
        topic: str,
        *,
        value: bytes | None,
        key: bytes | None = None,
        headers: Sequence[tuple[str, bytes]] | None = None,
        **_: Any,
    ) -> Any:
        if self.send_failure is not None:
            raise self.send_failure
        if self.send_fail_from is not None and len(self.sent) >= self.send_fail_from:
            raise RuntimeError("scripted partial send failure")
        self.sent.append(
            {
                "topic": topic,
                "value": value,
                "key": key,
                "headers": list(headers or ()),
            }
        )
        return None

    async def flush(self) -> None:
        if self.flush_failure is not None:
            raise self.flush_failure


class FakeConsumer:
    """Records commits; serves scripted poll batches/failures."""

    def __init__(self, topic: str) -> None:
        self.topic = topic
        self.started = False
        self.batches: list[dict[FakeTopicPartition, list[FakeConsumerRecord]]] = []
        self.commits: list[dict[FakeTopicPartition, int]] = []
        self.start_failure: BaseException | None = None
        self.poll_failure: BaseException | None = None
        self.commit_failure: BaseException | None = None

    async def start(self) -> None:
        if self.start_failure is not None:
            raise self.start_failure
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def getmany(
        self, *, timeout_ms: int, max_records: int
    ) -> dict[FakeTopicPartition, list[FakeConsumerRecord]]:
        if self.poll_failure is not None:
            raise self.poll_failure
        if not self.batches:
            return {}
        batch = self.batches.pop(0)
        total = 0
        result: dict[FakeTopicPartition, list[FakeConsumerRecord]] = {}
        for partition, records in batch.items():
            allowed = records[: max(0, max_records - total)]
            total += len(allowed)
            result[partition] = allowed
            if total >= max_records:
                break
        return result

    async def commit(
        self, offsets: dict[FakeTopicPartition, int] | None = None
    ) -> None:
        if self.commit_failure is not None:
            raise self.commit_failure
        self.commits.append(dict(offsets or {}))


__all__ = [
    "FakeConsumer",
    "FakeConsumerRecord",
    "FakeProducer",
    "FakeTopicPartition",
]
