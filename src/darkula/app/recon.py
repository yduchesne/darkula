# SPDX-License-Identifier: AGPL-3.0-only
"""Coordinator-owned bounded reconnaissance application layer (PR 10).

Implements Darkula's first production reasoning workflow as a **finite,
Coordinator-authorized** protocol:

.. code-block:: text

    SourceCandidate
        |
    ReconCoordinator ------------------- PostgreSQL/UoW
        |
    ReconAgent -- structured decision (INSPECT | COMPLETE)
        |
    trusted inspection authorization
        |
    Crawler -> CrawlerController -> PodmanSandbox -> runtime -> Fake World
        |
    bounded CrawlResult -> bounded evidence items
        |
    ReconAgent -> ReconAssessment
        |
    ReconCoordinator -> assessment + lifecycle transition (one UoW)

Dominant invariants (PR 10):

- ReconAgent **reasons and recommends**; the Coordinator **owns
  authorization and every lifecycle mutation**. ReconAgent never persists,
  never transitions status, never creates managed sources, and never touches
  a browser/sandbox/datastream/provider API.
- every model inspection request is converted by trusted deterministic code
  (:func:`authorize_recon_inspection`) into an existing PR 7
  ``CrawlRequest``; requests that cannot be safely represented fail closed
  before any crawler call;
- hard reasoning budgets are fixed in :class:`ReconSettings` and can never
  be expanded by the model;
- evidence references are created by trusted code and validated before
  persistence (unknown references fail closed with no final assessment);
- the final ``ReconAssessment`` append and the lifecycle transition are one
  PostgreSQL transaction; **no database transaction ever spans an LLM or
  crawler call** (short start UoW -> external loop -> short final UoW);
- hostile source text is untrusted data: source text never enters system
  instructions, and prompts/observability never carry raw content,
  credentials, or high-cardinality URLs;
- analytical dispositions (QUALIFY / NEEDS_MORE_RECON / REJECT) are never
  fabricated for operational failures: recoverable operational/protocol
  failures return the candidate to ``RECONNAISSANCE_PENDING`` with a
  ``NEEDS_MORE_RECON`` event and **no** assessment, and raise a typed error
  so the caller never mistakes them for an analytical decision;
- ``asyncio.CancelledError`` propagates unchanged and is never turned into
  a disposition or lifecycle event.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from darkula.app.llm import LlmError
from darkula.app.persistence import ConflictError, DarkulaSpi, PersistenceError
from darkula.config.settings import ReconSettings
from darkula.crawler.contracts import (
    AllowedOrigin,
    Crawler,
    CrawlRequest,
    CrawlResult,
    CrawlStatus,
    InvalidCrawlRequest,
)
from darkula.domain.identifiers import (
    CandidateEventId,
    ReconAssessmentId,
    SourceCandidateId,
)
from darkula.domain.source import (
    METADATA_SECRET_MARKERS,
    CandidateEventType,
    CandidateStatus,
    Confidence,
    ReconAssessment,
    ReconDisposition,
    SourceCandidate,
    SourceCandidateEventHistory,
)
from darkula.telemetry.metrics import get_counter, get_histogram
from darkula.telemetry.tracing import get_tracer

if TYPE_CHECKING:
    from darkula.app.recon_agent import ReconAgent

# ---------------------------------------------------------------------------
# Stable metadata constants (content-free; versioned).
# ---------------------------------------------------------------------------

#: Stable ReconAgent name for observability metadata.
RECON_AGENT_NAME = "recon_agent"

#: Stable prompt/decision-protocol version tag.
RECON_PROMPT_VERSION = "recon-v1"

#: Stable LlmClient operation name for every agent model attempt.
RECON_OPERATION_NAME = "recon.assess"

# ---------------------------------------------------------------------------
# Typed bounded errors (never raw exception/provider/source text).
# ---------------------------------------------------------------------------


class ReconError(RuntimeError):
    """Base class for bounded reconnaissance failures.

    Public messages never contain prompts, model output, source content,
    credentials, or raw provider/exception text.
    """


class ReconCandidateNotFoundError(ReconError):
    """The referenced candidate does not exist."""


class ReconCandidateNotEligibleError(ReconError):
    """The candidate cannot start reconnaissance in its current state
    (terminal, busy under an existing execution, or concurrent start lost)."""


class ReconProtocolError(ReconError):
    """The agent returned a decision that violates the bounded protocol
    (for example unknown evidence references or an unrepresentable
    completion). Never an analytical disposition."""


class ReconBudgetExceededError(ReconError):
    """The agent exhausted a trusted reasoning budget (turns/inspections/
    evidence) without a completion decision. Never an analytical
    disposition."""


class ReconInspectionRejectedError(ReconError):
    """An agent inspection request cannot be authorized (cross-origin,
    unsupported scheme, unrepresentable). No crawler call was made."""


class ReconExecutionError(ReconError):
    """An operational failure (LLM or crawler) prevented completion.

    Deliberately distinct from an analytical REJECT: no assessment is
    fabricated and the candidate is returned to a retryable pending state.
    """


# ---------------------------------------------------------------------------
# Structured decision protocol (discriminated finite schema).
# ---------------------------------------------------------------------------

#: Static schema bound: one target locator (bounded; settings never raise it).
MAX_TARGET_LENGTH = 2048
#: Static schema bound: one inspection purpose phrase.
MAX_PURPOSE_LENGTH = 500
#: Static schema bound: characteristic sections per completion.
MAX_CHARACTERISTIC_KEYS = 24
#: Static schema bound: one characteristic value.
MAX_CHARACTERISTIC_VALUE_LENGTH = 500
#: Static schema bound: evidence references per completion (settings bound
#: is the operational cap; the schema cap defends the protocol itself).
MAX_EVIDENCE_REFERENCES = 32
#: Static schema bound: serialized characteristics document.
MAX_CHARACTERISTICS_JSON_BYTES = 32768


class ReconAction(StrEnum):
    """The two decision actions of the finite recon protocol.

    ``INSPECT`` requests exactly one same-origin page inspection; the
    Coordinator authorizes or rejects it through trusted code. ``COMPLETE``
    terminates the loop with a recommendation. There is deliberately no
    free-form tool/action vocabulary.
    """

    INSPECT = "inspect"
    COMPLETE = "complete"


def _bounded_text(
    value: str, *, name: str, max_length: int, allow_empty: bool = False
) -> str:
    """Return a trimmed, bounded, control-free text value or raise."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    stripped = value.strip()
    if not allow_empty and not stripped:
        raise ValueError(f"{name} must not be blank")
    if len(stripped) > max_length:
        raise ValueError(f"{name} must not exceed {max_length} characters")
    if any(ord(ch) < 32 for ch in stripped):
        raise ValueError(f"{name} must not contain control characters")
    return stripped


