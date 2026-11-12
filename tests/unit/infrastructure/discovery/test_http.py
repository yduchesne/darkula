# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the bounded fixed-origin HTTPX registry transport (PR 18-F)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from darkula.app.registry_polling import RegistrySchemaError, RegistryTransportError
from darkula.infrastructure.discovery import http as http_module
from darkula.infrastructure.discovery.http import (
    RANSOMLOOK_ORIGIN,
    StdlibRegistryHttpTransport,
    _async_get_url,
    validate_relative_path,
)


class TestPathValidation:
    """T02: path injection is rejected before the transport."""

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
            "/unapproved",
        ],
    )
    def test_rejects_unsafe_paths(self, path: str) -> None:
        with pytest.raises(ValueError):
            validate_relative_path(path)


class TestFixedOrigin:
    """T01: the origin is fixed and not configurable."""

    def test_rejects_alternate_origin(self) -> None:
        with pytest.raises(ValueError):
            StdlibRegistryHttpTransport(origin="https://evil.example")

    def test_builds_url_from_fixed_origin(self) -> None:
        transport = StdlibRegistryHttpTransport()
        assert transport.build_url("/api/groups") == RANSOMLOOK_ORIGIN + "/api/groups"

    @pytest.mark.asyncio
    async def test_get_delegates_to_http_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        async def fake_async_get(
            url: str,
            *,
            timeout_seconds: float,
            max_response_bytes: int,
            transport: httpx.AsyncBaseTransport | None = None,
        ) -> http_module.RegistryHttpResponse:
            seen.update(
                url=url,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
                transport=transport,
            )
            return http_module.RegistryHttpResponse(status_code=200, body=b"[]")

        monkeypatch.setattr(http_module, "_async_get_url", fake_async_get)
        transport = StdlibRegistryHttpTransport()
        response = await transport.get(
            "/api/groups", timeout_seconds=2.0, max_response_bytes=64
        )
        assert response.status_code == 200
        assert seen == {
            "url": RANSOMLOOK_ORIGIN + "/api/groups",
            "timeout_seconds": 2.0,
            "max_response_bytes": 64,
            "transport": None,
        }


class _AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._data


def _mock_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class TestRequestShape:
    """T03: exact GET shape, no credentials/proxy/redirect/body."""

    @pytest.mark.asyncio
    async def test_requests_only_approved_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, stream=_AsyncBytes(b'["accessbay"]'))

        response = await _async_get_url(
            RANSOMLOOK_ORIGIN + "/api/groups",
            timeout_seconds=2.0,
            max_response_bytes=1024,
            transport=_mock_transport(handler),
        )
        assert response.status_code == 200
        assert len(seen) == 1
        request = seen[0]
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "www.ransomlook.io"
        assert request.url.port in (None, 443)
        assert request.url.path == "/api/groups"
        assert request.content == b""
        assert request.headers.get("Accept-Encoding") == "identity"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "proxy-authorization" not in request.headers


