# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic in-memory recon persistence fakes for PR 10 unit tests.

Implements the :class:`~darkula.app.repositories.SourceCandidateRepository`
contract (candidates, lifecycle history, recon assessments) plus the small
``UnitOfWork`` surface the ReconCoordinator depends on, with explicit
commit/rollback semantics. The fake mirrors the real stored-function
invariants that the coordinator unit tests exercise:

- ``transition`` is atomic: it applies only when the expected status
  matches, appends exactly one event, and raises ``ConflictError``
  otherwise (no update, no event);
- candidate/event/assessment identities are conflict-on-duplicate;
- history and recon assessments are append-only;
- pending mutations become visible to other units only on ``commit``.

The fake never touches PostgreSQL, Redpanda, the crawler, or an LLM; it is
a test double for the persistence boundary only (the behavior under test —
coordinator/agent/protocol orchestration — is the real code).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import Any, Self

from darkula.app.persistence import (
    ConflictError,
    DarkulaSpi,
    IntegrityError,
    UnitOfWork,
)
from darkula.app.repositories import SourceCandidateRepository
from darkula.domain.identifiers import SourceCandidateId
from darkula.domain.source import (
    CandidateStatus,
    ReconAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
)


@dataclass(slots=True)
class MemCandidateState:
    """Committed fake candidate persistence state."""

    candidates: dict[SourceCandidateId, SourceCandidate] = field(default_factory=dict)
    history: dict[SourceCandidateId, list[SourceCandidateEventHistory]] = field(
        default_factory=dict
    )
    assessments: dict[SourceCandidateId, list[ReconAssessment]] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class _PendingCandidateState:
    """Mutations staged inside one open unit of work."""

    candidates: dict[SourceCandidateId, SourceCandidate] = field(default_factory=dict)
    history: dict[SourceCandidateId, list[SourceCandidateEventHistory]] = field(
        default_factory=dict
    )
    assessments: dict[SourceCandidateId, list[ReconAssessment]] = field(
        default_factory=dict
    )


class MemCandidateRepository(SourceCandidateRepository):
    """In-memory SourceCandidateRepository over one UoW's pending state."""

    def __init__(
        self, state: MemCandidateState, pending: _PendingCandidateState
    ) -> None:
        self._state = state
        self._pending = pending

    def _candidate(self, candidate_id: SourceCandidateId) -> SourceCandidate | None:
        return self._pending.candidates.get(
            candidate_id, self._state.candidates.get(candidate_id)
        )

    def _history(
        self, candidate_id: SourceCandidateId
    ) -> list[SourceCandidateEventHistory]:
        return self._pending.history.get(
            candidate_id, self._state.history.get(candidate_id, [])
        )

    def _assessments(self, candidate_id: SourceCandidateId) -> list[ReconAssessment]:
        return self._pending.assessments.get(
            candidate_id, self._state.assessments.get(candidate_id, [])
        )

    async def create(
        self,
        candidate: SourceCandidate,
        *,
        event: SourceCandidateEventHistory,
    ) -> None:
        if candidate.candidate_id in self._state.candidates or (
            candidate.candidate_id in self._pending.candidates
        ):
            raise ConflictError("a candidate with this identity already exists")
        self._pending.candidates[candidate.candidate_id] = candidate
        self._pending.history.setdefault(candidate.candidate_id, []).append(event)

    async def get(self, candidate_id: SourceCandidateId) -> SourceCandidate | None:
        return self._candidate(candidate_id)

    async def transition(
        self,
        candidate_id: SourceCandidateId,
        *,
        expected: CandidateStatus,
        new_status: CandidateStatus,
        event: SourceCandidateEventHistory,
    ) -> None:
        current = self._candidate(candidate_id)
        if current is None:
            raise ConflictError("the candidate does not exist")
        if current.status is not expected:
            raise ConflictError(
                "candidate status does not equal the expected status; "
                "no transition and no event were applied"
            )
        self._pending.candidates[candidate_id] = replace(current, status=new_status)
        self._pending.history.setdefault(candidate_id, []).append(event)

    async def append_history(self, event: SourceCandidateEventHistory) -> None:
        if self._candidate(event.candidate_id) is None:
            raise IntegrityError("the referenced candidate does not exist")
        if any(
            item.event_id == event.event_id
            for item in self._history(event.candidate_id)
        ):
            raise ConflictError("an event with this identity already exists")
        self._pending.history.setdefault(event.candidate_id, []).append(event)

    async def list_history(
        self, candidate_id: SourceCandidateId
    ) -> tuple[SourceCandidateEventHistory, ...]:
        return tuple(self._history(candidate_id))

    async def append_recon_assessment(self, assessment: ReconAssessment) -> None:
        if self._candidate(assessment.candidate_id) is None:
            raise IntegrityError("the referenced candidate does not exist")
        if any(
            item.assessment_id == assessment.assessment_id
            for item in self._assessments(assessment.candidate_id)
        ):
            raise ConflictError("an assessment with this identity already exists")
        self._pending.assessments.setdefault(assessment.candidate_id, []).append(
            assessment
        )

    async def list_recon_assessments(
        self, candidate_id: SourceCandidateId
    ) -> tuple[ReconAssessment, ...]:
        return tuple(self._assessments(candidate_id))


