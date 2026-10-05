# SPDX-License-Identifier: AGPL-3.0-only
"""Policy-bounded historical source analysis (PR 14).

This is the application capability that turns a bounded persisted history for
one managed :class:`~darkula.domain.source.Source` into an immutable,
historical, source-level :class:`~darkula.domain.source.SourceAssessment`:

.. code-block:: text

    explicit SourceAnalysisRequest
      -> short read UoW: source + existing semantic assessment -> close
      -> bounded SourceAnalysisContextBuilder (short DB reads only)
      -> SourceAnalyst over the existing LlmClient (no UoW open)
      -> trusted evidence grounding (no UoW open)
      -> construct trusted SourceAssessment identity/time/window/profile
      -> short write UoW append+commit
      -> expected semantic race -> fresh UoW reload winner

Dominant invariant: ``SourceAnalysisService`` decides *what* historical window
may be analyzed and owns durable assessment identity. ``SourceAnalyst`` only
reasons over a bounded trusted context assembled from persisted Darkula
observations. The model may select supplied evidence references but may never
invent evidence, browse, mutate source state, or persist assessments.

Source-to-content ownership is proven from persisted production provenance:
normalized observations are selected only when their ``crawl_request_id`` is
the deterministic request identity derived from a persisted collection run of
the requested Source. No URI string matching is ever used to infer ownership.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from darkula.app.collection import (
    CollectionPolicyError,
    deterministic_request_id,
    policy_execution_from_snapshot,
)
from darkula.app.llm import LlmError, LlmErrorCode
from darkula.app.persistence import (
    ConflictError,
    DarkulaSpi,
    IntegrityError,
    UnitOfWork,
)
from darkula.app.source_analyst import (
    SourceAnalysisProposal,
    SourceAnalysisResponse,
    SourceAnalyst,
)
from darkula.config.settings import SourceAnalysisSettings
from darkula.domain.collection import CollectionRun
from darkula.domain.content import NormalizedContent
from darkula.domain.extraction import ExtractedEntity
from darkula.domain.geography import GeographicResolution
from darkula.domain.identifiers import (
    ExtractedEntityId,
    SourceAssessmentId,
    SourceId,
)
from darkula.domain.relationships import ExtractedRelationship
from darkula.domain.source import (
    Confidence,
    SourceAssessment,
)
from darkula.telemetry.decorators import counted, timed, traced
from darkula.telemetry.metrics import get_counter

#: Frozen developer-controlled analysis profile identity. Future semantic
#: changes require a new profile version; it is never runtime configuration.
SOURCE_ANALYSIS_PROFILE_NAME = "source-analysis"
SOURCE_ANALYSIS_PROFILE_VERSION = "v1"

#: Stable telemetry names (bounded; no ids/values/URIs/refs/content).
EXECUTIONS_METRIC = "darkula.source_analysis.executions"
CREATED_METRIC = "darkula.source_analysis.created"
REPLAYS_METRIC = "darkula.source_analysis.replays"
EVIDENCE_ITEMS_METRIC = "darkula.source_analysis.evidence_items"
DURATION_METRIC = "darkula.source_analysis.duration"
ANALYZE_SPAN = "source_analysis.analyze"


class SourceAnalysisError(RuntimeError):
    """Base class for bounded source-analysis failures (never raw details)."""


class SourceAnalysisSourceNotFoundError(SourceAnalysisError):
    """The requested managed Source does not exist."""


class SourceAnalysisInvalidWindowError(SourceAnalysisError):
    """The requested historical window is naive, inverted, or oversized."""


class SourceAnalysisNoEvidenceError(SourceAnalysisError):
    """No qualifying persisted evidence exists for the requested window.

    The absence of collected evidence is never inferred to mean inactivity;
    no assessment is fabricated and no model call is made.
    """


class SourceAnalysisTooLargeError(SourceAnalysisError):
    """The context or requested window exceeds a hard configured bound."""


class SourceAnalysisModelError(SourceAnalysisError):
    """The model-backed reasoning operation failed (bounded, provider-neutral)."""


class SourceAnalysisOutputInvalidError(SourceAnalysisError):
    """The model did not return a valid, grounded structured proposal."""


class SourceAnalysisEvidenceError(SourceAnalysisError):
    """A model-returned evidence reference could not be grounded."""


class SourceAnalysisPersistenceIntegrityError(SourceAnalysisError):
    """A source assessment could not be persisted or reloaded consistently."""


class SourceAnalysisEvidenceKind(StrEnum):
    """Finite vocabulary of persisted evidence kinds presented to the model."""

    CONTENT_OBSERVATION = "CONTENT_OBSERVATION"
    ENTITY_OCCURRENCE = "ENTITY_OCCURRENCE"
    GEOGRAPHIC_RESOLUTION = "GEOGRAPHIC_RESOLUTION"
    RELATIONSHIP_ASSERTION = "RELATIONSHIP_ASSERTION"
    COLLECTION_RUN = "COLLECTION_RUN"


#: Deterministic evidence-kind ordering rank inside one observation time.
_KIND_RANK: dict[str, int] = {
    SourceAnalysisEvidenceKind.CONTENT_OBSERVATION.value: 0,
    SourceAnalysisEvidenceKind.ENTITY_OCCURRENCE.value: 1,
    SourceAnalysisEvidenceKind.GEOGRAPHIC_RESOLUTION.value: 2,
    SourceAnalysisEvidenceKind.RELATIONSHIP_ASSERTION.value: 3,
    SourceAnalysisEvidenceKind.COLLECTION_RUN.value: 4,
}


@dataclass(frozen=True, slots=True)
class SourceAnalysisRequest:
    """One explicit caller request for a bounded historical source window.

    ``window_start``/``window_end`` describe the historical observation period
    (inclusive) and ``source_id`` is the managed Source identity. Provider,
    model, profile, prompt, and persistence details are deliberately not
    request fields.
    """

    source_id: SourceId
    window_start: datetime
    window_end: datetime

    def __post_init__(self) -> None:
        if self.window_start.tzinfo is None:
            raise SourceAnalysisInvalidWindowError(
                "window_start must be timezone-aware (UTC)"
            )
        if self.window_end.tzinfo is None:
            raise SourceAnalysisInvalidWindowError(
                "window_end must be timezone-aware (UTC)"
            )
        start = self.window_start.astimezone(UTC)
        end = self.window_end.astimezone(UTC)
        if end < start:
            raise SourceAnalysisInvalidWindowError(
                "window_end must not precede window_start"
            )
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)


@dataclass(frozen=True, slots=True)
class SourceAnalysisEvidence:
    """One invocation-local trusted evidence reference.

    ``ref`` (``A1``, ``A2``, ...) is invocation-local and never persisted;
    ``durable_reference`` is the trusted Darkula identity of the underlying
    persisted observation. ``summary`` is a deterministic rendering of a
    persisted fact, never an LLM summary.
    """

    ref: str
    kind: SourceAnalysisEvidenceKind
    observed_at: datetime
    summary: str
    durable_reference: str


@dataclass(frozen=True, slots=True)
class SourceAnalysisContext:
    """The bounded, deterministic evidence universe for one analysis request."""

    source_id: SourceId
    window_start: datetime
    window_end: datetime
    evidence: tuple[SourceAnalysisEvidence, ...]
    context_truncated: bool = False


@dataclass(frozen=True, slots=True)
class SourceAnalysisOutcome:
    """Outcome of one :meth:`SourceAnalysisService.analyze` call."""

    assessment: SourceAssessment
    created: bool


@dataclass(frozen=True, slots=True)
class _EvidenceDraft:
    """One unrefd evidence draft prior to deterministic ordering/ref assignment."""

    observed_at: datetime
    content_key: str
    kind: SourceAnalysisEvidenceKind
    identity: str
    summary: str
    durable_reference: str

    def sort_key(self) -> tuple[datetime, str, int, str]:
        """Return the frozen deterministic ordering key."""
        return (
            self.observed_at,
            self.content_key,
            _KIND_RANK[self.kind.value],
            self.identity,
        )


def _sanitize_summary(value: str, *, max_chars: int) -> str:
    """Return a bounded, control-free deterministic evidence summary."""
    cleaned = "".join(char if ord(char) >= 32 else " " for char in value).strip()
    if len(cleaned) > max_chars:
        return cleaned[:max_chars]
    return cleaned


def _entity_summary(entity: ExtractedEntity) -> str:
    return f"entity type={entity.entity_type.value} value={entity.normalized_value}"


def _geography_summary(resolution: GeographicResolution) -> str:
    parts = [f"geographic status={resolution.status.value}"]
    if resolution.canonical_name:
        parts.append(f"canonical={resolution.canonical_name}")
    if resolution.country_code:
        parts.append(f"country={resolution.country_code}")
    return " ".join(parts)


def _relationship_summary(
    relationship: ExtractedRelationship,
    entities: dict[ExtractedEntityId, ExtractedEntity],
) -> str:
    source = entities.get(relationship.source_entity_id)
    target = entities.get(relationship.target_entity_id)
    source_value = source.normalized_value if source is not None else "?"
    target_value = target.normalized_value if target is not None else "?"
    return (
        f"relationship {source_value} --{relationship.predicate.value}--> "
        f"{target_value}"
    )


def _content_summary(content: NormalizedContent) -> str:
    parts = [f"content kind={content.completeness.value}"]
    if content.content_type is not None:
        parts.append(f"type={content.content_type.value}")
    if content.title:
        parts.append(f"title={content.title}")
    return " ".join(parts)


def _run_summary(run: CollectionRun) -> str:
    completed = "none" if run.completed_at is None else run.completed_at.isoformat()
    return (
        f"collection-run status={run.status.value} "
        f"observations={run.content_observations} completed_at={completed}"
    )


def derive_source_request_ids(runs: tuple[CollectionRun, ...]) -> tuple[str, ...]:
    """Return the deterministic crawl-request identities of the given runs.

    A normalized observation belongs to a Source iff its ``crawl_request_id``
    equals the deterministic request identity derived from a persisted run of
    that Source. This is provenance-based ownership proof, never URI matching.
    Runs whose frozen snapshot cannot be decoded cannot have produced a
    provable observation and are skipped.
    """
    identities: set[str] = set()
    for run in runs:
        try:
            execution = policy_execution_from_snapshot(run.policy_snapshot)
        except CollectionPolicyError:
            continue
        for attempt in range(1, run.attempt_count + 1):
            for endpoint_id in execution.allowed_endpoint_ids:
                identities.add(
                    deterministic_request_id(
                        run_id=run.run_id,
                        endpoint_id=endpoint_id,
                        attempt=attempt,
                    )
                )
    return tuple(sorted(identities))


def ground_evidence_references(
    catalog: tuple[SourceAnalysisEvidence, ...],
    response: SourceAnalysisResponse,
    *,
    max_refs: int,
) -> tuple[str, ...]:
    """Convert model-cited invocation refs into trusted durable references.

    Unknown references fail the whole operation. Duplicate references are
    deduplicated deterministically and the result is returned in context order
    (``A1..An``), never model order. The model never constructs durable refs.
    """
    if len(response.evidence_refs) > max_refs:
        raise SourceAnalysisOutputInvalidError(
            "the model returned more evidence references than the bound allows"
        )
    by_ref = {evidence.ref: evidence for evidence in catalog}
    requested: set[str] = set()
    for ref in response.evidence_refs:
        if ref not in by_ref:
            raise SourceAnalysisEvidenceError(
                "the model cited an evidence reference that was not supplied"
            )
        requested.add(ref)
    ordered = tuple(
        evidence.durable_reference for evidence in catalog if evidence.ref in requested
    )
    if not ordered:
        raise SourceAnalysisEvidenceError(
            "the model cited no usable evidence reference"
        )
    return ordered


class SourceAnalysisContextBuilder:
    """Build the bounded deterministic analysis context for one request.

    The builder depends only on the application ``DarkulaSpi``. It never calls
    deterministic/semantic extraction, geographic resolution, or relationship
    extraction: missing persisted facts simply remain missing.
    """

    def __init__(
        self,
        *,
        spi: DarkulaSpi,
        settings: SourceAnalysisSettings,
    ) -> None:
        self._spi = spi
        self._max_evidence_items = settings.max_evidence_items
        self._max_context_chars = settings.max_context_chars
        self._max_evidence_summary_chars = settings.max_evidence_summary_chars

    async def build(self, request: SourceAnalysisRequest) -> SourceAnalysisContext:
        """Return the bounded context or an empty-evidence context."""
        async with self._spi.unit_of_work() as uow:
            runs = await uow.collection.list_runs_for_source_window(
                request.source_id,
                request.window_start,
                request.window_end,
            )
            request_ids = derive_source_request_ids(runs)
            observations: tuple[NormalizedContent, ...] = ()
            if request_ids:
                observations = await uow.content.list_observations_for_requests(
                    request_ids,
                    window_start=request.window_start,
                    window_end=request.window_end,
                    limit=self._max_evidence_items + 1,
                )
            content_overflow = len(observations) > self._max_evidence_items
            selected = observations[: self._max_evidence_items]
            drafts = await self._collect_drafts(uow, selected, runs)
        return self._assemble(request, drafts, content_overflow=content_overflow)

    async def _collect_drafts(
        self,
        uow: UnitOfWork,
        observations: tuple[NormalizedContent, ...],
        runs: tuple[CollectionRun, ...],
    ) -> list[_EvidenceDraft]:
        drafts: list[_EvidenceDraft] = []
        for content in observations:
            drafts.append(
                _EvidenceDraft(
                    observed_at=content.observed_at,
                    content_key=str(content.content_id),
                    kind=SourceAnalysisEvidenceKind.CONTENT_OBSERVATION,
                    identity=str(content.content_id),
                    summary=_sanitize_summary(
                        _content_summary(content),
                        max_chars=self._max_evidence_summary_chars,
                    ),
                    durable_reference=f"content:{content.content_id}",
                )
            )
            entities = await uow.extraction.list_entities_for_content(
                content.content_id
            )
            entity_index = {entity.entity_id: entity for entity in entities}
            for entity in entities:
                drafts.append(
                    _EvidenceDraft(
                        observed_at=content.observed_at,
                        content_key=str(content.content_id),
                        kind=SourceAnalysisEvidenceKind.ENTITY_OCCURRENCE,
                        identity=str(entity.entity_id),
                        summary=_sanitize_summary(
                            _entity_summary(entity),
                            max_chars=self._max_evidence_summary_chars,
                        ),
                        durable_reference=f"entity:{entity.entity_id}",
                    )
                )
            resolutions = await uow.geography.list_for_content(content.content_id)
            for resolution in resolutions:
                drafts.append(
                    _EvidenceDraft(
                        observed_at=content.observed_at,
                        content_key=str(content.content_id),
                        kind=SourceAnalysisEvidenceKind.GEOGRAPHIC_RESOLUTION,
                        identity=str(resolution.resolution_id),
                        summary=_sanitize_summary(
                            _geography_summary(resolution),
                            max_chars=self._max_evidence_summary_chars,
                        ),
                        durable_reference=f"geography:{resolution.resolution_id}",
                    )
                )
            relationships = await uow.relationships.list_for_content(content.content_id)
            for relationship in relationships:
                drafts.append(
                    _EvidenceDraft(
                        observed_at=content.observed_at,
                        content_key=str(content.content_id),
                        kind=SourceAnalysisEvidenceKind.RELATIONSHIP_ASSERTION,
                        identity=str(relationship.relationship_id),
                        summary=_sanitize_summary(
                            _relationship_summary(relationship, entity_index),
                            max_chars=self._max_evidence_summary_chars,
                        ),
                        durable_reference=(
                            f"relationship:{relationship.relationship_id}"
                        ),
                    )
                )
        for run in runs:
            drafts.append(
                _EvidenceDraft(
                    observed_at=run.scheduled_for,
                    content_key="",
                    kind=SourceAnalysisEvidenceKind.COLLECTION_RUN,
                    identity=str(run.run_id),
                    summary=_sanitize_summary(
                        _run_summary(run),
                        max_chars=self._max_evidence_summary_chars,
                    ),
                    durable_reference=f"collection-run:{run.run_id}",
                )
            )
        return drafts

    def _assemble(
        self,
        request: SourceAnalysisRequest,
        drafts: list[_EvidenceDraft],
        *,
        content_overflow: bool,
    ) -> SourceAnalysisContext:
        ordered = sorted(drafts, key=lambda draft: draft.sort_key())
        included: list[_EvidenceDraft] = []
        aggregate = 0
        truncated = content_overflow
        for draft in ordered:
            if len(included) >= self._max_evidence_items:
                truncated = True
                break
            if aggregate + len(draft.summary) > self._max_context_chars:
                truncated = True
                break
            included.append(draft)
            aggregate += len(draft.summary)
        evidence = tuple(
            SourceAnalysisEvidence(
                ref=f"A{index}",
                kind=draft.kind,
                observed_at=draft.observed_at,
                summary=draft.summary,
                durable_reference=draft.durable_reference,
            )
            for index, draft in enumerate(included, start=1)
        )
        return SourceAnalysisContext(
            source_id=request.source_id,
            window_start=request.window_start,
            window_end=request.window_end,
            evidence=evidence,
            context_truncated=truncated,
        )


class SourceAnalysisService:
    """Owns authorization, replay, context, grounding, and persistence."""

    def __init__(
        self,
        *,
        spi: DarkulaSpi,
        context_builder: SourceAnalysisContextBuilder,
        analyst: SourceAnalyst,
        settings: SourceAnalysisSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._spi = spi
        self._context_builder = context_builder
        self._analyst = analyst
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def profile_name(self) -> str:
        """Return the fixed analysis profile name."""
        return SOURCE_ANALYSIS_PROFILE_NAME

    @property
    def profile_version(self) -> str:
        """Return the fixed analysis profile version."""
        return SOURCE_ANALYSIS_PROFILE_VERSION

    @counted(metric=EXECUTIONS_METRIC)
    @timed(metric=DURATION_METRIC)
    @traced(span_name=ANALYZE_SPAN)
    async def analyze(self, request: SourceAnalysisRequest) -> SourceAnalysisOutcome:
        """Analyze one bounded historical source window.

        :raises SourceAnalysisSourceNotFoundError: the Source does not exist.
        :raises SourceAnalysisInvalidWindowError: the window is underspecified
            or exceeds the configured maximum.
        :raises SourceAnalysisNoEvidenceError: no qualifying persisted evidence
            exists (no model call is made).
        :raises SourceAnalysisModelError: the model operation failed.
        :raises SourceAnalysisOutputInvalidError: the model output was invalid.
        :raises SourceAnalysisEvidenceError: the model cited unknown evidence.
        :raises SourceAnalysisPersistenceIntegrityError: the assessment could
            not be persisted or reloaded.
        :raises asyncio.CancelledError: cancellation propagates unchanged.
        """
        self._enforce_window(request)
        exists, existing = await self._read_existing(request)
        if not exists:
            raise SourceAnalysisSourceNotFoundError(
                "the requested managed source does not exist"
            )
        if existing is not None:
            get_counter(REPLAYS_METRIC).add(1)
            return SourceAnalysisOutcome(assessment=existing, created=False)
        context = await self._context_builder.build(request)
        get_counter(EVIDENCE_ITEMS_METRIC).add(len(context.evidence))
        if not context.evidence:
            raise SourceAnalysisNoEvidenceError(
                "no qualifying persisted evidence exists for the requested window"
            )
        proposal = await self._reason(context)
        durable_refs = ground_evidence_references(
            context.evidence,
            proposal.response,
            max_refs=self._settings.max_evidence_refs,
        )
        assessment = self._construct_assessment(request, proposal, durable_refs)
        outcome = await self._persist(request, assessment)
        if outcome.created:
            get_counter(CREATED_METRIC).add(1)
        return outcome

    def _enforce_window(self, request: SourceAnalysisRequest) -> None:
        maximum = timedelta(days=self._settings.max_window_days)
        if request.window_end - request.window_start > maximum:
            raise SourceAnalysisInvalidWindowError(
                "the requested window exceeds the configured maximum"
            )

    async def _read_existing(
        self, request: SourceAnalysisRequest
    ) -> tuple[bool, SourceAssessment | None]:
        async with self._spi.unit_of_work() as uow:
            source = await uow.sources.get(request.source_id)
            if source is None:
                return False, None
            existing = await uow.sources.get_source_assessment_by_profile(
                request.source_id,
                request.window_start,
                request.window_end,
                self.profile_name,
                self.profile_version,
            )
            return True, existing

    async def _reason(self, context: SourceAnalysisContext) -> SourceAnalysisProposal:
        try:
            return await self._analyst.analyze(context)
        except LlmError as exc:
            if exc.code is LlmErrorCode.INVALID_STRUCTURED_OUTPUT:
                raise SourceAnalysisOutputInvalidError(
                    "the model returned an invalid structured analysis response"
                ) from exc
            raise SourceAnalysisModelError(
                "the source-analysis model operation failed"
            ) from exc

    def _construct_assessment(
        self,
        request: SourceAnalysisRequest,
        proposal: SourceAnalysisProposal,
        durable_refs: tuple[str, ...],
    ) -> SourceAssessment:
        response = proposal.response
        try:
            return SourceAssessment(
                assessment_id=SourceAssessmentId.generate(),
                source_id=request.source_id,
                assessed_at=self._clock(),
                window_start=request.window_start,
                window_end=request.window_end,
                confidence=Confidence(response.confidence),
                relevance=(
                    None
                    if response.relevance is None
                    else Confidence(response.relevance)
                ),
                activity=(
                    None if response.activity is None else Confidence(response.activity)
                ),
                novelty=(
                    None if response.novelty is None else Confidence(response.novelty)
                ),
                evidence_references=durable_refs,
                characteristics=response.characteristics.model_dump(),
                profile_name=self.profile_name,
                profile_version=self.profile_version,
            )
        except ValueError as exc:
            raise SourceAnalysisOutputInvalidError(
                "the model proposal could not form a valid assessment"
            ) from exc

    async def _persist(
        self,
        request: SourceAnalysisRequest,
        assessment: SourceAssessment,
    ) -> SourceAnalysisOutcome:
        lost_race = False
        async with self._spi.unit_of_work() as uow:
            try:
                await uow.sources.append_source_assessment(assessment)
            except ConflictError:
                await uow.rollback()
                lost_race = True
            except IntegrityError as exc:
                raise SourceAnalysisPersistenceIntegrityError(
                    "the source assessment could not be persisted"
                ) from exc
            else:
                await uow.commit()
        if lost_race:
            return await self._reload_winner(request)
        return SourceAnalysisOutcome(assessment=assessment, created=True)

    async def _reload_winner(
        self, request: SourceAnalysisRequest
    ) -> SourceAnalysisOutcome:
        async with self._spi.unit_of_work() as uow:
            winner = await uow.sources.get_source_assessment_by_profile(
                request.source_id,
                request.window_start,
                request.window_end,
                self.profile_name,
                self.profile_version,
            )
        if winner is None:
            raise SourceAnalysisPersistenceIntegrityError(
                "a concurrent assessment could not be reloaded"
            )
        return SourceAnalysisOutcome(assessment=winner, created=False)


# ``asyncio.CancelledError`` propagates unchanged by construction: the
# service never catches ``BaseException`` and the model/context helpers never
# suppress cancellation.


__all__ = [
    "ANALYZE_SPAN",
    "CREATED_METRIC",
    "DURATION_METRIC",
    "EVIDENCE_ITEMS_METRIC",
    "EXECUTIONS_METRIC",
    "REPLAYS_METRIC",
    "SOURCE_ANALYSIS_PROFILE_NAME",
    "SOURCE_ANALYSIS_PROFILE_VERSION",
    "SourceAnalysisContext",
    "SourceAnalysisContextBuilder",
    "SourceAnalysisError",
    "SourceAnalysisEvidence",
    "SourceAnalysisEvidenceError",
    "SourceAnalysisEvidenceKind",
    "SourceAnalysisInvalidWindowError",
    "SourceAnalysisModelError",
    "SourceAnalysisNoEvidenceError",
    "SourceAnalysisOutcome",
    "SourceAnalysisOutputInvalidError",
    "SourceAnalysisPersistenceIntegrityError",
    "SourceAnalysisRequest",
    "SourceAnalysisService",
    "SourceAnalysisSourceNotFoundError",
    "SourceAnalysisTooLargeError",
    "derive_source_request_ids",
    "ground_evidence_references",
]
