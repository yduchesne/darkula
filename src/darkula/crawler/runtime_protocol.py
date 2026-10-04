# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned, bounded CrawlerController <-> CrawlerRuntime protocol (PR 7).

Transport: one UTF-8 JSON document on the container's stdin/stdout. The
protocol is transport-agnostic and deterministic:

- ``PROTOCOL_VERSION`` selects the schema; unsupported versions are rejected;
- every field is validated with bounded types (no arbitrary Python objects,
  no binary payloads, no nested credentials outside the fixed shape);
- the trusted side enforces input/output size bounds;
- hostile page text is transported as **data only** and never interpreted.

The runtime image receives its own copy of this exact file (the Containerfile
copies ``src/darkula/crawler/runtime_protocol.py`` into ``/app/``), so both
sides of the boundary share one schema by construction. This module is pure
stdlib and must remain importable inside the minimal runtime image.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: Explicit schema version of the controller/runtime protocol (v1).
PROTOCOL_VERSION = 1

#: Bounded maximum control characters allowed in transport strings.
_MAX_FIELD_LENGTH = 4000


class RuntimeProtocolError(ValueError):
    """A malformed, unsupported, oversized, or unsafe protocol document.

    Never echoes payload content (the payload is hostile/untrusted by
    convention); only the bounded failure category.
    """


