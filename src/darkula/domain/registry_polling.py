# SPDX-License-Identifier: AGPL-3.0-only
"""Durable registry-poll checkpoint value (PR 18).

``RegistryPollCheckpoint`` is **polling state**, not a source, candidate,
policy, or authorization. It records, for one allowlisted registry
provider, the last exact upstream entry name whose complete set of
observations was durably ingested, plus a bounded cooldown and the
optimistic ``generation`` used for compare-and-swap advancement.

Invariants frozen here:

- ``generation == 0`` means "not yet persisted"; the first durable
  write creates ``generation == 1``;
- ``cursor`` is an exact, bounded upstream entry name (never a URL, broker
  offset, source identity, or candidate identity). ``None`` is the
  explicit end-of-pass state;
- every timestamp is timezone-aware UTC (naive values are rejected);
- the value carries no provider payload, secret, locator, or candidate
  reference.

The application layer (``darkula.app.registry_polling``) owns selection,
HTTP I/O, ingestion ordering, and the rule that the checkpoint advances
only *after* every accepted observation committed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

#: Bounded cursor/provider limits (Unicode code points / ``char_length``).
MAX_REGISTRY_PROVIDER_ID_LENGTH = 100
MAX_REGISTRY_CURSOR_LENGTH = 200

#: Provider identity is the same stable lower-case name used by PR 17.
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    """Return ``value`` normalized to timezone-aware UTC (naive rejected)."""
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(UTC)


def _validate_provider_id(value: str) -> str:
    """Validate the stable lower-case provider identity."""
    if not isinstance(value, str):
        raise ValueError("provider_id must be a string")
    if len(value) > MAX_REGISTRY_PROVIDER_ID_LENGTH:
        raise ValueError(
            f"provider_id must not exceed {MAX_REGISTRY_PROVIDER_ID_LENGTH} characters"
        )
    if _PROVIDER_ID.fullmatch(value) is None:
        raise ValueError("provider_id must be lower-case ASCII [a-z0-9][a-z0-9._-]*")
    return value


def validate_registry_cursor(value: str) -> str:
    """Return one exact, bounded, control-free upstream entry name.

    Leading/trailing whitespace is rejected (never silently trimmed):
    the cursor is an exact key in a bytewise-sorted upstream index, so
    silently changing it would skip or repeat entries.
    """
    if not isinstance(value, str):
        raise ValueError("registry cursor must be a string")
    if not value or not value.strip():
        raise ValueError("registry cursor must not be blank")
    if value != value.strip():
        raise ValueError("registry cursor must not have leading/trailing whitespace")
    if len(value) > MAX_REGISTRY_CURSOR_LENGTH:
        raise ValueError(
            f"registry cursor must not exceed {MAX_REGISTRY_CURSOR_LENGTH} characters"
        )
    if _CONTROL_CHARS.search(value):
        raise ValueError("registry cursor must not contain control characters")
    return value


@dataclass(frozen=True, slots=True)
class RegistryPollCheckpoint:
    """One provider's durable polling position (PR 18)."""

    provider_id: str
    cursor: str | None
    generation: int
    cooldown_until: datetime | None = None
    last_completed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _validate_provider_id(self.provider_id))
        if self.cursor is not None:
            object.__setattr__(self, "cursor", validate_registry_cursor(self.cursor))
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise ValueError("registry checkpoint generation must be an integer")
        if self.generation < 0:
            raise ValueError("registry checkpoint generation must be >= 0")
        if self.cooldown_until is not None:
            object.__setattr__(
                self,
                "cooldown_until",
                _require_utc(self.cooldown_until, field_name="cooldown_until"),
            )
        if self.last_completed_at is not None:
            object.__setattr__(
                self,
                "last_completed_at",
                _require_utc(self.last_completed_at, field_name="last_completed_at"),
            )

    @classmethod
    def unpersisted(cls, provider_id: str) -> RegistryPollCheckpoint:
        """Return the absent-row value (``generation == 0``, no cursor)."""
        return cls(provider_id=provider_id, cursor=None, generation=0)


__all__ = [
    "MAX_REGISTRY_CURSOR_LENGTH",
    "MAX_REGISTRY_PROVIDER_ID_LENGTH",
    "RegistryPollCheckpoint",
    "validate_registry_cursor",
]
