# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral source-domain repository contracts (PR 4).

Repositories are aggregate-oriented and transaction-scoped: instances are
obtained only through :class:`~darkula.app.persistence.UnitOfWork`, never
constructed directly, and a single unit-of-work commit covers all of their
work.

Semantics frozen here:

- ``SourceCandidateRepository`` owns candidates, candidate lifecycle history
  and recon assessments;
- ``SourceRepository`` owns managed sources, endpoints and source
  assessments;
- status transitions pass the *expected* current status and are atomic with
  exactly one apprentice history event (conflict on mismatch);
- candidate event history and both assessment histories are append-only in
  production: no update/delete methods exist;
- every production implementation of these contracts invokes versioned
  PostgreSQL stored functions; no implementation may embed data-access SQL
  in Python (see the PostgreSQL adapter and the migration artifacts).

Conflict/integrity/not-found failures surface through the bounded
``PersistenceError`` subtypes defined in
:mod:`darkula.app.persistence`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from darkula.domain.identifiers import (
    SourceCandidateId,
    SourceId,
)
from darkula.domain.source import (
    CandidateStatus,
    ReconAssessment,
    Source,
    SourceAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
    SourceEndpoint,
)

__all__ = [
    "SourceCandidateRepository",
    "SourceRepository",
]


class SourceCandidateRepository(ABC):
    """Candidate lifecycle + recon assessment persistence contract.

    All methods are bound to the owning unit of work; there is no
    repository-owned transaction or connection.
    """

    @abstractmethod
    async def create(
        self,
        candidate: SourceCandidate,
        *,
        event: SourceCandidateEventHistory,
    ) -> None:
        """Persist a candidate and its initial history event atomically.

        :raises ConflictError: if the candidate or initial event identity
            already exists (no update, no overwrite).
        """

    @abstractmethod
    async def get(self, candidate_id: SourceCandidateId) -> SourceCandidate | None:
        """Return the candidate or ``None`` when it does not exist."""

    @abstractmethod
    async def transition(
        self,
        candidate_id: SourceCandidateId,
        *,
        expected: CandidateStatus,
        new_status: CandidateStatus,
        event: SourceCandidateEventHistory,
    ) -> None:
        """Transition candidate status atomically with one history event.

        The transition applies only when the current status equals
        ``expected``; the stored function updates the status and appends
        exactly one event in one database operation.

        :raises ConflictError: if the current status differs from
            ``expected`` (no update is applied and no event is appended).
        """

    @abstractmethod
    async def append_history(self, event: SourceCandidateEventHistory) -> None:
        """Append one candidate lifecycle history record.

        :raises ConflictError: if the event identity already exists.
        :raises IntegrityError: if the referenced candidate does not exist.
        """

    @abstractmethod
    async def list_history(
        self, candidate_id: SourceCandidateId
    ) -> tuple[SourceCandidateEventHistory, ...]:
        """Return the candidate's history in deterministic order."""

    @abstractmethod
    async def append_recon_assessment(self, assessment: ReconAssessment) -> None:
        """Append one immutable recon assessment.

        :raises ConflictError: if the assessment identity already exists.
        :raises IntegrityError: if the referenced candidate does not exist.
        """

    @abstractmethod
    async def list_recon_assessments(
        self, candidate_id: SourceCandidateId
    ) -> tuple[ReconAssessment, ...]:
        """Return the candidate's recon assessments in deterministic order."""


class SourceRepository(ABC):
    """Managed source, endpoint and source assessment persistence contract.

    All methods are bound to the owning unit of work; there is no
    repository-owned transaction or connection.
    """

    @abstractmethod
    async def create(self, source: Source) -> None:
        """Persist a managed source.

        :raises ConflictError: if the source identity already exists.
        """

    @abstractmethod
    async def get(self, source_id: SourceId) -> Source | None:
        """Return the source or ``None`` when it does not exist."""

    @abstractmethod
    async def add_endpoint(self, endpoint: SourceEndpoint) -> None:
        """Attach one endpoint to its source.

        :raises ConflictError: if the endpoint identity already exists.
        :raises IntegrityError: if the referenced source does not exist.
        """

    @abstractmethod
    async def list_endpoints(self, source_id: SourceId) -> tuple[SourceEndpoint, ...]:
        """Return the source's endpoints in deterministic order."""

    @abstractmethod
    async def append_source_assessment(self, assessment: SourceAssessment) -> None:
        """Append one immutable source assessment.

        :raises ConflictError: if the assessment identity already exists.
        :raises IntegrityError: if the referenced source does not exist.
        """

    @abstractmethod
    async def list_source_assessments(
        self, source_id: SourceId
    ) -> tuple[SourceAssessment, ...]:
        """Return the source's assessments in deterministic order."""
