# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the bounded fixed-origin registry HTTP transport (PR 18)."""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from darkula.app.registry_polling import RegistrySchemaError
from darkula.infrastructure.discovery import http as http_module
from darkula.infrastructure.discovery.http import (
    RANSOMLOOK_ORIGIN,
    StdlibRegistryHttpTransport,
    _blocking_get,
    _read_bounded,
    validate_relative_path,
)


class TestPathValidation:
    """U33: path injection is rejected before the transport."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/groups",
            "/api/group/blackgate",
            "/api/group/name%20with%20space",
        ],
    )
    def test_accepts_safe_relative_paths(self, path: str) -> None:
        assert validate_relative_path(path) == path

    @pytest.mark.parametrize(
        "path",
        [
            "https://evil.example/api/groups",
            "//evil.example/api/groups",
            "/api/group/a/b",
            "/api/groups?x=1",
            "/api/groups#frag",
            "/api/group/back\\slash",
            "/api/group/../secret",
            "/api/group/na me",
            "/api/group/" + "a" * 3000,
        ],
    )
    def test_rejects_unsafe_paths(self, path: str) -> None:
        with pytest.raises(ValueError):
            validate_relative_path(path)


class TestFixedOrigin:
    """D01/D10: the origin is fixed and not configurable."""

    def test_rejects_alternate_origin(self) -> None:
        with pytest.raises(ValueError):
            StdlibRegistryHttpTransport(origin="https://evil.example")

    def test_builds_url_from_fixed_origin(self) -> None:
        transport = StdlibRegistryHttpTransport()
        assert transport.build_url("/api/groups") == RANSOMLOOK_ORIGIN + "/api/groups"

    @pytest.mark.asyncio
    async def test_get_delegates_to_bounded_blocking_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_blocking(
            url: str, *, timeout_seconds: float, max_response_bytes: int
        ) -> http_module.RegistryHttpResponse:
            seen.update(
                url=url,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
            )
            return http_module.RegistryHttpResponse(status_code=200, body=b"[]")

        monkeypatch.setattr(http_module, "_blocking_get", fake_blocking)
        transport = StdlibRegistryHttpTransport()
        response = await transport.get(
            "/api/groups", timeout_seconds=2.0, max_response_bytes=64
        )
        assert response.status_code == 200
        assert seen == {
            "url": RANSOMLOOK_ORIGIN + "/api/groups",
            "timeout_seconds": 2.0,
            "max_response_bytes": 64,
        }


class TestBoundedRead:
    """U32: response bytes are bounded."""

    def test_accepts_bounded_body(self) -> None:
        assert _read_bounded(io.BytesIO(b"abc"), 3) == b"abc"

    def test_rejects_oversized_body(self) -> None:
        with pytest.raises(RegistrySchemaError):
            _read_bounded(io.BytesIO(b"abcd"), 3)

    def test_redirect_handler_denies(self) -> None:
        handler: Any = http_module._DenyRedirectHandler()
        assert (
            handler.redirect_request(
                None, None, 302, "Found", None, "https://evil.example"
            )
            is None
        )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/ok":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://evil.example/")
            self.end_headers()
        elif self.path == "/huge":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 1000)
        elif self.path == "/error":
            self.send_response(503)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return None


@pytest.fixture()
def local_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = str(server.server_address[0]), server.server_address[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestBlockingGet:
    """D34/D35/U31/U32: statuses and redirects are never followed."""

    def test_reads_bounded_success(self, local_server: str) -> None:
        response = _blocking_get(
            local_server + "/ok", timeout_seconds=2.0, max_response_bytes=64
        )
        assert response.status_code == 200
        assert response.body == b'{"ok":true}'

    def test_returns_redirect_status_without_following(self, local_server: str) -> None:
        response = _blocking_get(
            local_server + "/redirect", timeout_seconds=2.0, max_response_bytes=64
        )
        assert response.status_code == 302

    def test_returns_error_status(self, local_server: str) -> None:
        response = _blocking_get(
            local_server + "/error", timeout_seconds=2.0, max_response_bytes=64
        )
        assert response.status_code == 503

    def test_rejects_oversized_response(self, local_server: str) -> None:
        with pytest.raises(RegistrySchemaError):
            _blocking_get(
                local_server + "/huge", timeout_seconds=2.0, max_response_bytes=64
            )
