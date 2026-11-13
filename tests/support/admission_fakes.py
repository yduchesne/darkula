# SPDX-License-Identifier: AGPL-3.0-only
"""Composed in-memory fake for PR 19 candidate admission.

This is a *single* admission-specific test-support class. It composes the
existing PR 17 discovery/access/qualification fakes and the PR 10 candidate
fake rather than introducing a competing persistence architecture:

- observations, access evidence, qualification decisions and registry state
  come from :mod:`tests.support.discovery_fakes`;
- candidates, lifecycle history and recon assessments come from
  :mod:`tests.support.recon_fakes`;
- admission slots and the admission ledger live in
  :class:`MemAdmissionState`.

Commit/rollback semantics are shared with the existing fakes through
``apply_pending_discovery``/``apply_pending_candidates``; nothing here
duplicates a unit-of-work implementation. The fake mirrors the real
stored-function decision matrix closely enough for deterministic unit tests;
real PostgreSQL multi-connection races and rollback are proven by the
integration suite.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from darkula.app.persistence import AdmissionInvariantError, DarkulaSpi, UnitOfWork
from darkula.app.repositories import (
    AccessRepository,
    AdmissionRepository,
    DiscoveryRepository,
    QualificationRepository,
    SourceCandidateRepository,
)
from darkula.domain.candidate_admission import (
    MAX_POSSIBLE_ALIASES,
    AdmissionOutcome,
    AdmissionReason,
    AdmissionResult,
    AliasSuggestionsResult,
    PossibleAliasSuggestion,
)
from darkula.domain.discovery import (
    DiscoveryObservation,
    DiscoveryReferenceKind,
    DiscoveryValidationStatus,
)
from darkula.domain.identifiers import (
    CandidateEventId,
    DiscoveryObservationId,
    ReconAssessmentId,
    SourceCandidateId,
    SourceEndpointId,
)
from darkula.domain.source import (
    CandidateEventType,
    CandidateStatus,
    ReconAssessment,
    SourceCandidate,
    SourceCandidateEventHistory,
)
from tests.support.discovery_fakes import (
    MemAccessRepository,
    MemDiscoveryRepository,
    MemDiscoveryState,
    MemQualificationRepository,
    _Pending,
    apply_pending_discovery,
)
from tests.support.recon_fakes import (
    MemCandidateRepository,
    MemCandidateState,
    _PendingCandidateState,
    apply_pending_candidates,
)

_MAX_PENDING_LIMIT = 50
_MAX_CLAIMS_LIMIT = 65

SlotKey = tuple[str, SourceCandidateId, DiscoveryObservationId]


def locator_digest(normalized_locator: str) -> str:
    """Return the admission slot lookup digest for one normalized locator."""
    return hashlib.sha256(normalized_locator.encode("utf-8")).hexdigest()


@dataclass
class MemAdmissionState:
    """Committed admission slots, ledger, and exact endpoint fixtures."""

    slots: dict[str, SlotKey] = field(default_factory=dict)
    ledger: dict[DiscoveryObservationId, AdmissionResult] = field(default_factory=dict)
    endpoints: list[tuple[SourceEndpointId, str]] = field(default_factory=list)
    verified: dict[DiscoveryObservationId, datetime] = field(default_factory=dict)


@dataclass
class _PendingAdmission:
    slots: dict[str, SlotKey] = field(default_factory=dict)
    ledger: dict[DiscoveryObservationId, AdmissionResult] = field(default_factory=dict)
    verified: dict[DiscoveryObservationId, datetime] = field(default_factory=dict)


class MemAdmissionRepository(AdmissionRepository):
    """In-memory admission repository mirroring the PR 19 stored function."""

    def __init__(
        self,
        *,
        discovery: MemDiscoveryRepository,
        discovery_state: MemDiscoveryState,
        discovery_pending: _Pending,
        candidates: MemCandidateRepository,
        candidate_state: MemCandidateState,
        candidate_pending: _PendingCandidateState,
        state: MemAdmissionState,
        pending: _PendingAdmission,
    ) -> None:
        self._discovery = discovery
        self._discovery_state = discovery_state
        self._discovery_pending = discovery_pending
        self._candidates = candidates
        self._candidate_state = candidate_state
        self._candidate_pending = candidate_pending
        self._state = state
        self._pending = pending

    # -- helpers --------------------------------------------------------

    def _all_candidates(self) -> list[SourceCandidate]:
        visible = dict(self._candidate_state.candidates)
        visible.update(self._candidate_pending.candidates)
        return list(visible.values())

    def _slot(self, key: str) -> SlotKey | None:
        staged = self._pending.slots.get(key)
        if staged is not None:
            return staged
        return self._state.slots.get(key)

    def _existing(
        self, observation_id: DiscoveryObservationId
    ) -> AdmissionResult | None:
        staged = self._pending.ledger.get(observation_id)
        if staged is not None:
            return staged
        return self._state.ledger.get(observation_id)

    def _visible_observations(self) -> list[DiscoveryObservation]:
        visible = {
            item.observation_id: item
            for item in self._discovery_state.observations_by_id.values()
        }
        visible.update(
            {item.observation_id: item for item in self._discovery_pending.observations}
        )
        return list(visible.values())

    def _admitted_observations(
        self,
    ) -> list[tuple[DiscoveryObservation, SourceCandidateId]]:
        rows: list[tuple[DiscoveryObservation, SourceCandidateId]] = []
        visible = {
            item.observation_id: item
            for item in self._discovery_state.observations_by_id.values()
        }
        visible.update(
            {item.observation_id: item for item in self._discovery_pending.observations}
        )
        ledger = dict(self._state.ledger)
        ledger.update(self._pending.ledger)
        for observation_id, result in ledger.items():
            if result.candidate_id is None:
                continue
            observation = visible.get(observation_id)
            if observation is None:
                continue
            rows.append((observation, result.candidate_id))
        return rows

    # -- repository contract -------------------------------------------

    async def pending_observation_ids(
        self, provider_id: str, *, limit: int = 25
    ) -> tuple[DiscoveryObservationId, ...]:
        if limit < 1 or limit > _MAX_PENDING_LIMIT:
            raise ValueError(f"limit must be within 1..{_MAX_PENDING_LIMIT}")
        ledger_ids = set(self._state.ledger) | set(self._pending.ledger)
        selected = [
            observation
            for observation in self._visible_observations()
            if observation.provider_id == provider_id
            and observation.observation_id not in ledger_ids
        ]
        selected.sort(key=lambda item: (item.ingested_at, str(item.observation_id)))
        return tuple(item.observation_id for item in selected[:limit])

    async def admit(
        self,
        observation_id: DiscoveryObservationId,
        candidate_id: SourceCandidateId,
        event_id: CandidateEventId,
        *,
        processed_at: datetime,
    ) -> AdmissionResult:
        del processed_at
        observation = await self._discovery.get(observation_id)
        if observation is None:
            from darkula.app.persistence import NotFoundError

            raise NotFoundError("the discovery observation does not exist")

        existing = self._existing(observation_id)
        if existing is not None:
            return replace(existing, replayed=True)

        if observation.reference_kind is DiscoveryReferenceKind.NAME_ONLY:
            result = AdmissionResult(
                observation_id=observation_id,
                outcome=AdmissionOutcome.UNRESOLVED_NAME,
                reason=AdmissionReason.NAME_ONLY,
                candidate_id=None,
                matched_endpoint_ids=(),
            )
            self._pending.ledger[observation_id] = result
            return result

        if (
            observation.reference_kind is not DiscoveryReferenceKind.LOCATOR
            or observation.validation_status
            is not DiscoveryValidationStatus.VALID_LOCATOR
            or observation.normalized_locator is None
        ):
            raise AdmissionInvariantError(
                "the persisted discovery classification is inconsistent; "
                "no admission was written"
            )

        locator = observation.normalized_locator
        key = locator_digest(locator)

        slot = self._slot(key)
        if slot is not None:
            if slot[0] != locator:
                raise AdmissionInvariantError(
                    "the locator digest collides with a different normalized "
                    "locator; no admission was written"
                )
            result = AdmissionResult(
                observation_id=observation_id,
                outcome=AdmissionOutcome.ADMITTED_EXISTING,
                reason=AdmissionReason.EXISTING_LOCATOR_SLOT,
                candidate_id=slot[1],
                matched_endpoint_ids=(),
            )
            self._pending.ledger[observation_id] = result
            return result

        matches = sorted(
            (
                endpoint_id
                for endpoint_id, uri in self._state.endpoints
                if uri == locator
            ),
            key=str,
        )
        if len(matches) >= 33:
            result = AdmissionResult(
                observation_id=observation_id,
                outcome=AdmissionOutcome.REVIEW_REQUIRED,
                reason=AdmissionReason.EXCESS_ENDPOINT_MATCHES,
                candidate_id=None,
                matched_endpoint_ids=(),
            )
            self._pending.ledger[observation_id] = result
            return result
        if matches:
            result = AdmissionResult(
                observation_id=observation_id,
                outcome=AdmissionOutcome.KNOWN_ENDPOINT,
                reason=AdmissionReason.EXACT_MANAGED_ENDPOINT,
                candidate_id=None,
                matched_endpoint_ids=tuple(matches[:32]),
            )
            self._pending.ledger[observation_id] = result
            return result

        legacy = sorted(
            (
                candidate
                for candidate in self._all_candidates()
                if candidate.entrypoint == locator
            ),
            key=lambda item: str(item.candidate_id),
        )
        if len(legacy) >= 2:
            result = AdmissionResult(
                observation_id=observation_id,
                outcome=AdmissionOutcome.REVIEW_REQUIRED,
                reason=AdmissionReason.MULTIPLE_LEGACY_CANDIDATES,
                candidate_id=None,
                matched_endpoint_ids=(),
            )
            self._pending.ledger[observation_id] = result
            return result
        if len(legacy) == 1:
            if legacy[0].status is CandidateStatus.PROMOTED:
                result = AdmissionResult(
                    observation_id=observation_id,
                    outcome=AdmissionOutcome.REVIEW_REQUIRED,
                    reason=AdmissionReason.MULTIPLE_LEGACY_CANDIDATES,
                    candidate_id=None,
                    matched_endpoint_ids=(),
                )
                self._pending.ledger[observation_id] = result
                return result
            self._pending.slots[key] = (
                locator,
                legacy[0].candidate_id,
                observation_id,
            )
            result = AdmissionResult(
                observation_id=observation_id,
                outcome=AdmissionOutcome.ADMITTED_EXISTING,
                reason=AdmissionReason.EXISTING_LEGACY_CANDIDATE,
                candidate_id=legacy[0].candidate_id,
                matched_endpoint_ids=(),
            )
            self._pending.ledger[observation_id] = result
            return result

        candidate = SourceCandidate(
            candidate_id=candidate_id,
            discovered_at=observation.ingested_at,
            discovery_method=observation.detection_method.value,
            entrypoint=locator,
            status=CandidateStatus.DISCOVERED,
            discovery_context={
                "observation_id": str(observation_id),
                "locator_key": key,
            },
        )
        event = SourceCandidateEventHistory(
            event_id=event_id,
            candidate_id=candidate_id,
            event_type=CandidateEventType.DISCOVERED,
            occurred_at=observation.ingested_at,
            context={"observation_id": str(observation_id)},
            reason="discovery observation admitted",
            provenance=f"discovery_observation:{observation_id}",
        )
        await self._candidates.create(candidate, event=event)
        self._pending.slots[key] = (locator, candidate_id, observation_id)
        result = AdmissionResult(
            observation_id=observation_id,
            outcome=AdmissionOutcome.ADMITTED_NEW,
            reason=AdmissionReason.NEW_LOCATOR,
            candidate_id=candidate_id,
            matched_endpoint_ids=(),
        )
        self._pending.ledger[observation_id] = result
        return result

    async def get(
        self, observation_id: DiscoveryObservationId
    ) -> AdmissionResult | None:
        return self._existing(observation_id)

    async def claims_for_candidate(
        self, candidate_id: SourceCandidateId, *, limit: int = 65
    ) -> tuple[DiscoveryObservation, ...]:
        if limit < 1 or limit > _MAX_CLAIMS_LIMIT:
            raise ValueError(f"limit must be within 1..{_MAX_CLAIMS_LIMIT}")
        selected: list[DiscoveryObservation] = []
        for observation, owner in self._admitted_observations():
            if owner != candidate_id:
                continue
            result = self._existing(observation.observation_id)
            if result is None or result.candidate_id != candidate_id:
                continue
            if result.outcome not in (
                AdmissionOutcome.ADMITTED_NEW,
                AdmissionOutcome.ADMITTED_EXISTING,
            ):
                continue
            selected.append(observation)
        selected.sort(key=lambda item: (item.ingested_at, str(item.observation_id)))
        return tuple(selected[:limit])

    def _slot_for_candidate(self, candidate_id: SourceCandidateId) -> SlotKey | None:
        for slot in (*self._state.slots.values(), *self._pending.slots.values()):
            if slot[1] == candidate_id:
                return slot
        return None

    def _verified_at(self, observation_id: DiscoveryObservationId) -> datetime | None:
        staged = self._pending.verified.get(observation_id)
        if staged is not None:
            return staged
        return self._state.verified.get(observation_id)

    async def record_verified_registry_observation(
        self, observation_id: DiscoveryObservationId, *, verified_at: datetime
    ) -> None:
        from darkula.app.persistence import NotFoundError
        from darkula.domain.discovery import DiscoveryDetectionMethod

        observation = await self._discovery.get(observation_id)
        if observation is None:
            raise NotFoundError("the discovery observation does not exist")
        if (
            observation.provider_id != "ransomlook"
            or observation.detection_method is not DiscoveryDetectionMethod.REGISTRY
        ):
            raise AdmissionInvariantError(
                "the stored observation is not a RansomLook REGISTRY claim; "
                "no verified marker was written"
            )
        if self._verified_at(observation_id) is not None:
            return
        self._pending.verified[observation_id] = verified_at

    async def verified_registry_observation_ids_for_candidate(
        self, candidate_id: SourceCandidateId, *, limit: int = 65
    ) -> tuple[DiscoveryObservationId, ...]:
        if limit < 1 or limit > _MAX_CLAIMS_LIMIT:
            raise ValueError(f"limit must be within 1..{_MAX_CLAIMS_LIMIT}")
        selected: list[DiscoveryObservation] = []
        for observation, owner in self._admitted_observations():
            if owner != candidate_id:
                continue
            if self._verified_at(observation.observation_id) is None:
                continue
            selected.append(observation)
        selected.sort(key=lambda item: (item.ingested_at, str(item.observation_id)))
        return tuple(item.observation_id for item in selected[:limit])

    async def current_endpoint_match(
        self, candidate_id: SourceCandidateId
    ) -> tuple[bool, bool]:
        slot = self._slot_for_candidate(candidate_id)
        if slot is None:
            return False, False
        locator = slot[0]
        present = any(uri == locator for _endpoint_id, uri in self._state.endpoints)
        return True, present

    async def possible_aliases(
        self, candidate_id: SourceCandidateId, *, limit: int = 16
    ) -> AliasSuggestionsResult:
        if limit < 1 or limit > MAX_POSSIBLE_ALIASES:
            raise ValueError(f"limit must be within 1..{MAX_POSSIBLE_ALIASES}")
        admitted = self._admitted_observations()
        ours = [observation for observation, owner in admitted if owner == candidate_id]
        theirs = [
            (observation, owner)
            for observation, owner in admitted
            if owner != candidate_id
        ]
        best: dict[SourceCandidateId, PossibleAliasSuggestion] = {}
        for ours_observation in ours:
            for other_observation, other_candidate in theirs:
                if (
                    ours_observation.provider_id != other_observation.provider_id
                    or ours_observation.origin_reference is None
                    or ours_observation.origin_reference
                    != other_observation.origin_reference
                    or ours_observation.normalized_locator
                    == other_observation.normalized_locator
                ):
                    continue
                candidate = PossibleAliasSuggestion(
                    left_candidate_id=candidate_id,
                    right_candidate_id=other_candidate,
                    left_observation_id=ours_observation.observation_id,
                    right_observation_id=other_observation.observation_id,
                )
                current = best.get(other_candidate)
                if current is None or (
                    str(candidate.left_observation_id),
                    str(candidate.right_observation_id),
                ) < (
                    str(current.left_observation_id),
                    str(current.right_observation_id),
                ):
                    best[other_candidate] = candidate
        ordered = sorted(best.values(), key=lambda item: str(item.right_candidate_id))
        if len(ordered) > limit:
            return AliasSuggestionsResult(items=(), too_many=True)
        return AliasSuggestionsResult(items=tuple(ordered), too_many=False)


class MemAdmissionUnitOfWork(UnitOfWork):
    """One in-memory transaction composing discovery and candidate state."""

    def __init__(self, spi: MemAdmissionSpi) -> None:
        self._spi = spi
        self._discovery_pending = _Pending.empty()
        self._candidate_pending = _PendingCandidateState()
        self._admission_pending = _PendingAdmission()
        self._discovery = MemDiscoveryRepository(spi.discovery, self._discovery_pending)
        self._candidates = MemCandidateRepository(
            spi.candidates, self._candidate_pending
        )
        self._access = MemAccessRepository(
            spi.discovery,
            self._discovery_pending,
            candidate_exists=self._candidate_exists,
        )
        self._qualification = MemQualificationRepository(
            spi.discovery,
            self._discovery_pending,
            recon_assessment_owner=self._recon_assessment_owner,
        )
        self._admission = MemAdmissionRepository(
            discovery=self._discovery,
            discovery_state=spi.discovery,
            discovery_pending=self._discovery_pending,
            candidates=self._candidates,
            candidate_state=spi.candidates,
            candidate_pending=self._candidate_pending,
            state=spi.admission,
            pending=self._admission_pending,
        )
        self.commits = 0
        self.rollbacks = 0

    def _candidate_exists(self, candidate_id: SourceCandidateId) -> bool:
        return (
            candidate_id in self._spi.discovery.candidate_status
            or candidate_id in self._candidate_pending.candidates
        )

    def _recon_assessment_owner(
        self, assessment_id: ReconAssessmentId
    ) -> SourceCandidateId | None:
        for candidate_id, assessments in self._candidate_pending.assessments.items():
            if any(item.assessment_id == assessment_id for item in assessments):
                return candidate_id
        for candidate_id, assessments in self._spi.candidates.assessments.items():
            if any(item.assessment_id == assessment_id for item in assessments):
                return candidate_id
        return None

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
        candidate_pending = self._candidate_pending
        apply_pending_candidates(self._spi.candidates, candidate_pending)
        for candidate_id, candidate in candidate_pending.candidates.items():
            self._spi.discovery.candidate_status[candidate_id] = candidate.status
        for key, slot in self._admission_pending.slots.items():
            self._spi.admission.slots[key] = slot
        for observation_id, result in self._admission_pending.ledger.items():
            self._spi.admission.ledger[observation_id] = result
        for observation_id, verified_at in self._admission_pending.verified.items():
            self._spi.admission.verified.setdefault(observation_id, verified_at)
        # Qualification decisions live in the discovery fake; mirror their
        # resulting status onto the shared candidate records so a subsequent
        # candidate read observes the committed lifecycle state.
        for qualification, _event in self._discovery_pending.decisions:
            stored = self._spi.candidates.candidates.get(qualification.candidate_id)
            if stored is not None:
                self._spi.candidates.candidates[qualification.candidate_id] = replace(
                    stored, status=qualification.new_status
                )
        apply_pending_discovery(self._spi.discovery, self._discovery_pending)
        self._reset_pending()
        self.commits += 1

    async def rollback(self) -> None:
        self._reset_pending()
        self.rollbacks += 1

    def _reset_pending(self) -> None:
        self._discovery_pending = _Pending.empty()
        self._candidate_pending = _PendingCandidateState()
        self._admission_pending = _PendingAdmission()

    @property
    def discovery(self) -> DiscoveryRepository:
        return self._discovery

    @property
    def access(self) -> AccessRepository:
        return self._access

    @property
    def qualification(self) -> QualificationRepository:
        return self._qualification

    @property
    def admission(self) -> AdmissionRepository:
        return self._admission

    @property
    def source_candidates(self) -> SourceCandidateRepository:
        return self._candidates

    @property
    def sources(self) -> Any:
        raise AssertionError("admission fake exposes no source repository")

    @property
    def outbox(self) -> Any:
        raise AssertionError("admission fake exposes no outbox repository")

    @property
    def processed_messages(self) -> Any:
        raise AssertionError("admission fake exposes no processed-message repository")

    @property
    def content(self) -> Any:
        raise AssertionError("admission fake exposes no content repository")

    @property
    def collection(self) -> Any:
        raise AssertionError("admission fake exposes no collection repository")

    @property
    def extraction(self) -> Any:
        raise AssertionError("admission fake exposes no extraction repository")

    @property
    def geography(self) -> Any:
        raise AssertionError("admission fake exposes no geography repository")

    @property
    def relationships(self) -> Any:
        raise AssertionError("admission fake exposes no relationship repository")


class MemAdmissionSpi(DarkulaSpi):
    """DarkulaSpi composing discovery, candidate and admission fake state."""

    def __init__(self) -> None:
        self.discovery = MemDiscoveryState.empty()
        self.candidates = MemCandidateState()
        self.admission = MemAdmissionState()

    def unit_of_work(self) -> UnitOfWork:
        return MemAdmissionUnitOfWork(self)

    def latest_status(self, candidate_id: SourceCandidateId) -> CandidateStatus | None:
        return self.discovery.candidate_status.get(candidate_id)


async def seed_observation(
    spi: MemAdmissionSpi, observation: DiscoveryObservation
) -> None:
    """Seed one committed immutable discovery observation."""
    async with spi.unit_of_work() as uow:
        await uow.discovery.insert_or_get_exact(observation)
        await uow.commit()


async def seed_candidate(
    spi: MemAdmissionSpi,
    candidate: SourceCandidate,
    event: SourceCandidateEventHistory,
) -> None:
    """Seed one committed candidate identity, status and initial event."""
    async with spi.unit_of_work() as uow:
        await uow.source_candidates.create(candidate, event=event)
        await uow.commit()


async def seed_endpoint(
    spi: MemAdmissionSpi, endpoint_id: SourceEndpointId, uri: str
) -> None:
    """Seed one committed exact managed-endpoint locator fixture."""
    spi.admission.endpoints.append((endpoint_id, uri))


def seed_verified_observation(
    spi: MemAdmissionSpi,
    observation_id: DiscoveryObservationId,
    verified_at: datetime | None = None,
) -> None:
    """Seed one committed approved-ingestion marker in the parity fake."""
    spi.admission.verified.setdefault(observation_id, verified_at or T0)


def seed_slot(
    spi: MemAdmissionSpi,
    normalized_locator: str,
    candidate_id: SourceCandidateId,
    observation_id: DiscoveryObservationId,
) -> None:
    """Seed one committed locator slot (used to inject digest collisions)."""
    spi.admission.slots[locator_digest(normalized_locator)] = (
        normalized_locator,
        candidate_id,
        observation_id,
    )


async def seed_recon_assessment(
    spi: MemAdmissionSpi, assessment: ReconAssessment
) -> None:
    """Seed one committed genuine recon assessment owned by its candidate."""
    spi.candidates.assessments.setdefault(assessment.candidate_id, []).append(
        assessment
    )


__all__ = [
    "RANSOMLOOK_ORIGIN",
    "T0",
    "MemAdmissionRepository",
    "MemAdmissionSpi",
    "MemAdmissionState",
    "MemAdmissionUnitOfWork",
    "build_candidate",
    "build_candidate_event",
    "build_observation",
    "locator_digest",
    "seed_candidate",
    "seed_endpoint",
    "seed_observation",
    "seed_recon_assessment",
    "seed_slot",
    "seed_verified_observation",
]


# ---------------------------------------------------------------------------
# Deterministic builders for PR 19 unit tests.
# ---------------------------------------------------------------------------

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
"""Fixed UTC timestamp for deterministic PR 19 unit fixtures."""

RANSOMLOOK_ORIGIN = "https://www.ransomlook.io/api/group/example-group"
"""Fixed synthetic RansomLook group origin (never fetched)."""


def build_observation(
    *,
    url: str = "http://example-test.onion/",
    provider_id: str = "ransomlook",
    record_id: str = "location:" + "a" * 64,
    record_version: str = "v1",
    reference_kind: DiscoveryReferenceKind = DiscoveryReferenceKind.LOCATOR,
    source_kind_hint: Any = None,
    origin_reference: str | None = RANSOMLOOK_ORIGIN,
    requires_auth_claim: bool | None = None,
    detection_method: Any = None,
    observed_at: Any = None,
    ingested_at: Any = None,
) -> DiscoveryObservation:
    """Build one valid immutable observation for admission/qualification tests."""
    from darkula.domain.discovery import (
        DiscoveryDetectionMethod,
        DiscoveryInput,
        observation_from_input,
    )
    from darkula.domain.source import EndpointType

    if source_kind_hint is None:
        host = url.split("://", 1)[-1].split("/", 1)[0] if "://" in url else ""
        source_kind_hint = (
            EndpointType.ONION if host.endswith(".onion") else EndpointType.CLEARNET
        )
    method = detection_method or DiscoveryDetectionMethod.REGISTRY
    source = DiscoveryInput(
        provider_id=provider_id,
        provider_record_id=record_id,
        provider_record_version=record_version,
        detection_method=method,
        reference_kind=reference_kind,
        asserted_reference=url,
        observed_at=observed_at or T0,
        evidence_reference="registry://ransomlook/location/fixture",
        source_kind_hint=source_kind_hint,
        requires_auth_claim=requires_auth_claim,
        origin_reference=origin_reference,
    )
    return observation_from_input(
        source,
        observation_id=DiscoveryObservationId.generate(),
        ingested_at=ingested_at or T0,
    )


def build_candidate(
    *,
    entrypoint: str = "http://example-test.onion/",
    status: CandidateStatus = CandidateStatus.DISCOVERED,
    candidate_id: SourceCandidateId | None = None,
    discovery_method: str = "REGISTRY",
) -> SourceCandidate:
    """Build one deterministic candidate record."""
    return SourceCandidate(
        candidate_id=candidate_id or SourceCandidateId.generate(),
        discovered_at=T0,
        discovery_method=discovery_method,
        entrypoint=entrypoint,
        status=status,
    )


def build_candidate_event(
    candidate: SourceCandidate,
    *,
    event_type: CandidateEventType = CandidateEventType.DISCOVERED,
) -> SourceCandidateEventHistory:
    """Build one deterministic candidate lifecycle event."""
    return SourceCandidateEventHistory(
        event_id=CandidateEventId.generate(),
        candidate_id=candidate.candidate_id,
        event_type=event_type,
        occurred_at=T0,
        reason="seed",
    )