class ReconInspection(BaseModel):
    """One bounded same-origin inspection request.

    The schema intentionally cannot express headers, cookies, credentials,
    browser/sandbox/network options, shell commands, or resource limits:
    every authorized inspection becomes a normal PR 7 ``CrawlRequest`` with
    trusted-side budgets only.
    """

    model_config = ConfigDict(extra="forbid")

    #: Absolute or relative same-origin target. Resolved against the
    #: candidate entrypoint origin by trusted code.
    target: str
    purpose: str

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _bounded_text(
            value, name="inspection target", max_length=MAX_TARGET_LENGTH
        )

    @field_validator("purpose")
    @classmethod
    def _validate_purpose(cls, value: str) -> str:
        return _bounded_text(
            value, name="inspection purpose", max_length=MAX_PURPOSE_LENGTH
        )


class ReconCompletion(BaseModel):
    """One terminating recommendation.

    ``evidence_references`` must cite only references actually presented to
    the model during this execution; trusted code validates them before any
    persistence. ``characteristics`` is a bounded structured document that
    becomes the persisted ``ReconAssessment.characteristics``.
    """

    model_config = ConfigDict(extra="forbid")

    disposition: ReconDisposition
    confidence: float = Field(ge=0.0, le=1.0)
    characteristics: dict[str, str] = Field(default_factory=dict)
    evidence_references: list[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a number, not a boolean")
        if not (0.0 <= value <= 1.0):
            raise ValueError("confidence must be within [0, 1]")
        return float(value)

    @field_validator("characteristics")
    @classmethod
    def _validate_characteristics(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_CHARACTERISTIC_KEYS:
            raise ValueError(
                f"characteristics must not exceed {MAX_CHARACTERISTIC_KEYS} sections"
            )
        import json

        validated: dict[str, str] = {}
        for key, item in value.items():
            bounded_key = _bounded_text(key, name="characteristic key", max_length=200)
            lower_key = bounded_key.lower()
            if any(marker in lower_key for marker in METADATA_SECRET_MARKERS):
                raise ValueError("characteristics must not contain secret-like keys")
            bounded_value = _bounded_text(
                str(item),
                name="characteristic value",
                max_length=MAX_CHARACTERISTIC_VALUE_LENGTH,
            )
            lower_value = bounded_value.lower()
            if any(marker in lower_value for marker in METADATA_SECRET_MARKERS):
                raise ValueError(
                    "characteristics must not contain credential-like values"
                )
            validated[bounded_key] = bounded_value
        try:
            encoded = json.dumps(validated, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("characteristics must be JSON-compatible") from exc
        if len(encoded.encode("utf-8")) > MAX_CHARACTERISTICS_JSON_BYTES:
            raise ValueError("characteristics must not exceed the JSON document bound")
        return validated

    @field_validator("evidence_references")
    @classmethod
    def _validate_evidence_references(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_EVIDENCE_REFERENCES:
            raise ValueError(
                f"evidence_references must not exceed {MAX_EVIDENCE_REFERENCES} refs"
            )
        return [
            _bounded_text(reference, name="evidence reference", max_length=256)
            for reference in value
        ]


class ReconAgentDecision(BaseModel):
    """One bounded agent decision: exactly one of inspection/completion.

    The discriminated shape (``action`` selects the payload) is the error
    surface a model can produce: it cannot request arbitrary tools, headers,
    credentials, or budget expansions because none of those fields exist.
    """

    model_config = ConfigDict(extra="forbid")

    action: ReconAction
    inspection: ReconInspection | None = None
    completion: ReconCompletion | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> ReconAgentDecision:
        if self.action is ReconAction.INSPECT:
            if self.inspection is None or self.completion is not None:
                raise ValueError(
                    "an INSPECT decision must carry exactly its inspection payload"
                )
        else:
            if self.completion is None or self.inspection is not None:
                raise ValueError(
                    "a COMPLETE decision must carry exactly its completion payload"
                )
        return self


# ---------------------------------------------------------------------------
# Bounded evidence registry.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconEvidenceItem:
    """One bounded observation presented to the model.

    Carries only what reconnaissance needs: a trusted stable reference, a
    bounded locator/title/excerpt, crawl provenance, and a deterministic
    sequence. Never raw artifact bytes, never Fake World truth internals.
    """

    reference: str
    url: str
    title: str
    excerpt: str
    provenance: str
    sequence: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reference",
            _bounded_text(self.reference, name="evidence reference", max_length=256),
        )
        object.__setattr__(
            self, "url", _bounded_text(self.url, name="evidence url", max_length=2048)
        )
        object.__setattr__(
            self,
            "title",
            _bounded_text(
                self.title, name="evidence title", max_length=2048, allow_empty=True
            ),
        )
        object.__setattr__(
            self,
            "excerpt",
            _bounded_text(
                self.excerpt, name="evidence excerpt", max_length=2048, allow_empty=True
            ),
        )
        object.__setattr__(
            self,
            "provenance",
            _bounded_text(self.provenance, name="evidence provenance", max_length=1024),
        )
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise ValueError("evidence sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("evidence sequence must be >= 1")


def evidence_reference(*, execution_id: str, turn: int, sequence: int) -> str:
    """Return the trusted deterministic reference of one observed page.

    References are created only by trusted Darkula code and identify one
    observation within one reconnaissance execution. Never a URL, prompt,
    or broker position.
    """
    return f"{execution_id}:{turn}:{sequence}"


class ReconEvidenceContext:
    """In-memory bounded registry of one execution's presented observations.

    The registry enforces the trusted evidence cap deterministically
    (first-N wins) and answers the reference-validity questions the final
    assessment depends on. It holds nothing beyond bounded structured
    observations and lives only for the duration of one execution.
    """

    def __init__(self, *, settings: ReconSettings) -> None:
        self._settings = settings
        self._items: dict[str, ReconEvidenceItem] = {}
        self._order: list[str] = []

    @property
    def settings(self) -> ReconSettings:
        """The trusted settings whose evidence cap this registry enforces."""
        return self._settings

    def add(self, item: ReconEvidenceItem) -> bool:
        """Append one item; ``False`` when the bounded subset is full or the
        reference is a duplicate (deterministic first-N evidence cap).

        Title/excerpt are deterministically truncated to the trusted
        per-observation excerpt budget before storage, so hostile page text
        can never exceed the bound inside the registry or the prompts it
        feeds.
        """
        if len(self._order) >= self._settings.max_evidence_items:
            return False
        if item.reference in self._items:
            return False
        item = dataclass_replace(
            item,
            title=_truncate_text(
                item.title, max_chars=self._settings.max_excerpt_chars
            ),
            excerpt=_truncate_text(
                item.excerpt, max_chars=self._settings.max_excerpt_chars
            ),
        )
        self._items[item.reference] = item
        self._order.append(item.reference)
        return True

    def has(self, reference: str) -> bool:
        """Return whether ``reference`` was presented by trusted code."""
        return reference in self._items

    def get(self, reference: str) -> ReconEvidenceItem | None:
        """Return the bounded observation for a known reference."""
        return self._items.get(reference)

    def references(self) -> tuple[str, ...]:
        """Return every reference in deterministic insertion order."""
        return tuple(self._order)

    def __len__(self) -> int:
        return len(self._order)


def build_evidence_context(context: ReconEvidenceContext, *, max_chars: int) -> str:
    """Render the deterministic bounded evidence context for one prompt.

    Items appear in trusted insertion order; the aggregate is truncated at
    ``max_chars`` deterministically (stop after the first block that does
    not fit). Excerpt/title truncation happens at item creation time, before
    any item enters the registry.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    blocks: list[str] = []
    remaining = max_chars
    for reference in context.references():
        item = context.get(reference)
        if item is None:  # pragma: no cover - registry invariant
            continue
        block = (
            f"[{reference}]\n"
            f"url: {item.url}\n"
            f"title: {item.title}\n"
            f"excerpt: {item.excerpt}\n"
        )
        if len(block) > remaining:
            if blocks:
                break
            blocks.append(block[:remaining])
            break
        blocks.append(block)
        remaining -= len(block)
    return "\n".join(blocks).rstrip()


def _truncate_text(value: str, *, max_chars: int) -> str:
    """Deterministic bounded truncation of one piece of hostile text."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    return value[:max_chars]


# ---------------------------------------------------------------------------
# Inspection authorization (trusted mapping to the existing Crawler).
# ---------------------------------------------------------------------------


def deterministic_recon_request_id(*, execution_id: str, turn: int, target: str) -> str:
    """Return the stable deterministic CrawlRequest identity for one
    inspection (same execution/turn/target -> same id; next turn -> distinct
    id). Never Python ``hash()``."""
    name = f"{execution_id}:{turn}:{target}"
    return str(uuid.uuid5(uuid.NAMESPACE_OID, name))


def authorize_recon_inspection(
    *,
    candidate: SourceCandidate,
    inspection: ReconInspection,
    settings: ReconSettings,
    turn: int,
    execution_id: str,
) -> CrawlRequest:
    """Convert one model inspection request into a normal PR 7 CrawlRequest.

    Trusted deterministic mapping rules:

    - the candidate ``entrypoint`` anchors the allowed origin;
    - targets resolve against that origin and must stay same-origin;
    - unsupported schemes, userinfo, and control characters are rejected;
    - credentials are always ``None`` (PR 10 delivers unauthenticated recon);
    - budgets come only from ``settings`` — never from the model;
    - the deterministic request identity is derived per execution/turn.

    :raises ReconInspectionRejectedError: when the request cannot be safely
        represented. The crawler is **never** called for a rejected request.
    """
    try:
        origin = AllowedOrigin.from_url(candidate.entrypoint)
    except InvalidCrawlRequest as exc:
        raise ReconInspectionRejectedError(
            "the candidate entrypoint is not a crawlable origin"
        ) from exc

    target = inspection.target
    try:
        parts = urlsplit(target)
    except ValueError as exc:
        raise ReconInspectionRejectedError("the requested target is malformed") from exc
    if parts.scheme not in ("", "http", "https"):
        raise ReconInspectionRejectedError(
            "the requested target uses an unsupported scheme"
        )
    if parts.username is not None or parts.password is not None:
        raise ReconInspectionRejectedError(
            "the requested target must not contain userinfo"
        )
    if any(ord(ch) < 32 for ch in target):
        raise ReconInspectionRejectedError(
            "the requested target must not contain control characters"
        )

    start_url = urljoin(candidate.entrypoint, target)
    if not origin.contains(start_url):
        raise ReconInspectionRejectedError(
            "the requested target is outside the candidate origin"
        )

    request_id = deterministic_recon_request_id(
        execution_id=execution_id,
        turn=turn,
        target=start_url,
    )
    try:
        return CrawlRequest(
            request_id=request_id,
            start_url=start_url,
            allowed_origin=origin,
            credentials=None,
            max_pages=settings.max_pages_per_inspection,
            max_requests=settings.max_requests_per_inspection,
            max_depth=settings.max_depth_per_inspection,
            timeout_seconds=settings.inspection_timeout_seconds,
        )
    except InvalidCrawlRequest as exc:
        raise ReconInspectionRejectedError(
            "the inspection cannot be represented by the Crawler contract"
        ) from exc


# ---------------------------------------------------------------------------
# Coordinator.
# ---------------------------------------------------------------------------

#: Span names for the reconnaissance workflow (bounded attributes only).
WORKFLOW_SPAN = "recon.workflow"
CRAWL_SPAN = "recon.crawl"

#: Metric names (bounded; no candidate IDs, no URLs, no content).
WORKFLOW_STARTED_METRIC = "darkula.recon.workflow.started"
WORKFLOW_COMPLETED_METRIC = "darkula.recon.workflow.completed"
FAILURES_METRIC = "darkula.recon.failures"
INSPECTIONS_METRIC = "darkula.recon.inspections"
INVALID_REQUESTS_METRIC = "darkula.recon.invalid_requests"
WORKFLOW_DURATION_METRIC = "darkula.recon.workflow.duration"

#: Outcome attribute used on completion/failure counters.
_OUTCOME_ATTR = "darkula.outcome"
_DISPOSITION_ATTR = "darkula.recon.disposition"
_FAILURE_CATEGORY_ATTR = "darkula.recon.failure_category"


def _failure_category(error: ReconError) -> str:
    """Return the bounded category label of one typed recon failure."""
    if isinstance(error, ReconCandidateNotFoundError):
        return "candidate_not_found"
    if isinstance(error, ReconCandidateNotEligibleError):
        return "candidate_not_eligible"
    if isinstance(error, ReconProtocolError):
        return "protocol"
    if isinstance(error, ReconBudgetExceededError):
        return "budget"
    if isinstance(error, ReconInspectionRejectedError):
        return "inspection_rejected"
    return "execution"


@dataclass(frozen=True, slots=True)
class ReconWorkflowResult:
    """Structured outcome of one :meth:`ReconCoordinator.reconnoiter` call.

    ``final_status``/``assessment_id`` reflect the durable lifecycle result;
    ``disposition`` is the analytical recommendation when one was produced.
    """

    candidate_id: SourceCandidateId
    final_status: CandidateStatus
    disposition: ReconDisposition | None
    assessment_id: ReconAssessmentId | None
    turns: int
    inspections: int
    evidence_items: int


class ReconCoordinator:
    """Coordinator-owned bounded reconnaissance workflow.

    Owns all candidate lifecycle mutation and every work authorization.
    Holds no PostgreSQL transaction across LLM/crawler I/O: a short start
    UoW (load + expected-state transition) commits first, the bounded
    agent/crawler loop runs with no open UoW, and one final UoW appends the
    assessment and applies the terminal transition atomically.
    """

    def __init__(
        self,
        *,
        spi: DarkulaSpi,
        agent: ReconAgent,
        crawler: Crawler,
        settings: ReconSettings,
        clock: Callable[[], datetime] | None = None,
        execution_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._spi = spi
        self._agent = agent
        self._crawler = crawler
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._execution_id_factory = execution_id_factory or (lambda: str(uuid.uuid4()))

    # -- public API --------------------------------------------------------

    async def reconnoiter(self, candidate_id: SourceCandidateId) -> ReconWorkflowResult:
        """Run one bounded reconnaissance execution and return its result.

        :raises ReconCandidateNotFoundError: the candidate does not exist.
        :raises ReconCandidateNotEligibleError: the candidate is terminal or
            already under reconnaissance (includes lost concurrent starts).
        :raises ReconProtocolError / ReconBudgetExceededError /
            ReconInspectionRejectedError / ReconExecutionError: bounded
            workflow failures — never analytical dispositions.
        :raises asyncio.CancelledError: cancellation propagates unchanged
            with no assessment and no lifecycle event.
        """
        start_ns = time.perf_counter_ns()
        tracer = get_tracer()
        with tracer.start_as_current_span(WORKFLOW_SPAN):
            try:
                result = await self._reconnoiter(candidate_id)
            except asyncio.CancelledError:
                raise
            except ReconError as exc:
                get_counter(FAILURES_METRIC).add(
                    1, {_FAILURE_CATEGORY_ATTR: _failure_category(exc)}
                )
                raise
        get_histogram(WORKFLOW_DURATION_METRIC).record(
            (time.perf_counter_ns() - start_ns) / 1e9
        )
        return result

    async def _reconnoiter(
        self, candidate_id: SourceCandidateId
    ) -> ReconWorkflowResult:
        now = self._clock()
        execution_id = self._execution_id_factory()

        # -- short start UoW: load + expected-state transition -------------
        async with self._spi.unit_of_work() as uow:
            candidate = await uow.source_candidates.get(candidate_id)
            if candidate is None:
                raise ReconCandidateNotFoundError(
                    "the candidate does not exist; no reconnaissance is started"
                )
            self._assert_startable(candidate)
            started_event = SourceCandidateEventHistory(
                event_id=CandidateEventId.generate(),
                candidate_id=candidate_id,
                event_type=CandidateEventType.RECONNAISSANCE_STARTED,
                occurred_at=now,
                context={"recon_prompt_version": RECON_PROMPT_VERSION},
                reason="Coordinator started bounded reconnaissance",
            )
            try:
                await uow.source_candidates.transition(
                    candidate_id,
                    expected=candidate.status,
                    new_status=CandidateStatus.UNDER_RECONNAISSANCE,
                    event=started_event,
                )
            except ConflictError:
                # The concurrent-start loser lands here when its own
                # transaction read the pre-commit status: the atomic stored
                # function detects the drift. Proven by the real-PostgreSQL
                # concurrent-start integration (RP3).
                raise ReconCandidateNotEligibleError(  # pragma: no cover - RP3
                    "a concurrent reconnaissance already started; no duplicate work"
                ) from None
            await uow.commit()
        get_counter(WORKFLOW_STARTED_METRIC).add(1)

        # -- bounded agent/crawler loop (no open UoW) ----------------------
        evidence = ReconEvidenceContext(settings=self._settings)
        turns = 0
        inspections = 0
        completion: ReconCompletion | None = None
        for turn in range(1, self._settings.max_turns + 1):
            turns = turn
            decision = await self._agent_decision(
                candidate, turn=turn, evidence=evidence
            )
            if decision.action is ReconAction.COMPLETE:
                if decision.completion is None:  # schema guarantee; defensive
                    raise ReconProtocolError(
                        "a COMPLETE decision has no completion payload"
                    )
                completion = decision.completion
                break
            # INSPECT
            if decision.inspection is None:  # schema guarantee; defensive
                raise ReconProtocolError(
                    "an INSPECT decision has no inspection payload"
                )
            if inspections >= self._settings.max_inspections:
                await self._recoverable_pending(candidate_id)
                raise ReconBudgetExceededError(
                    "reconnaissance exceeded the maximum number of inspections"
                )
            inspections += 1
            await self._inspect(
                candidate=candidate,
                inspection=decision.inspection,
                turn=turn,
                execution_id=execution_id,
                candidate_id=candidate_id,
                evidence=evidence,
            )

        if completion is None:
            await self._recoverable_pending(candidate_id)
            raise ReconBudgetExceededError(
                "reconnaissance exhausted its turn budget without a completion decision"
            )

        # -- validate the final recommendation ------------------------------
        await self._validate_completion(candidate_id, completion, evidence)

        # -- map to the existing domain assessment --------------------------
        assessed_at = self._clock()
        try:
            assessment = ReconAssessment(
                assessment_id=ReconAssessmentId.generate(),
                candidate_id=candidate_id,
                assessed_at=assessed_at,
                disposition=completion.disposition,
                confidence=Confidence(completion.confidence),
                evidence_references=tuple(completion.evidence_references),
                characteristics=(
                    dict(completion.characteristics)
                    if completion.characteristics
                    else None
                ),
            )
        except (ValueError, TypeError) as exc:
            await self._recoverable_pending(candidate_id)
            raise ReconProtocolError(
                "the completion decision cannot be represented"
            ) from exc

        final_status, final_event = _final_transition(completion.disposition)

        # -- one final UoW: append assessment + transition atomically -------
        final_event_record = SourceCandidateEventHistory(
            event_id=CandidateEventId.generate(),
            candidate_id=candidate_id,
            event_type=final_event,
            occurred_at=self._clock(),
            context={"recon_prompt_version": RECON_PROMPT_VERSION},
            reason=_final_reason(final_event),
        )
        async with self._spi.unit_of_work() as uow:
            fresh = await uow.source_candidates.get(candidate_id)
            if (
                fresh is None
                or fresh.status is not CandidateStatus.UNDER_RECONNAISSANCE
            ):
                raise ReconExecutionError(
                    "the candidate changed state during reconnaissance; "
                    "the final transaction was rolled back"
                )
            try:
                await uow.source_candidates.append_recon_assessment(assessment)
                await uow.source_candidates.transition(
                    candidate_id,
                    expected=CandidateStatus.UNDER_RECONNAISSANCE,
                    new_status=final_status,
                    event=final_event_record,
                )
                await uow.commit()
            except ConflictError:
                # Defensive: within one PostgreSQL transaction a successful
                # append cannot be followed by a conflicting transition (the
                # fresh-status check above already covers state drift). The
                # rollback guarantee for this branch is proven by the
                # real-PostgreSQL final-conflict integration (RP7).
                raise ReconExecutionError(  # pragma: no cover - defensive
                    "a concurrent lifecycle change raced the final transition; "
                    "assessment and transition were rolled back together"
                ) from None

        get_counter(WORKFLOW_COMPLETED_METRIC).add(
            1, {_DISPOSITION_ATTR: completion.disposition.value}
        )
        return ReconWorkflowResult(
            candidate_id=candidate_id,
            final_status=final_status,
            disposition=completion.disposition,
            assessment_id=assessment.assessment_id,
            turns=turns,
            inspections=inspections,
            evidence_items=len(evidence),
        )

    # -- internal steps -----------------------------------------------------

    def _assert_startable(self, candidate: SourceCandidate) -> None:
        """Reject terminal/busy candidates before any transition (no-work
        replay rule)."""
        if candidate.status is CandidateStatus.UNDER_RECONNAISSANCE:
            raise ReconCandidateNotEligibleError(
                "the candidate is already under reconnaissance; no duplicate work"
            )
        if candidate.status not in (
            CandidateStatus.DISCOVERED,
            CandidateStatus.RECONNAISSANCE_PENDING,
        ):
            raise ReconCandidateNotEligibleError(
                "the candidate is not eligible for reconnaissance "
                "(terminal status replay performs no LLM or crawler work)"
            )

    async def _agent_decision(
        self,
        candidate: SourceCandidate,
        *,
        turn: int,
        evidence: ReconEvidenceContext,
    ) -> ReconAgentDecision:
        """One agent decision; operational model failures are typed and the
        candidate is returned to a retryable pending state."""
        try:
            return await self._agent.decide(
                candidate_entrypoint=candidate.entrypoint,
                turn=turn,
                evidence=evidence,
            )
        except asyncio.CancelledError:
            raise
        except LlmError as exc:
            await self._recoverable_pending(candidate.candidate_id)
            raise ReconExecutionError(
                f"the model operation failed ({exc.code.value})"
            ) from exc
        except ReconError:
            await self._recoverable_pending(candidate.candidate_id)
            raise

    async def _inspect(
        self,
        *,
        candidate: SourceCandidate,
        inspection: ReconInspection,
        turn: int,
        execution_id: str,
        candidate_id: SourceCandidateId,
        evidence: ReconEvidenceContext,
    ) -> CrawlResult:
        """Authorize one inspection, execute it through the existing Crawler,
        and bound-append its pages to the evidence registry."""
        tracer = get_tracer()
        try:
            request = authorize_recon_inspection(
                candidate=candidate,
                inspection=inspection,
                settings=self._settings,
                turn=turn,
                execution_id=execution_id,
            )
        except ReconInspectionRejectedError:
            get_counter(INVALID_REQUESTS_METRIC).add(1)
            await self._recoverable_pending(candidate_id)
            raise
        try:
            with tracer.start_as_current_span(CRAWL_SPAN):
                result = await self._crawler.crawl(request)
        except asyncio.CancelledError:
            raise
        except ReconError:
            raise
        except Exception as exc:
            await self._recoverable_pending(candidate_id)
            raise ReconExecutionError(
                "an authorized inspection crawl failed operationally"
            ) from exc
        if result.status is not CrawlStatus.COMPLETED:
            await self._recoverable_pending(candidate_id)
            raise ReconExecutionError(
                f"the inspection crawl ended in state {result.status.value}"
            )
        get_counter(INSPECTIONS_METRIC).add(1)
        for index, page in enumerate(result.pages, start=1):
            if len(evidence) >= self._settings.max_evidence_items:
                break
            reference = evidence_reference(
                execution_id=execution_id, turn=turn, sequence=index
            )
            item = ReconEvidenceItem(
                reference=reference,
                url=page.url,
                title=_truncate_text(
                    page.title, max_chars=self._settings.max_excerpt_chars
                ),
                excerpt=_truncate_text(
                    page.text_excerpt, max_chars=self._settings.max_excerpt_chars
                ),
                provenance=f"crawl:{result.request_id}",
                sequence=index,
            )
            evidence.add(item)
        return result

    async def _validate_completion(
        self,
        candidate_id: SourceCandidateId,
        completion: ReconCompletion,
        evidence: ReconEvidenceContext,
    ) -> None:
        """Validate every model-returned reference against the trusted
        registry before persistence; unknown references fail closed."""
        known = set(evidence.references())
        unknown = [ref for ref in completion.evidence_references if ref not in known]
        if unknown:
            await self._recoverable_pending(candidate_id)
            raise ReconProtocolError(
                "the completion decision cited unknown evidence references"
            )
        if len(completion.evidence_references) > self._settings.max_evidence_items:
            await self._recoverable_pending(candidate_id)
            raise ReconProtocolError(
                "the completion decision exceeded the evidence reference bound"
            )

    async def _recoverable_pending(self, candidate_id: SourceCandidateId) -> None:
        """Best-effort UNDER_RECONNAISSANCE -> RECONNAISSANCE_PENDING retry
        transition for recoverable failures.

        Appends **no** assessment and never fabricates an analytical
        disposition: the ``NEEDS_MORE_RECON`` event honestly records that
        reconnaissance did not conclude and a later execution may retry.
        Cancellation is never routed through this path.
        """
        try:
            async with self._spi.unit_of_work() as uow:
                fresh = await uow.source_candidates.get(candidate_id)
                if (
                    fresh is None
                    or fresh.status is not CandidateStatus.UNDER_RECONNAISSANCE
                ):
                    await uow.commit()
                    return
                await uow.source_candidates.transition(
                    candidate_id,
                    expected=CandidateStatus.UNDER_RECONNAISSANCE,
                    new_status=CandidateStatus.RECONNAISSANCE_PENDING,
                    event=SourceCandidateEventHistory(
                        event_id=CandidateEventId.generate(),
                        candidate_id=candidate_id,
                        event_type=CandidateEventType.NEEDS_MORE_RECON,
                        occurred_at=self._clock(),
                        context={"recon_prompt_version": RECON_PROMPT_VERSION},
                        reason=(
                            "reconnaissance could not complete; retryable pending "
                            "state (no analytical disposition)"
                        ),
                    ),
                )
                await uow.commit()
        except ConflictError, PersistenceError:
            # Best effort only: a concurrent actor already resolved the
            # lifecycle or persistence is unavailable; the typed workflow
            # failure still propagates to the caller.
            return


def _final_transition(
    disposition: ReconDisposition,
) -> tuple[CandidateStatus, CandidateEventType]:
    """Map one analytical disposition to the Coordinator-owned final
    lifecycle transition (section 6 of the PR 10 plan)."""
    if disposition is ReconDisposition.QUALIFY:
        return CandidateStatus.QUALIFIED, CandidateEventType.QUALIFIED
    if disposition is ReconDisposition.NEEDS_MORE_RECON:
        return (
            CandidateStatus.RECONNAISSANCE_PENDING,
            CandidateEventType.NEEDS_MORE_RECON,
        )
    return CandidateStatus.REJECTED, CandidateEventType.REJECTED


def _final_reason(event: CandidateEventType) -> str:
    """One bounded human-safe reason for the final lifecycle event."""
    if event is CandidateEventType.QUALIFIED:
        return "reconnaissance completed with a QUALIFY recommendation"
    if event is CandidateEventType.NEEDS_MORE_RECON:
        return "reconnaissance completed with a NEEDS_MORE_RECON recommendation"
    return "reconnaissance completed with a REJECT recommendation"


__all__ = [
    "CRAWL_SPAN",
    "FAILURES_METRIC",
    "INSPECTIONS_METRIC",
    "INVALID_REQUESTS_METRIC",
    "MAX_CHARACTERISTICS_JSON_BYTES",
    "MAX_CHARACTERISTIC_KEYS",
    "MAX_CHARACTERISTIC_VALUE_LENGTH",
    "MAX_EVIDENCE_REFERENCES",
    "MAX_PURPOSE_LENGTH",
    "MAX_TARGET_LENGTH",
    "RECON_AGENT_NAME",
    "RECON_OPERATION_NAME",
    "RECON_PROMPT_VERSION",
    "WORKFLOW_COMPLETED_METRIC",
    "WORKFLOW_DURATION_METRIC",
    "WORKFLOW_SPAN",
    "WORKFLOW_STARTED_METRIC",
    "ReconAction",
    "ReconAgentDecision",
    "ReconBudgetExceededError",
    "ReconCandidateNotEligibleError",
    "ReconCandidateNotFoundError",
    "ReconCompletion",
    "ReconCoordinator",
    "ReconError",
    "ReconEvidenceContext",
    "ReconEvidenceItem",
    "ReconExecutionError",
    "ReconInspection",
    "ReconInspectionRejectedError",
    "ReconProtocolError",
    "ReconWorkflowResult",
    "authorize_recon_inspection",
    "build_evidence_context",
    "deterministic_recon_request_id",
    "evidence_reference",
]
