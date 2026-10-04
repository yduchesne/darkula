# SPDX-License-Identifier: AGPL-3.0-only
"""Executable managed-source collection concepts (PR 9).

PR 9 freezes the deterministic, policy-authorized collection lifecycle that
connects the PR 7 crawler and the PR 8 content-ingestion boundary:

- :class:`CollectionPolicy` — durable, reusable authorization/
  configuration for one managed :class:`~darkula.domain.source.Source`
  (a logical source, never a URI);
- :class:`CollectionRun` — one immutable historical execution record of a
  policy occurrence (``(policy_id, scheduled_for)``), with its frozen
  execution snapshot so later policy edits never rewrite run history.

Rules frozen here (PR 9 invariants):

- ``Source`` is logical identity; ``SourceEndpoint`` is a locator;
  :class:`CollectionPolicyId`, :class:`CollectionRunId`, the broker
  ``message_id``, and broker positions are distinct identities — a URI or
  broker offset is never collection identity;
- policy stores credential **references** only, never secret values;
- schedule semantics are the smallest deterministic v0.1 model: a positive
  bounded ``interval_seconds`` and an explicit UTC ``next_due_at``; the
  occurrence identity is ``(policy_id, scheduled_for)``;
- runs are historical and immutable: a run carries ``policy_revision`` and
  the exact execution snapshot needed to explain its authorization;
- the state machine is finite and explicit: ``QUEUED -> RUNNING ->
  SUCCEEDED/FAILED/CANCELLED`` (``QUEUED -> CANCELLED`` allowed);
- all timestamps are timezone-aware UTC (naive datetimes rejected);
- every budget mirrors the PR 7 ``CrawlRequest`` bounds (the PR 7
  controller remains the enforcement boundary).

Deliberately NOT frozen here: cron/calendar scheduling (STOP condition 5),
authenticated collection (PR 9 delivers unauthenticated crawling; policy
``authentication_reference`` is metadata only), retry backoff, and any
agentic/recon semantics (PR 10).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    SourceEndpointId,
    SourceId,
)

#: Smallest allowed policy interval (seconds) — bounded, positive schedule.
MIN_POLICY_INTERVAL_SECONDS = 60

#: Largest allowed policy interval (seconds) — one calendar year.
MAX_POLICY_INTERVAL_SECONDS = 365 * 24 * 60 * 60

#: Maximum number of authorized endpoints one policy may list.
MAX_POLICY_ENDPOINTS = 64

#: Maximum number of allowed path entries (reserved schedule metadata).
MAX_ALLOWED_PATHS = 64

#: Per-path text bound.
MAX_ALLOWED_PATH_LENGTH = 512

#: Authentication references are references only: bounded, non-secret text.
MAX_AUTHENTICATION_REFERENCE_LENGTH = 512

#: Failure summaries are bounded/sanitized never raw exception/URI/content.
MAX_FAILURE_SUMMARY_LENGTH = 1024

#: Budget bounds mirror the PR 7 CrawlRequest validation limits exactly.
MAX_POLICY_PAGES = 10_000
MAX_POLICY_REQUESTS = 100_000
MAX_POLICY_DEPTH = 64
MAX_POLICY_TIMEOUT_SECONDS = 86_400.0

#: Secret-like markers rejected in free-form collection text (parity with
#: the source/content domains and the configuration loader).
METADATA_SECRET_MARKERS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "auth",
    "cookie",
    "access_key",
    "session",
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class CollectionRunStatus(StrEnum):
    """Finite lifecycle state of one :class:`CollectionRun`.

    Terminals: ``SUCCEEDED`` / ``FAILED`` / ``CANCELLED``. A terminal run is
    historical and immutable; a later schedule creates a new run.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: Statuses that end a run's lifecycle.
TERMINAL_RUN_STATUSES: frozenset[CollectionRunStatus] = frozenset(
    {
        CollectionRunStatus.SUCCEEDED,
        CollectionRunStatus.FAILED,
        CollectionRunStatus.CANCELLED,
    }
)