def _require_str(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeProtocolError(f"{name} must be a string")
    if len(value) > _MAX_FIELD_LENGTH:
        raise RuntimeProtocolError(f"{name} exceeds the field bound")
    if any(ord(ch) < 32 for ch in value):
        raise RuntimeProtocolError(f"{name} must not contain control characters")
    return value


def _require_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeProtocolError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise RuntimeProtocolError(f"{name} out of range")
    return value


def _require_float(value: Any, *, name: str, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RuntimeProtocolError(f"{name} must be a number")
    number = float(value)
    if number < minimum or number > maximum:
        raise RuntimeProtocolError(f"{name} out of range")
    return number


@dataclass(frozen=True, slots=True)
class RuntimeCredentials:
    """Bounded source authentication material carried across the boundary."""

    username: str
    password: str


@dataclass(frozen=True, slots=True)
class RuntimeInput:
    """Validated controller -> runtime execution input (schema v1)."""

    version: int
    execution_id: str
    request_id: str
    start_url: str
    origin_scheme: str
    origin_host: str
    origin_port: int
    credentials: RuntimeCredentials | None
    max_pages: int
    max_requests: int
    max_depth: int
    timeout_seconds: float
    max_page_text_bytes: int


@dataclass(frozen=True, slots=True)
class RuntimeOutputPage:
    """One bounded observed page in the runtime output."""

    url: str
    depth: int
    http_status: int | None
    rendered: bool
    title: str
    text_excerpt: str


@dataclass(frozen=True, slots=True)
class RuntimeOutput:
    """Validated runtime -> controller output (schema v1)."""

    version: int
    execution_id: str
    request_id: str
    status: str
    pages: tuple[RuntimeOutputPage, ...]
    links: tuple[str, ...]
    requests: int
    redirects_observed: int
    duration_seconds: float
    reason: str | None = None


def encode_input(payload: RuntimeInput) -> bytes:
    """Serialize one validated runtime input to bounded UTF-8 JSON."""
    document: dict[str, Any] = {
        "version": payload.version,
        "execution_id": payload.execution_id,
        "request_id": payload.request_id,
        "start_url": payload.start_url,
        "origin": {
            "scheme": payload.origin_scheme,
            "host": payload.origin_host,
            "port": payload.origin_port,
        },
        "credentials": None
        if payload.credentials is None
        else {
            "username": payload.credentials.username,
            "password": payload.credentials.password,
        },
        "max_pages": payload.max_pages,
        "max_requests": payload.max_requests,
        "max_depth": payload.max_depth,
        "timeout_seconds": payload.timeout_seconds,
        "max_page_text_bytes": payload.max_page_text_bytes,
    }
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _parse_origin(document: dict[str, Any]) -> tuple[str, str, int]:
    raw_origin = document.get("origin")
    if not isinstance(raw_origin, dict):
        raise RuntimeProtocolError("origin must be an object")
    scheme = _require_str(raw_origin.get("scheme"), name="origin.scheme").lower()
    host = _require_str(raw_origin.get("host"), name="origin.host").lower()
    if scheme not in ("http", "https"):
        raise RuntimeProtocolError("origin.scheme must be http or https")
    port = _require_int(
        raw_origin.get("port"), name="origin.port", minimum=1, maximum=65535
    )
    return scheme, host, port


def decode_input(raw: bytes, *, max_bytes: int) -> RuntimeInput:
    """Decode and validate one bounded runtime input document."""
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise RuntimeProtocolError("runtime input exceeds the size bound")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeProtocolError("runtime input is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise RuntimeProtocolError("runtime input must be a JSON object")
    version = _require_int(
        document.get("version"), name="version", minimum=1, maximum=256
    )
    if version != PROTOCOL_VERSION:
        raise RuntimeProtocolError(f"unsupported runtime protocol version: {version}")
    execution_id = _require_str(document.get("execution_id"), name="execution_id")
    request_id = _require_str(document.get("request_id"), name="request_id")
    start_url = _require_str(document.get("start_url"), name="start_url")
    max_pages = _require_int(
        document.get("max_pages"), name="max_pages", minimum=1, maximum=100000
    )
    max_requests = _require_int(
        document.get("max_requests"), name="max_requests", minimum=1, maximum=1000000
    )
    max_depth = _require_int(
        document.get("max_depth"), name="max_depth", minimum=1, maximum=256
    )
    timeout_seconds = _require_float(
        document.get("timeout_seconds"),
        name="timeout_seconds",
        minimum=0.001,
        maximum=86400.0,
    )
    max_page_text_bytes = _require_int(
        document.get("max_page_text_bytes"),
        name="max_page_text_bytes",
        minimum=1,
        maximum=1000000,
    )
    raw_credentials = document.get("credentials")
    credentials: RuntimeCredentials | None = None
    if raw_credentials is not None:
        if not isinstance(raw_credentials, dict):
            raise RuntimeProtocolError("credentials must be an object or null")
        credentials = RuntimeCredentials(
            username=_require_str(
                raw_credentials.get("username"), name="credentials.username"
            ),
            password=_require_str(
                raw_credentials.get("password"), name="credentials.password"
            ),
        )
    scheme, host, port = _parse_origin(document)
    return RuntimeInput(
        version=version,
        execution_id=execution_id,
        request_id=request_id,
        start_url=start_url,
        origin_scheme=scheme,
        origin_host=host,
        origin_port=port,
        credentials=credentials,
        max_pages=max_pages,
        max_requests=max_requests,
        max_depth=max_depth,
        timeout_seconds=timeout_seconds,
        max_page_text_bytes=max_page_text_bytes,
    )


def encode_output(payload: RuntimeOutput) -> bytes:
    """Serialize one validated runtime output to bounded UTF-8 JSON.

    Used by the CrawlerRuntime; also useful to trusted-side tests that
    script deterministic fake runtime outputs.
    """
    document: dict[str, Any] = {
        "version": payload.version,
        "execution_id": payload.execution_id,
        "request_id": payload.request_id,
        "status": payload.status,
        "pages": [
            {
                "url": page.url,
                "depth": page.depth,
                "http_status": page.http_status,
                "rendered": page.rendered,
                "title": page.title,
                "text_excerpt": page.text_excerpt,
            }
            for page in payload.pages
        ],
        "links": list(payload.links),
        "requests": payload.requests,
        "redirects_observed": payload.redirects_observed,
        "duration_seconds": payload.duration_seconds,
        "reason": payload.reason,
    }
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _decode_page(raw_page: Any) -> RuntimeOutputPage:
    if not isinstance(raw_page, dict):
        raise RuntimeProtocolError("pages entries must be objects")
    url = _require_str(raw_page.get("url"), name="page.url")
    depth = _require_int(
        raw_page.get("depth"), name="page.depth", minimum=0, maximum=256
    )
    http_status = raw_page.get("http_status")
    if http_status is not None:
        http_status = _require_int(
            http_status, name="page.http_status", minimum=100, maximum=599
        )
    rendered = raw_page.get("rendered")
    if not isinstance(rendered, bool):
        raise RuntimeProtocolError("page.rendered must be a boolean")
    title = _require_str(raw_page.get("title"), name="page.title")
    excerpt = _require_str(raw_page.get("text_excerpt"), name="page.text_excerpt")
    return RuntimeOutputPage(
        url=url,
        depth=depth,
        http_status=http_status,
        rendered=rendered,
        title=title,
        text_excerpt=excerpt,
    )


def decode_output(
    raw: bytes, *, max_bytes: int, max_pages: int, max_links: int
) -> RuntimeOutput:
    """Decode and validate one bounded runtime output document."""
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise RuntimeProtocolError("runtime output exceeds the size bound")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeProtocolError("runtime output is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise RuntimeProtocolError("runtime output must be a JSON object")
    version = _require_int(
        document.get("version"), name="version", minimum=1, maximum=256
    )
    if version != PROTOCOL_VERSION:
        raise RuntimeProtocolError(f"unsupported runtime protocol version: {version}")
    execution_id = _require_str(document.get("execution_id"), name="execution_id")
    request_id = _require_str(document.get("request_id"), name="request_id")
    status = _require_str(document.get("status"), name="status")
    if status not in {
        "completed",
        "workload_failed",
        "timed_out",
        "resource_limited",
        "policy_violation",
        "terminated",
    }:
        raise RuntimeProtocolError(f"unknown runtime status: {status!r}")
    raw_pages = document.get("pages")
    if not isinstance(raw_pages, list):
        raise RuntimeProtocolError("pages must be a list")
    if len(raw_pages) > max_pages:
        raise RuntimeProtocolError("pages count exceeds the bound")
    pages = tuple(_decode_page(item) for item in raw_pages)
    raw_links = document.get("links")
    if not isinstance(raw_links, list):
        raise RuntimeProtocolError("links must be a list")
    if len(raw_links) > max_links:
        raise RuntimeProtocolError("links count exceeds the bound")
    links = tuple(_require_str(item, name="link") for item in raw_links)
    requests = _require_int(
        document.get("requests"), name="requests", minimum=0, maximum=2000000
    )
    redirects_observed = _require_int(
        document.get("redirects_observed"),
        name="redirects_observed",
        minimum=0,
        maximum=100000,
    )
    duration_seconds = _require_float(
        document.get("duration_seconds"),
        name="duration_seconds",
        minimum=0.0,
        maximum=86400.0,
    )
    reason = document.get("reason")
    if reason is not None:
        reason = _require_str(reason, name="reason")
    return RuntimeOutput(
        version=version,
        execution_id=execution_id,
        request_id=request_id,
        status=status,
        pages=pages,
        links=links,
        requests=requests,
        redirects_observed=redirects_observed,
        duration_seconds=duration_seconds,
        reason=reason,
    )


__all__ = [
    "PROTOCOL_VERSION",
    "RuntimeCredentials",
    "RuntimeInput",
    "RuntimeOutput",
    "RuntimeOutputPage",
    "RuntimeProtocolError",
    "decode_input",
    "decode_output",
    "encode_input",
    "encode_output",
]