class TestBoundedBody:
    """T04-T07: raw streaming bounds, encodings, and non-200 bodies."""

    @pytest.mark.asyncio
    async def test_accepts_exact_cap(self) -> None:
        payload = b"x" * 16

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, stream=_AsyncBytes(payload))

        response = await _async_get_url(
            RANSOMLOOK_ORIGIN + "/api/groups",
            timeout_seconds=2.0,
            max_response_bytes=16,
            transport=_mock_transport(handler),
        )
        assert response.body == payload
        assert response.content_encoding is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [
            {"Content-Length": "999"},
            {"Content-Length": "abc"},
            {"Content-Length": "-1"},
        ],
    )
    async def test_rejects_invalid_or_oversize_declared_length(
        self, headers: dict[str, str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, headers=headers, stream=_AsyncBytes(b"x" * 4))

        with pytest.raises(RegistrySchemaError):
            await _async_get_url(
                RANSOMLOOK_ORIGIN + "/api/groups",
                timeout_seconds=2.0,
                max_response_bytes=16,
                transport=_mock_transport(handler),
            )

    @pytest.mark.asyncio
    async def test_rejects_actual_body_over_cap(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, stream=_AsyncBytes(b"x" * 17))

        with pytest.raises(RegistrySchemaError):
            await _async_get_url(
                RANSOMLOOK_ORIGIN + "/api/groups",
                timeout_seconds=2.0,
                max_response_bytes=16,
                transport=_mock_transport(handler),
            )

    @pytest.mark.asyncio
    async def test_rejects_compressed_encoding(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=_AsyncBytes(b"compressed"),
            )

        with pytest.raises(RegistrySchemaError):
            await _async_get_url(
                RANSOMLOOK_ORIGIN + "/api/groups",
                timeout_seconds=2.0,
                max_response_bytes=1024,
                transport=_mock_transport(handler),
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [429, 302, 503])
    async def test_non_200_body_is_never_consumed(self, status: int) -> None:
        class _ExplodingStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                raise AssertionError("non-200 body must not be read")
                yield b""  # pragma: no cover - unreachable

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                status,
                headers={"Retry-After": "30", "Location": "https://evil.example"},
                stream=_ExplodingStream(),
            )

        response = await _async_get_url(
            RANSOMLOOK_ORIGIN + "/api/groups",
            timeout_seconds=2.0,
            max_response_bytes=1024,
            transport=_mock_transport(handler),
        )
        assert response.status_code == status
        assert response.body == b""
        assert response.retry_after == "30"


class TestTransportErrors:
    """T08: all network failures become bounded transport failures."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("connect failed"),
            httpx.ReadError("read failed"),
            httpx.ReadTimeout("read timed out"),
            httpx.RemoteProtocolError("protocol failed"),
        ],
    )
    async def test_network_error_maps_to_transport_error(
        self, error: httpx.TransportError
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            raise error

        with pytest.raises(RegistryTransportError) as captured:
            await _async_get_url(
                RANSOMLOOK_ORIGIN + "/api/groups",
                timeout_seconds=2.0,
                max_response_bytes=1024,
                transport=_mock_transport(handler),
            )
        assert str(captured.value) == "registry request failed"


class _TrickleServer:
    """Async loopback server streaming tiny chunks without completing."""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._handler_task: asyncio.Task[None] | None = None
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self.port = 0
        self._stop = asyncio.Event()

    async def __aenter__(self) -> _TrickleServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sock = self._server.sockets[0]
        self.port = int(sock.getsockname()[1])
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._stop.set()
        if self._handler_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                self._handler_task.cancel()
                await self._handler_task
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._handler_task = asyncio.current_task()
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.connected.set()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Content-Type: application/json\r\n\r\n"
            )
            await writer.drain()
            while not self._stop.is_set():
                writer.write(b"1\r\nx\r\n")
                await writer.drain()
                await asyncio.sleep(0.02)
        except ConnectionError, asyncio.IncompleteReadError:
            pass
        finally:
            self.closed.set()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/groups"


class TestDeadlineAndCancellation:
    """T09/T10: an elapsed deadline and caller cancellation close the socket."""

    @pytest.mark.asyncio
    async def test_elapsed_deadline_fires_during_active_trickle(self) -> None:
        async with _TrickleServer() as server:
            started = asyncio.get_running_loop().time()
            with pytest.raises(RegistryTransportError):
                await _async_get_url(
                    server.url,
                    timeout_seconds=0.2,
                    max_response_bytes=10_000,
                )
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < 1.5
            assert server.connected.is_set()
            await asyncio.wait_for(server.closed.wait(), timeout=1.5)

    @pytest.mark.asyncio
    async def test_caller_cancellation_propagates_and_closes(self) -> None:
        async with _TrickleServer() as server:
            task = asyncio.create_task(
                _async_get_url(
                    server.url,
                    timeout_seconds=5.0,
                    max_response_bytes=10_000,
                )
            )
            await asyncio.wait_for(server.connected.wait(), timeout=1.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(server.closed.wait(), timeout=1.5)