class MemCandidateUnitOfWork(UnitOfWork):
    """One fake transaction over :class:`MemCandidateState`."""

    def __init__(self, state: MemCandidateState) -> None:
        self._state = state
        self._pending = _PendingCandidateState()
        self._repository: MemCandidateRepository | None = None
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        for candidate_id, candidate in self._pending.candidates.items():
            self._state.candidates[candidate_id] = candidate
        for candidate_id, events in self._pending.history.items():
            self._state.history.setdefault(candidate_id, []).extend(events)
        for candidate_id, assessments in self._pending.assessments.items():
            self._state.assessments.setdefault(candidate_id, []).extend(assessments)
        self._pending = _PendingCandidateState()
        self.commits += 1

    async def rollback(self) -> None:
        self._pending = _PendingCandidateState()
        self.rollbacks += 1

    @property
    def source_candidates(self) -> SourceCandidateRepository:
        if self._repository is None:
            self._repository = MemCandidateRepository(self._state, self._pending)
        return self._repository

    @property
    def sources(self) -> Any:
        raise AssertionError("recon fake exposes no source repository")

    @property
    def outbox(self) -> Any:
        raise AssertionError("recon fake exposes no outbox repository")

    @property
    def processed_messages(self) -> Any:
        raise AssertionError("recon fake exposes no processed-message repository")

    @property
    def content(self) -> Any:
        raise AssertionError("recon fake exposes no content repository")

    @property
    def collection(self) -> Any:
        raise AssertionError("recon fake exposes no collection repository")

    @property
    def extraction(self) -> Any:
        raise AssertionError("recon fake exposes no extraction repository")

    @property
    def geography(self) -> Any:
        raise AssertionError("recon fake exposes no geography repository")


class MemCandidateSpi(DarkulaSpi):
    """DarkulaSpi handing out :class:`MemCandidateUnitOfWork` instances."""

    def __init__(self, state: MemCandidateState | None = None) -> None:
        self.state = state or MemCandidateState()

    def unit_of_work(self) -> UnitOfWork:
        return MemCandidateUnitOfWork(self.state)


async def seed_candidate(
    spi: MemCandidateSpi, candidate: SourceCandidate, event: SourceCandidateEventHistory
) -> None:
    """Persist one candidate with its initial event through the fake."""
    async with spi.unit_of_work() as uow:
        await uow.source_candidates.create(candidate, event=event)
        await uow.commit()


__all__ = [
    "MemCandidateRepository",
    "MemCandidateSpi",
    "MemCandidateState",
    "MemCandidateUnitOfWork",
    "seed_candidate",
]