class CollectionFailureCode(StrEnum):
    """Bounded durable failure vocabulary persisted on failed runs.

    Sanitized categories only: never provider class names, raw exception
    text, URIs, content, or credentials.
    """

    SOURCE_INACTIVE = "SOURCE_INACTIVE"
    POLICY_INACTIVE = "POLICY_INACTIVE"
    NO_ACTIVE_ENDPOINT = "NO_ACTIVE_ENDPOINT"
    CRAWL_FAILED = "CRAWL_FAILED"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
    CONTENT_PERSISTENCE_FAILED = "CONTENT_PERSISTENCE_FAILED"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
    INVALID_STATE = "INVALID_STATE"


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    """Return ``value`` normalized to timezone-aware UTC."""
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


def validate_failure_summary(value: str | None) -> str | None:
    """Return a bounded, sanitized failure summary or ``None``.

    Rejects control characters and secret-like values; the caller must only
    ever pass category-level summaries (never raw exception text, URIs,
    content, or credentials).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("failure summary must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("failure summary must not be blank")
    if len(stripped) > MAX_FAILURE_SUMMARY_LENGTH:
        raise ValueError(
            f"failure summary must not exceed {MAX_FAILURE_SUMMARY_LENGTH} characters"
        )
    if _CONTROL_CHARS.search(stripped):
        raise ValueError("failure summary must not contain control characters")
    lowered = stripped.lower()
    if any(marker in lowered for marker in METADATA_SECRET_MARKERS):
        raise ValueError("failure summary must not contain secret-like values")
    return stripped


def _validate_interval(seconds: int) -> int:
    """Validate one positive bounded schedule interval."""
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise ValueError("interval_seconds must be an integer")
    if seconds < MIN_POLICY_INTERVAL_SECONDS or seconds > MAX_POLICY_INTERVAL_SECONDS:
        raise ValueError(
            "interval_seconds must be within "
            f"[{MIN_POLICY_INTERVAL_SECONDS}, {MAX_POLICY_INTERVAL_SECONDS}]"
        )
    return seconds


def _validate_budget(value: int, *, name: str, maximum: int) -> int:
    """Validate one positive bounded collection budget."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be within [1, {maximum}]")
    return value


def _validate_path(path: str) -> str:
    """Validate one bounded, control-free allowed path entry."""
    if not isinstance(path, str):
        raise ValueError("allowed paths must be strings")
    stripped = path.strip()
    if not stripped:
        raise ValueError("allowed paths must not be blank")
    if len(stripped) > MAX_ALLOWED_PATH_LENGTH:
        raise ValueError(
            f"allowed paths must not exceed {MAX_ALLOWED_PATH_LENGTH} characters"
        )
    if _CONTROL_CHARS.search(stripped):
        raise ValueError("allowed paths must not contain control characters")
    lowered = stripped.lower()
    if any(marker in lowered for marker in METADATA_SECRET_MARKERS):
        raise ValueError("allowed paths must not contain secret-like values")
    return stripped


