# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded semantic identity types and shared Fake World validation helpers.

Semantic identity is distinct from presentation identity: rendered URLs,
page numbers, display names, HTML element ids, and filenames are
presentation, never these stable value objects.

Rules frozen here (PR 6 invariants):

- identity values are immutable, non-blank, control-free, and bounded;
- scenario/behavior identities additionally follow a bounded ASCII
  ``[A-Za-z0-9-]`` shape so downstream tooling and telemetry treat them as
  safe tokens;
- Fake World timestamps are fixed timezone-aware UTC values; naive and
  wall-clock times are rejected so rendering never depends on ``now``;
- ``split_ref`` parses stable ``kind:value`` traceability/relationship
  references deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

MAX_SEMANTIC_ID_LENGTH = 200

#: Control characters (C0 plus DEL) are rejected wherever an identity may
#: reach logs, telemetry, URLs, or HTML output.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: Conservative valid token shape for scenario and behavior identities.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{1,63}$")

#: Behavior identifiers follow the documented convention, for example
#: ``FW-BG-AUTH-001``.
BEHAVIOR_ID_RE = re.compile(r"^FW-[A-Z0-9]+(?:-[A-Z0-9]+)+-\d{2,}$")


class FakeWorldValidationError(ValueError):
    """Typed validation failure for Fake World definitions/requests.

    Deliberately distinct from *source-level behavior* (404/429/503/redirect,
    which are rendered responses): this exception signals a scenario or
    programming error to test/authoring code and is never rendered.
    """


def _validate_semantic_id(
    type_name: str,
    raw: str,
    *,
    max_length: int,
    match: re.Pattern[str] | None,
) -> str:
    """Return a trimmed, non-blank, bounded, control-free semantic id."""
    if not isinstance(raw, str):
        raise FakeWorldValidationError(f"{type_name} must be a string")
    value = raw.strip()
    if not value:
        raise FakeWorldValidationError(f"{type_name} must not be blank")
    if len(value) > max_length:
        raise FakeWorldValidationError(
            f"{type_name} must not exceed {max_length} characters"
        )
    if _CONTROL_CHARS.search(value):
        raise FakeWorldValidationError(
            f"{type_name} must not contain control characters"
        )
    if match is not None and match.fullmatch(value) is None:
        raise FakeWorldValidationError(
            f"{type_name} must match {match.pattern!r} (got {value!r})"
        )
    return value


@dataclass(frozen=True, slots=True)
class SemanticId:
    """Base class for stable opaque Fake World semantic identities.

    Subclasses are distinct identity types: two IDs of different classes
    never compare equal even with the same value.
    """

    value: str
    max_length: ClassVar[int] = MAX_SEMANTIC_ID_LENGTH
    match: ClassVar[re.Pattern[str] | None] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _validate_semantic_id(
                type(self).__name__,
                self.value,
                max_length=self.max_length,
                match=self.match,
            ),
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ScenarioId(SemanticId):
    """Stable identity of one logical Fake World scenario."""

    match: ClassVar[re.Pattern[str]] = _TOKEN_RE


@dataclass(frozen=True, slots=True)
class SourceId(SemanticId):
    """Stable semantic identity of one fictional source."""


@dataclass(frozen=True, slots=True)
class BoardId(SemanticId):
    """Stable semantic identity of one fictional forum board."""


@dataclass(frozen=True, slots=True)
class ThreadId(SemanticId):
    """Stable semantic identity of one fictional forum thread."""


@dataclass(frozen=True, slots=True)
class PostId(SemanticId):
    """Stable semantic identity of one fictional forum post."""


@dataclass(frozen=True, slots=True)
class AttachmentId(SemanticId):
    """Stable semantic identity of one fictional attachment."""


@dataclass(frozen=True, slots=True)
class ForumAliasId(SemanticId):
    """Stable semantic identity of one rendered forum alias."""


@dataclass(frozen=True, slots=True)
class ActorId(SemanticId):
    """Stable semantic identity of one fictional world actor (truth)."""


@dataclass(frozen=True, slots=True)
class AliasId(SemanticId):
    """Stable semantic identity of one world-level alias (truth).

    ``AliasId`` belongs to the truth ontology and is distinguished from
    ``ForumAliasId`` which identifies an alias as rendered by one source.
    """


@dataclass(frozen=True, slots=True)
class OrganizationId(SemanticId):
    """Stable semantic identity of one fictional organization (truth)."""


@dataclass(frozen=True, slots=True)
class LocationId(SemanticId):
    """Stable semantic identity of one fictional location (truth)."""


@dataclass(frozen=True, slots=True)
class RelationshipId(SemanticId):
    """Stable semantic identity of one world relationship (truth)."""


@dataclass(frozen=True, slots=True)
class EventId(SemanticId):
    """Stable semantic identity of one world event (truth)."""


@dataclass(frozen=True, slots=True)
class BehaviorId(SemanticId):
    """Stable identity of one significant canonical Fake World behavior.

    Convention (documented in ``docs/FAKE_WORLD.md``): ``FW-<source>-<area>-NNN``,
    for example ``FW-BG-AUTH-001``.
    """

    match: ClassVar[re.Pattern[str]] = BEHAVIOR_ID_RE


@dataclass(frozen=True, slots=True)
class ScenarioVersion:
    """Positive integer version of one logical scenario."""

    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise FakeWorldValidationError("scenario version must be an integer")
        if self.value < 1:
            raise FakeWorldValidationError(
                "scenario version must be a positive integer"
            )

    def __str__(self) -> str:
        return str(self.value)


def require_utc(value: datetime, *, field_name: str) -> None:
    """Reject naive or non-UTC timestamps (determinism rule)."""
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(None):
        raise FakeWorldValidationError(
            f"{field_name} must be timezone-aware UTC (naive and wall-clock "
            "timestamps are rejected for determinism)"
        )


def split_ref(ref: str) -> tuple[str, str]:
    """Split a stable ``kind:value`` reference deterministically.

    ``split_ref("post:p-host-creds-1") -> ("post", "p-host-creds-1")``.
    """
    if not isinstance(ref, str) or ":" not in ref:
        raise FakeWorldValidationError(
            f"reference must have the form 'kind:value' (got {ref!r})"
        )
    kind, value = ref.split(":", 1)
    kind = kind.strip()
    value = value.strip()
    if not kind or not value:
        raise FakeWorldValidationError(
            f"reference kind and value must not be blank (got {ref!r})"
        )
    if _CONTROL_CHARS.search(kind) or _CONTROL_CHARS.search(value):
        raise FakeWorldValidationError(
            "reference kind/value must not contain control characters"
        )
    return kind, value


__all__ = [
    "BEHAVIOR_ID_RE",
    "MAX_SEMANTIC_ID_LENGTH",
    "ActorId",
    "AliasId",
    "AttachmentId",
    "BehaviorId",
    "BoardId",
    "EventId",
    "FakeWorldValidationError",
    "ForumAliasId",
    "LocationId",
    "OrganizationId",
    "PostId",
    "RelationshipId",
    "ScenarioId",
    "ScenarioVersion",
    "SemanticId",
    "SourceId",
    "ThreadId",
    "require_utc",
    "split_ref",
]
