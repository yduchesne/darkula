# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the persistence / UnitOfWork boundary (PERSIST-*)."""

from __future__ import annotations

import asyncio
import pathlib
from typing import Self

import pytest

from darkula.app.persistence import DarkulaSpi, UnitOfWork
from darkula.app.repositories import (
    CollectionRepository,
    ContentRepository,
    ExtractionRepository,
    OutboxRepository,
    ProcessedMessageRepository,
    SourceCandidateRepository,
    SourceRepository,
)


class _RecordingUnitOfWork(UnitOfWork):
    """Test-local UnitOfWork stub recording commit/rollback events."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.entered = False

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    @property
    def source_candidates(self) -> SourceCandidateRepository:
        raise AssertionError("recording stub exposes no candidates repository")

    @property
    def sources(self) -> SourceRepository:
        raise AssertionError("recording stub exposes no sources repository")

    @property
    def outbox(self) -> OutboxRepository:
        raise AssertionError("recording stub exposes no outbox repository")

    @property
    def processed_messages(self) -> ProcessedMessageRepository:
        raise AssertionError("recording stub exposes no processed-message repository")

    @property
    def content(self) -> ContentRepository:
        raise AssertionError("recording stub exposes no content repository")

    @property
    def collection(self) -> CollectionRepository:
        raise AssertionError("recording stub exposes no collection repository")

    @property
    def extraction(self) -> ExtractionRepository:
        raise AssertionError("recording stub exposes no extraction repository")


class _RecordingSpi(DarkulaSpi):
    """Test-local DarkulaSpi stub that hands out fresh UnitOfWork instances."""

    def __init__(self) -> None:
        self.created: list[_RecordingUnitOfWork] = []

    def unit_of_work(self) -> _RecordingUnitOfWork:
        uow = _RecordingUnitOfWork()
        self.created.append(uow)
        return uow


class TestExplicitTransaction:
    """PERSIST-01/02: commit and rollback are explicit operations."""

    @pytest.mark.asyncio
    async def test_explicit_commit_representable(self) -> None:
        uow = _RecordingUnitOfWork()
        async with uow:
            await uow.commit()
        assert uow.commits == 1
        assert uow.rollbacks == 0

    @pytest.mark.asyncio
    async def test_explicit_rollback_representable(self) -> None:
        uow = _RecordingUnitOfWork()
        async with uow:
            await uow.rollback()
        assert uow.rollbacks == 1

    @pytest.mark.asyncio
    async def test_normal_exit_does_not_commit(self) -> None:
        uow = _RecordingUnitOfWork()
        async with uow:
            pass
        assert uow.commits == 0
        assert uow.rollbacks == 0

    @pytest.mark.asyncio
    async def test_exceptional_exit_rolls_back(self) -> None:
        uow = _RecordingUnitOfWork()
        with pytest.raises(RuntimeError, match="boom"):
            async with uow:
                raise RuntimeError("boom")
        assert uow.rollbacks == 1

    @pytest.mark.asyncio
    async def test_cancellation_exit_rolls_back(self) -> None:
        uow = _RecordingUnitOfWork()
        with pytest.raises(asyncio.CancelledError):
            async with uow:
                raise asyncio.CancelledError()
        assert uow.rollbacks == 1


class TestSpi:
    """PERSIST-01: unit_of_work is the only construction seam."""

    @pytest.mark.asyncio
    async def test_spi_hands_out_fresh_unit_of_work(self) -> None:
        spi = _RecordingSpi()
        first = spi.unit_of_work()
        second = spi.unit_of_work()
        assert first is not second
        async with first as uow:
            await uow.commit()
        assert first.commits == 1
        assert second.commits == 0


class TestBoundaryPurity:
    """PERSIST-04/05: no ORM/driver types, no premature repositories."""

    def test_no_sqlalchemy_or_psycopg_in_interface(self) -> None:
        source = pathlib.Path(__file__).resolve().parents[3] / "src"
        text = (source / "darkula" / "app" / "persistence.py").read_text()
        for forbidden in ("import sqlalchemy", "from sqlalchemy", "import psycopg"):
            assert forbidden not in text, f"persistence leak: {forbidden}"

    def test_no_domain_repository_methods_introduced(self) -> None:
        from darkula.app import persistence

        module_names = {
            "source_repository",
            "evidence_repository",
            "collection_repository",
            "SourceRepository",
            "EvidenceRepository",
            "CollectionRepository",
        }
        assert module_names.isdisjoint(vars(persistence))