def _validate_paths(paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate the allowed-path collection: tuple of bounded entries.

    Deduplicates while preserving the deterministic first-seen order.
    """
    if isinstance(paths, (list, tuple)):
        paths = tuple(paths)
    else:
        raise ValueError("allowed_paths must be a tuple of strings")
    checked = tuple(_validate_path(path) for path in paths)
    return tuple(dict.fromkeys(checked))


def _validate_endpoint_ids(
    endpoint_ids: tuple[SourceEndpointId, ...],
) -> tuple[SourceEndpointId, ...]:
    """Validate a non-empty, unique set of authorized endpoint identities.

    A policy authorizes managed endpoints by identity (never by URI); the
    identity order is frozen here by sorting so snapshot/JSON serialization
    stays deterministic.
    """
    if not isinstance(endpoint_ids, tuple):
        raise ValueError("allowed_endpoint_ids must be a tuple")
    if not endpoint_ids:
        raise ValueError("a policy must authorize at least one endpoint")
    if len(endpoint_ids) > MAX_POLICY_ENDPOINTS:
        raise ValueError(
            f"a policy must authorize at most {MAX_POLICY_ENDPOINTS} endpoints"
        )
    for endpoint_id in endpoint_ids:
        if not isinstance(endpoint_id, SourceEndpointId):
            raise ValueError("allowed_endpoint_ids must hold SourceEndpointId values")
    if len(set(endpoint_ids)) != len(endpoint_ids):
        raise ValueError("allowed_endpoint_ids must not contain duplicates")
    return tuple(sorted(endpoint_ids, key=lambda item: str(item)))


def _validate_authentication_reference(value: str | None) -> str | None:
    """Validate a bounded, secret-free *reference* (never a secret value)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("authentication_reference must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("authentication_reference must not be blank")
    if len(stripped) > MAX_AUTHENTICATION_REFERENCE_LENGTH:
        raise ValueError(
            "authentication_reference must not exceed "
            f"{MAX_AUTHENTICATION_REFERENCE_LENGTH} characters"
        )
    if _CONTROL_CHARS.search(stripped):
        raise ValueError("authentication_reference must not contain control characters")
    lowered = stripped.lower()
    if any(marker in lowered for marker in METADATA_SECRET_MARKERS):
        raise ValueError(
            "authentication_reference must not contain secret-like values "
            "(references only, never credentials)"
        )
    return stripped


@dataclass(frozen=True, slots=True)
class CollectionPolicy:
    """Durable, reusable authorization/configuration for one Source.

    The policy belongs to a managed :class:`~darkula.domain.source.Source`
    (never to a URI) and authorizes managed
    :class:`~darkula.domain.source.SourceEndpoint` identities. ``revision``
    increments on every edit: historical runs reference the exact
    ``revision`` plus their frozen snapshot, so policy edits never rewrite
    run history.
    """

    policy_id: CollectionPolicyId
    source_id: SourceId
    active: bool
    created_at: datetime
    updated_at: datetime
    revision: int
    interval_seconds: int
    next_due_at: datetime
    allowed_endpoint_ids: tuple[SourceEndpointId, ...]
    allowed_paths: tuple[str, ...] = ()
    max_pages: int = 20
    max_requests: int = 60
    max_depth: int = 4
    timeout_seconds: float = 60.0
    authentication_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "created_at", _require_utc(self.created_at, field_name="created_at")
        )
        object.__setattr__(
            self, "updated_at", _require_utc(self.updated_at, field_name="updated_at")
        )
        object.__setattr__(
            self,
            "next_due_at",
            _require_utc(self.next_due_at, field_name="next_due_at"),
        )
        if not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        object.__setattr__(
            self,
            "interval_seconds",
            _validate_interval(self.interval_seconds),
        )
        object.__setattr__(
            self,
            "allowed_endpoint_ids",
            _validate_endpoint_ids(self.allowed_endpoint_ids),
        )
        object.__setattr__(
            self,
            "allowed_paths",
            _validate_paths(self.allowed_paths),
        )
        object.__setattr__(
            self,
            "max_pages",
            _validate_budget(
                self.max_pages, name="max_pages", maximum=MAX_POLICY_PAGES
            ),
        )
        object.__setattr__(
            self,
            "max_requests",
            _validate_budget(
                self.max_requests, name="max_requests", maximum=MAX_POLICY_REQUESTS
            ),
        )
        object.__setattr__(
            self,
            "max_depth",
            _validate_budget(
                self.max_depth, name="max_depth", maximum=MAX_POLICY_DEPTH
            ),
        )
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(
            self.timeout_seconds, bool
        ):
            raise ValueError("timeout_seconds must be a number")
        timeout = float(self.timeout_seconds)
        if timeout <= 0.0 or timeout > MAX_POLICY_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be within (0, {MAX_POLICY_TIMEOUT_SECONDS}]"
            )
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(
            self,
            "authentication_reference",
            _validate_authentication_reference(self.authentication_reference),
        )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")

    def execution_snapshot(self) -> dict[str, Any]:
        """Return the bounded, JSON-compatible execution snapshot.

        The snapshot freezes every field a run needs to explain its
        authorization deterministically: no URIs, no credentials, no
        unstructured policy JSON. Stored with each historical run.
        """
        return {
            "policy_revision": self.revision,
            "interval_seconds": self.interval_seconds,
            "allowed_endpoint_ids": [
                str(endpoint_id) for endpoint_id in self.allowed_endpoint_ids
            ],
            "allowed_paths": list(self.allowed_paths),
            "max_pages": self.max_pages,
            "max_requests": self.max_requests,
            "max_depth": self.max_depth,
            "timeout_seconds": self.timeout_seconds,
            "authentication_reference": self.authentication_reference,
        }


