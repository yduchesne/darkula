# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, secret-safe telemetry attribute vocabulary (PR 3).

Decorator static attributes are the only attributes the Darkula
``traced``/``timed``/``counted`` helpers may attach: they are developer-
controlled constants validated at decoration time. This module enforces the
bounds (length, control characters) and rejects secret-like keys **and**
values so a caller mistake can never route prompts, tokens, credentials, or
unbounded content into telemetry.
"""

from __future__ import annotations

from collections.abc import Mapping

#: Upper bound for any telemetry attribute value.
MAX_ATTRIBUTE_VALUE_LENGTH = 256

#: Upper bound for any telemetry attribute key.
MAX_ATTRIBUTE_KEY_LENGTH = 200

#: Canonical duration-outcome attribute key.
OUTCOME = "darkula.outcome"

#: Outcome value for a successful operation.
SUCCESS_OUTCOME = "success"

#: Outcome value for an operation that raised an ordinary exception.
ERROR_OUTCOME = "error"

#: Secret-like markers rejected in both keys and values (fail closed).
SECRET_MARKERS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "credentials",
    "auth",
    "cookie",
    "session",
    "prompt",
)


def _reject_secret_like(name: str) -> None:
    """Raise ``ValueError`` when a name contains a secret marker."""
    lowered = name.lower()
    if any(marker in lowered for marker in SECRET_MARKERS):
        raise ValueError("telemetry attributes must not carry secret-like names/values")


def _validate_key(key: object) -> str:
    """Return a bounded, control-free attribute key or raise."""
    if not isinstance(key, str):
        raise ValueError("static attribute names must be strings")
    value = key.strip()
    if not value:
        raise ValueError("static attribute names must not be blank")
    if len(value) > MAX_ATTRIBUTE_KEY_LENGTH:
        raise ValueError("static attribute names must not exceed 200 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError("static attribute names must not contain control characters")
    _reject_secret_like(value)
    return value


def _validate_value(value: object) -> str:
    """Return a bounded, control-free attribute value or raise."""
    if not isinstance(value, str):
        raise ValueError("static attributes must map to strings")
    stripped = value.strip()
    if not stripped:
        raise ValueError("static attribute values must not be blank")
    if len(stripped) > MAX_ATTRIBUTE_VALUE_LENGTH:
        raise ValueError(
            f"static attribute values must not exceed "
            f"{MAX_ATTRIBUTE_VALUE_LENGTH} characters"
        )
    if any(ord(char) < 32 for char in stripped):
        raise ValueError("static attribute values must not contain control characters")
    _reject_secret_like(stripped)
    return stripped


def validate_bounded_attributes(
    attributes: Mapping[str, str] | None,
) -> dict[str, str]:
    """Validate bounded safe static attributes and return a plain dict.

    ``None``/empty mappings produce an empty dict. Every key and value is
    trimmed, bounded, control-free, and free of secret-like markers.
    """
    if not attributes:
        return {}
    validated: dict[str, str] = {}
    for key, value in attributes.items():
        validated[_validate_key(key)] = _validate_value(value)
    return validated


__all__ = [
    "ERROR_OUTCOME",
    "MAX_ATTRIBUTE_KEY_LENGTH",
    "MAX_ATTRIBUTE_VALUE_LENGTH",
    "OUTCOME",
    "SECRET_MARKERS",
    "SUCCESS_OUTCOME",
    "validate_bounded_attributes",
]
