# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic Fake World *source registry* transport (PR 18).

This is not a second discovery system: it is the external HTTP boundary for
the real RansomLook discovery adapter
(:class:`~darkula.infrastructure.discovery.ransomlook.RansomLookDiscoveryAdapter`).
It returns bytes/status/headers matching the two documented RansomLook public
REST shapes and records a bounded call log. The real parser, poller,
discovery service, and PostgreSQL persistence are exercised unchanged.

Fixtures are fully synthetic and safe for publication. Hidden Fake World
truth is never imported here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote


@dataclass(frozen=True, slots=True)
class FakeRegistryHttpResponse:
    """Transport-shaped response (no production import from Fake World)."""

    status_code: int
    body: bytes
    content_encoding: str | None = None
    retry_after: str | None = None


#: Canonical synthetic index, bytewise sorted.
FAKE_REGISTRY_INDEX: tuple[str, ...] = (
    "accessbay",
    "blackgate",
    "nightleak",
    "shadowtalk",
)

#: In-memory-only classification for the deterministic call log.
REQUEST_CLASS_INDEX = "INDEX"
REQUEST_CLASS_DETAIL = "DETAIL"


@dataclass(frozen=True, slots=True)
class FakeRegistryCall:
    """One recorded transport call (method + path classification).

    ``path`` is retained only so tests can assert exact encoded paths; it
    must never be forwarded into production telemetry.
    """

    method: str
    request_class: str
    path: str


@dataclass(frozen=True, slots=True)
class ScriptedRegistryResponse:
    """One scripted transport response used for failure-path tests."""

    status_code: int
    body: bytes = b""
    retry_after: str | None = None
    content_encoding: str | None = None


def _json_bytes(document: Any) -> bytes:
    return json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def default_detail(name: str) -> list[Any]:
    """Return the canonical synthetic detail document for ``name``."""
    if name == "blackgate":
        return [
            {
                "name": "blackgate",
                "locations": [
                    {
                        "slug": "http://blackgate-board.onion/forum",
                        "updated": "2026-01-02T03:04:05Z",
                    },
                    {"slug": "blackgate-mirror.onion"},
                ],
            },
            [],
        ]
    if name == "nightleak":
        return [
            {
                "name": "nightleak",
                "locations": [
                    {"slug": "https://nightleak-one.example.org/leaks"},
                    {"slug": "https://nightleak-two.example.org/leaks"},
                ],
            },
            [],
        ]
    if name == "shadowtalk":
        return [
            {
                "name": "shadowtalk",
                "locations": [
                    {
                        "slug": "https://shadowtalk-secret.example.org/",
                        "private": True,
                    },
                    {"slug": "shadowtalk-echo.onion"},
                ],
            },
            [],
        ]
    if name == "accessbay":
        return [
            {
                "name": "accessbay",
                "locations": [
                    {
                        "slug": "https://accessbay.example.org/market",
                        "available": False,
                    }
                ],
            },
            [],
        ]
    raise KeyError(name)


@dataclass
class FakeSourceRegistry:
    """Deterministic in-process registry transport.

    ``scripted`` responses are consumed in order for every call, which lets
    tests drive 429/503/404/malformed/oversized/partial failure paths without
    any real network. When exhausted, the deterministic canonical index and
    detail documents are served.
    """

    index: tuple[str, ...] = FAKE_REGISTRY_INDEX
    details: Mapping[str, Any] = field(default_factory=dict)
    scripted: list[ScriptedRegistryResponse] = field(default_factory=list)
    calls: list[FakeRegistryCall] = field(default_factory=list)

    def detail_document(self, name: str) -> Any:
        override = self.details.get(name)
        if override is not None:
            return override
        return default_detail(name)

    def _index_document(self) -> Any:
        return list(self.index)

    async def get(
        self,
        path: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> FakeRegistryHttpResponse:
        del timeout_seconds
        if not path.startswith("/api/"):
            raise AssertionError(f"unexpected fake registry path shape: {path!r}")
        if self.scripted:
            scripted = self.scripted.pop(0)
            self.calls.append(
                FakeRegistryCall(
                    method="GET",
                    request_class="SCRIPTED",
                    path=path,
                )
            )
            return FakeRegistryHttpResponse(
                status_code=scripted.status_code,
                body=scripted.body,
                content_encoding=scripted.content_encoding,
                retry_after=scripted.retry_after,
            )
        if path == "/api/groups":
            self.calls.append(
                FakeRegistryCall(
                    method="GET", request_class=REQUEST_CLASS_INDEX, path=path
                )
            )
            body = _json_bytes(self._index_document())
        elif path.startswith("/api/group/"):
            name = unquote(path[len("/api/group/") :])
            self.calls.append(
                FakeRegistryCall(
                    method="GET", request_class=REQUEST_CLASS_DETAIL, path=path
                )
            )
            body = _json_bytes(self.detail_document(name))
        else:
            raise AssertionError(f"unexpected fake registry path shape: {path!r}")
        if len(body) > max_response_bytes:
            return FakeRegistryHttpResponse(
                status_code=200, body=body[:max_response_bytes]
            )
        return FakeRegistryHttpResponse(status_code=200, body=body)

    def script(self, *responses: ScriptedRegistryResponse) -> None:
        """Append scripted responses consumed before canonical fixtures."""
        self.scripted.extend(responses)

    def reset_calls(self) -> None:
        self.calls.clear()


def synthetic_sensitive_fields_detail() -> list[Any]:
    """Return a detail document carrying the forbidden upstream fields."""
    return [
        {
            "name": "blackgate",
            "meta": {"note": "synthetic metadata must never persist"},
            "header": "synthetic-scraper-header",
            "init_script": "synthetic-init-script",
            "browser": "synthetic-browser",
            "locations": [
                {
                    "slug": "https://blackgate-sensitive.example.org/",
                    "screen": "c2NyZWVuc2hvdC1ieXRlcw==",
                    "source": "PGh0bWw+bm90LXBlcnNpc3RlZDwvaHRtbD4=",
                    "header": "synthetic-scraper-header",
                    "init_script": "synthetic-init-script",
                    "browser": "synthetic-browser",
                    "title": "synthetic title",
                    "available": True,
                }
            ],
        },
        [
            {
                "post_title": "synthetic post must never be read",
                "description": "synthetic victim post body",
            }
        ],
    ]


__all__ = [
    "FAKE_REGISTRY_INDEX",
    "REQUEST_CLASS_DETAIL",
    "REQUEST_CLASS_INDEX",
    "FakeRegistryCall",
    "FakeRegistryHttpResponse",
    "FakeSourceRegistry",
    "ScriptedRegistryResponse",
    "default_detail",
    "synthetic_sensitive_fields_detail",
]