@dataclass(frozen=True, slots=True)
class CollectionRun:
    """One immutable historical execution of a policy occurrence.

    ``scheduled_for`` is the semantic occurrence identity (with
    ``policy_id``); ``policy_revision`` + ``policy_snapshot`` freeze the
    exact policy semantics this run was authorized under. Terminal runs are
    never rewritten by later policy edits.
    """

    run_id: CollectionRunId
    policy_id: CollectionPolicyId
    policy_revision: int
    source_id: SourceId
    scheduled_for: datetime
    created_at: datetime
    status: CollectionRunStatus
    policy_snapshot: Mapping[str, Any] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    execution_id: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    crawl_requests_attempted: int = 0
    pages_observed: int = 0
    content_observations: int = 0
    content_created: int = 0
    content_deduplicated: int = 0
    failure_code: CollectionFailureCode | None = None
    failure_summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scheduled_for",
            _require_utc(self.scheduled_for, field_name="scheduled_for"),
        )
        object.__setattr__(
            self, "created_at", _require_utc(self.created_at, field_name="created_at")
        )
        object.__setattr__(self, "policy_revision", int(self.policy_revision))
        if self.policy_revision < 1:
            raise ValueError("policy_revision must be a positive integer")
        if not isinstance(self.status, CollectionRunStatus):
            raise ValueError("status must be a CollectionRunStatus")
        if self.started_at is not None:
            object.__setattr__(
                self,
                "started_at",
                _require_utc(self.started_at, field_name="started_at"),
            )
        if self.completed_at is not None:
            object.__setattr__(
                self,
                "completed_at",
                _require_utc(self.completed_at, field_name="completed_at"),
            )
        if self.lease_expires_at is not None:
            object.__setattr__(
                self,
                "lease_expires_at",
                _require_utc(self.lease_expires_at, field_name="lease_expires_at"),
            )
        for name, value in (
            ("attempt_count", self.attempt_count),
            ("crawl_requests_attempted", self.crawl_requests_attempted),
            ("pages_observed", self.pages_observed),
            ("content_observations", self.content_observations),
            ("content_created", self.content_created),
            ("content_deduplicated", self.content_deduplicated),
        ):
            object.__setattr__(self, name, int(value))
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        object.__setattr__(
            self,
            "failure_summary",
            validate_failure_summary(self.failure_summary),
        )
        # Status/timestamp consistency (CR2/CR3): RUNNING implies started_at;
        # terminal implies completed_at; QUEUED implies neither.
        if self.status is CollectionRunStatus.RUNNING and self.started_at is None:
            raise ValueError("a RUNNING run requires started_at")
        if self.status in TERMINAL_RUN_STATUSES and self.completed_at is None:
            raise ValueError("a terminal run requires completed_at")
        if self.status is CollectionRunStatus.QUEUED and (
            self.started_at is not None or self.completed_at is not None
        ):
            raise ValueError("a QUEUED run must not carry started/completed timestamps")


def validate_counter_deltas(
    *,
    crawl_requests_attempted: int,
    pages_observed: int,
    content_observations: int,
    content_created: int,
    content_deduplicated: int,
) -> None:
    """Validate finalization counter deltas before they are persisted."""
    for name, value in (
        ("crawl_requests_attempted", crawl_requests_attempted),
        ("pages_observed", pages_observed),
        ("content_observations", content_observations),
        ("content_created", content_created),
        ("content_deduplicated", content_deduplicated),
    ):
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")


__all__ = [
    "MAX_ALLOWED_PATHS",
    "MAX_ALLOWED_PATH_LENGTH",
    "MAX_AUTHENTICATION_REFERENCE_LENGTH",
    "MAX_FAILURE_SUMMARY_LENGTH",
    "MAX_POLICY_DEPTH",
    "MAX_POLICY_ENDPOINTS",
    "MAX_POLICY_INTERVAL_SECONDS",
    "MAX_POLICY_PAGES",
    "MAX_POLICY_REQUESTS",
    "MAX_POLICY_TIMEOUT_SECONDS",
    "METADATA_SECRET_MARKERS",
    "MIN_POLICY_INTERVAL_SECONDS",
    "TERMINAL_RUN_STATUSES",
    "CollectionFailureCode",
    "CollectionPolicy",
    "CollectionRun",
    "CollectionRunStatus",
    "validate_counter_deltas",
    "validate_failure_summary",
]
