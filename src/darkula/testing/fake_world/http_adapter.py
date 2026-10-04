# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Thin real-HTTP Fake World adapter (PR 7).

Sits between HTTP clients (the crawler browser) and the existing
deterministic :class:`FakeWorldRenderer`. It performs **no scenario
semantics of its own**: it translates real HTTP
method/path/query/headers/body into :class:`FakeWorldRequest`, maps
session cookies to :class:`FakeWorldSession`, invokes the renderer, and
translates the rendered response back to HTTP (status, headers,
attachments, redirects, 429, 503).

Rules frozen here (PR 7 invariants):

- BlackGate behavior lives only in the existing renderer; this adapter never
  re-implements source routes;
- session state is scoped to one server instance (`token -> session`);
  a new server/renderer pair starts deterministic canonical state (H12);
- hidden truth never crosses HTTP: this module imports no truth ontology
  and never renders truth facts;
- the service is testable without sockets through ``handle(...)``.

The stdlib ``http.server`` based server is the smallest Python-3.14-
compatible hosting seam; no production web-framework dependency is added.
"""

from __future__ import annotations

import threading
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from darkula.testing.fake_world.identifiers import SourceId
from darkula.testing.fake_world.registry import get_scenario
from darkula.testing.fake_world.rendering import (
    FakeWorldRenderer,
    FakeWorldRequest,
    FakeWorldSession,
    FrozenParams,
    HttpMethod,
    RenderedSourceResponse,
)

#: Canonical BlackGate scenario identity used by the adapter.
SCENARIO_ID = "blackgate-core"
SCENARIO_VERSION = 1
#: Session cookie the renderer emits (mirrors the renderer's contract).
SESSION_COOKIE = "blackgate_session"

#: Synthetic canonical BlackGate source identity.
_SOURCE_ID = SourceId("blackgate")


def _header_pairs(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """Return normalized lowercase (name, value) pairs from a mapping."""
    return [(name.lower().strip(), value) for name, value in headers.items()]


@dataclass(frozen=True, slots=True)
class HttpWireResponse:
    """One transport-level HTTP response produced by the adapter."""

    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class FakeWorldHttpService:
    """Deterministic Fake World HTTP translation service (server-scoped).

    One service owns one renderer + canonical scenario and its session
    state; a fresh service starts canonical state (H12). Renders are
    serialized so counter increments stay deterministic under the
    threaded HTTP server.
    """

    def __init__(self, renderer: FakeWorldRenderer | None = None) -> None:
        self._renderer = renderer or FakeWorldRenderer()
        self._scenario = get_scenario(SCENARIO_ID, version=SCENARIO_VERSION)
        self._sessions: dict[str, FakeWorldSession] = {}
        self._lock = threading.Lock()

    # -- translatable pure interface (unit tests without sockets) ---------
    def handle(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpWireResponse:
        """Translate one real HTTP request into one rendered HTTP response."""
        query = dict(query or {})
        header_map = dict(headers or {})
        try:
            http_method = HttpMethod(method.upper())
        except ValueError:
            http_method = HttpMethod.GET
        wire_headers = _header_pairs(header_map)
        header_cookie = _cookie_from_headers(header_map)
        session = self._session_for_cookie(header_cookie) if header_cookie else None
        request = FakeWorldRequest(
            source_id=_SOURCE_ID,
            method=http_method,
            path=path,
            query=FrozenParams.from_mapping(query),
            headers=FrozenParams(wire_headers),
            body=body,
        )
        with self._lock:
            result = self._renderer.render(self._scenario, request, session)
        self._apply_session_update(header_cookie, result.session_update)
        return _translate_response(result.response)

    # -- cookie/session mapping -------------------------------------------
    def _session_for_cookie(self, cookie_header: str) -> FakeWorldSession | None:
        token = _cookie_value(cookie_header)
        if token is None:
            return None
        return self._sessions.get(token)

    def _apply_session_update(self, cookie_header: str | None, update: object) -> None:
        from darkula.testing.fake_world.rendering import SessionUpdate

        if not isinstance(update, SessionUpdate):
            return
        old_token = _cookie_value(cookie_header) if cookie_header else None
        if update.session is None:
            if old_token is not None:
                self._sessions.pop(old_token, None)
            return
        self._sessions[update.session.token] = update.session

    def reset(self) -> None:
        """Restore canonical runtime state (fresh renderer + no sessions)."""
        self._renderer.reset()
        self._sessions.clear()


def _cookie_value(cookie_header: str) -> str | None:
    """Extract the BlackGate session token from a Cookie header (bounded)."""
    for part in cookie_header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip().lower() == SESSION_COOKIE:
            token = value.strip()
            if len(token) > 512:
                return None
            return token
    return None


def _cookie_from_headers(headers: Mapping[str, str]) -> str | None:
    """Return the Cookie header value case-insensitively (or ``None``)."""
    for name, value in headers.items():
        if name.lower() == "cookie":
            return value
    return None


def _translate_response(
    response: RenderedSourceResponse,
) -> HttpWireResponse:
    """Translate one rendered response into wire HTTP response data."""
    headers = tuple((name, value) for name, value in response.headers.items())
    # Ensure content-length is present even when the renderer omitted it.
    lowered = {name.lower() for name, _ in headers}
    if "content-length" not in lowered:
        headers = (*headers, ("content-length", str(len(response.body))))
    return HttpWireResponse(
        status_code=response.status_code,
        headers=headers,
        body=response.body,
    )


class _FakeWorldHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler delegating every request to the service."""

    protocol_version = "HTTP/1.1"

    def _dispatch(self) -> None:
        service = self.server.service  # type: ignore[attr-defined]
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query_map: dict[str, str] = {}
        for key, value in query:
            query_map.setdefault(key, value)
        headers = dict(self.headers.items())
        # Non-GET bodies are read deterministically (bounded below).
        content_length = 0
        raw = self.headers.get("Content-Length")
        if raw and raw.isascii() and raw.isdigit():
            content_length = min(int(raw), 8192)
        body = self.rfile.read(content_length) if content_length else None
        wire = service.handle(
            self.command,
            parsed.path,
            query=query_map,
            headers=headers,
            body=body,
        )
        self.send_response(wire.status_code)
        for name, value in wire.headers:
            if name.lower() == "connection":
                continue
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(wire.body)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def log_message(self, format: str, *args: object) -> None:
        # Quiet: hostile/untrusted request lines must never enter logs.
        return


class FakeWorldHttpServer(ThreadingHTTPServer):
    """Threaded stdlib HTTP server hosting one Fake World service."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        host: str,
        port: int,
        service: FakeWorldHttpService,
    ) -> None:
        super().__init__((host, port), _FakeWorldHandler)
        self.service = service


__all__ = [
    "SCENARIO_ID",
    "SCENARIO_VERSION",
    "SESSION_COOKIE",
    "FakeWorldHttpServer",
    "FakeWorldHttpService",
    "HttpWireResponse",
]
