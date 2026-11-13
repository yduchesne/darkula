# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Per-execution least-privilege HTTPS CONNECT gateway (PR 20).

Accepts exactly ``CONNECT <approved-fqdn>:443 HTTP/1.1`` on its private
internal listener, resolves the approved hostname afresh, vets every answer,
dials the selected numeric IPv4 address, and copies a bounded opaque TLS
stream. It never terminates TLS, never inspects or rewrites HTTP, never
accepts a client-chosen destination and never retries another address.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable

from https_egress.policy import (
    CHUNK_BYTES,
    CONNECT_TIMEOUT_SECONDS,
    HEADER_TIMEOUT_SECONDS,
    IDLE_TIMEOUT_SECONDS,
    MAX_BYTES_PER_DIRECTION,
    MAX_HEADER_BYTES,
    MAX_HEADERS,
    MAX_TOTAL_BYTES,
    MAX_TUNNELS,
    REASON_BUDGET_EXCEEDED,
    REASON_CONNECT_FAILED,
    REASON_DENIED_TARGET,
    EgressDenied,
    GatewayConfig,
    numeric_connect,
    resolve_public_ipv4,
)

_LOGGER = logging.getLogger(__name__)

_BAD_REQUEST = (
    b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)
_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_TOO_MANY = (
    b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)
_BAD_GATEWAY = (
    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)
_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"

#: Header names that must never cross the gateway on a CONNECT request.
_FORBIDDEN_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "content-length",
        "transfer-encoding",
        "forwarded",
    }
)

Resolver = Callable[[str], Awaitable[str]]
Connector = Callable[
    [str, int, float], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]


class _HttpError(Exception):
    """A bounded CONNECT parsing refusal with a fixed HTTP response."""

    def __init__(self, response: bytes, reason: str) -> None:
        super().__init__(reason)
        self.response = response
        self.reason = reason


def parse_connect_request(data: bytes, expected_host: str) -> None:
    """Validate one exact raw CONNECT request for the approved host.

    Raises :class:`_HttpError` with a fixed 400/403 response for anything
    that is not the single supported request shape. No input is logged.
    """
    if len(data) > MAX_HEADER_BYTES:
        raise _HttpError(_BAD_REQUEST, "header_too_large")
    if not data.endswith(b"\r\n\r\n"):
        raise _HttpError(_BAD_REQUEST, "incomplete_request")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise _HttpError(_BAD_REQUEST, "non_ascii_request") from None
    if "\x00" in text:
        raise _HttpError(_BAD_REQUEST, "nul_in_request")
    lines = text[: -len("\r\n\r\n")].split("\r\n")
    if not lines or any("\r" in line or "\n" in line for line in lines):
        raise _HttpError(_BAD_REQUEST, "header_smuggling")
    request_line = lines[0]
    parts = request_line.split(" ")
    if len(parts) != 3 or any(not part for part in parts):
        raise _HttpError(_BAD_REQUEST, "malformed_request_line")
    method, target, version = parts
    if method != "CONNECT":
        raise _HttpError(_FORBIDDEN, REASON_DENIED_TARGET)
    if version != "HTTP/1.1":
        raise _HttpError(_BAD_REQUEST, "unsupported_http_version")
    if "://" in target or "/" in target or "@" in target:
        raise _HttpError(_FORBIDDEN, REASON_DENIED_TARGET)
    if not target.endswith(":443"):
        raise _HttpError(_FORBIDDEN, REASON_DENIED_TARGET)
    target_host = target[: -len(":443")]
    if target_host != expected_host or target_host.endswith("."):
        raise _HttpError(_FORBIDDEN, REASON_DENIED_TARGET)
    if "%" in target_host or "[" in target_host or "]" in target_host:
        raise _HttpError(_FORBIDDEN, REASON_DENIED_TARGET)
    header_lines = lines[1:]
    if len(header_lines) > MAX_HEADERS:
        raise _HttpError(_BAD_REQUEST, "too_many_headers")
    seen_host = False
    for line in header_lines:
        if not line or line[0] in (" ", "\t"):
            raise _HttpError(_BAD_REQUEST, "obsolete_line_folding")
        if ":" not in line:
            raise _HttpError(_BAD_REQUEST, "malformed_header")
        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if not name or not value:
            raise _HttpError(_BAD_REQUEST, "malformed_header")
        if name in _FORBIDDEN_HEADERS or name.startswith("x-forwarded-"):
            raise _HttpError(_FORBIDDEN, REASON_DENIED_TARGET)
        if name == "host":
            if seen_host:
                raise _HttpError(_BAD_REQUEST, "duplicate_host")
            seen_host = True
            if value not in (f"{expected_host}:443", expected_host):
                raise _HttpError(_FORBIDDEN, REASON_DENIED_TARGET)
    if not seen_host:
        raise _HttpError(_BAD_REQUEST, "missing_host")


def _response_for(reason: str) -> bytes:
    """Map one bounded egress refusal reason to its fixed HTTP response."""
    if reason in ("denied_ip", "unsupported_address_family"):
        return _FORBIDDEN
    return _BAD_GATEWAY


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    """Close one writer without ever masking the primary outcome."""
    if writer is None:
        return
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    except Exception:
        pass


async def _write_response(writer: asyncio.StreamWriter, response: bytes) -> None:
    """Send one fixed response then close the client connection."""
    try:
        writer.write(response)
        await asyncio.wait_for(writer.drain(), timeout=2.0)
    except Exception:
        pass
    finally:
        await _close_writer(writer)


class _TunnelBudget:
    """Per-tunnel directional byte accounting."""

    __slots__ = ("down", "up")

    def __init__(self) -> None:
        self.up = 0
        self.down = 0

    def add(self, direction: str, amount: int) -> bool:
        """Account bytes; return ``False`` when a direction is over budget."""
        if direction == "up":
            self.up += amount
            return self.up <= MAX_BYTES_PER_DIRECTION
        self.down += amount
        return self.down <= MAX_BYTES_PER_DIRECTION


class HttpsEgressGateway:
    """One disposable per-execution CONNECT gateway instance."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        resolver: Resolver = resolve_public_ipv4,
        connector: Connector = numeric_connect,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._connector = connector
        self._server: asyncio.AbstractServer | None = None
        self._tunnels = 0
        self._total_bytes = 0
        self._closed = False
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def sockets(self) -> tuple[socket.socket, ...]:
        """Bound sockets (used by tests to prove the exact bind address)."""
        if self._server is None:
            return ()
        return tuple(getattr(self._server, "sockets", None) or ())

    @property
    def active_tunnels(self) -> int:
        """Number of currently active CONNECT tunnels."""
        return self._tunnels

    async def start(self) -> None:
        """Bind exactly the configured private internal IPv4 address."""
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._config.bind_ipv4,
            port=self._config.listen_port,
            limit=MAX_HEADER_BYTES,
        )

    async def serve(self) -> None:
        """Serve until the bounded execution deadline, then close."""
        if self._server is None:
            raise RuntimeError("gateway server was not started")
        try:
            await asyncio.wait_for(
                self._server.serve_forever(),
                timeout=self._config.execution_timeout_seconds,
            )
        except TimeoutError:
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop accepting and close every active tunnel promptly."""
        self._closed = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=5.0)
            except TimeoutError:
                pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._tasks.add(current)
        try:
            await self._handle_tunnel(reader, writer)
        finally:
            if current is not None:
                self._tasks.discard(current)

    async def _handle_tunnel(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._closed:
            await _close_writer(writer)
            return
        if self._tunnels >= MAX_TUNNELS:
            await _write_response(writer, _TOO_MANY)
            return
        try:
            data = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=HEADER_TIMEOUT_SECONDS
            )
        except TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError:
            await _write_response(writer, _BAD_REQUEST)
            return
        except asyncio.CancelledError:
            await _close_writer(writer)
            raise
        except OSError:
            await _close_writer(writer)
            return
        try:
            parse_connect_request(data, self._config.host)
        except _HttpError as exc:
            await _write_response(writer, exc.response)
            return

        self._tunnels += 1
        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        budget = _TunnelBudget()
        try:
            try:
                address = await self._resolver(self._config.host)
            except EgressDenied as exc:
                _LOGGER.info("egress refused: %s", exc.reason)
                await _write_response(writer, _response_for(exc.reason))
                return
            try:
                upstream_reader, upstream_writer = await self._connector(
                    address, self._config.connect_port, CONNECT_TIMEOUT_SECONDS
                )
            except TimeoutError, OSError:
                _LOGGER.info("egress refused: %s", REASON_CONNECT_FAILED)
                await _write_response(writer, _BAD_GATEWAY)
                return
            writer.write(_ESTABLISHED)
            await writer.drain()
            await self._pump_both(
                reader, writer, upstream_reader, upstream_writer, budget
            )
        except asyncio.CancelledError:
            raise
        except EgressDenied as exc:
            _LOGGER.info("egress tunnel closed: %s", exc.reason)
        except Exception:
            _LOGGER.info("egress tunnel closed: %s", REASON_CONNECT_FAILED)
        finally:
            self._tunnels -= 1
            await _close_writer(upstream_writer)
            await _close_writer(writer)

    async def _pump_both(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        budget: _TunnelBudget,
    ) -> None:
        up = asyncio.create_task(
            self._pump(client_reader, upstream_writer, budget, "up"),
            name="egress-pump-up",
        )
        down = asyncio.create_task(
            self._pump(upstream_reader, client_writer, budget, "down"),
            name="egress-pump-down",
        )
        tasks = {up, down}
        try:
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception

    async def _pump(
        self,
        source: asyncio.StreamReader,
        destination: asyncio.StreamWriter,
        budget: _TunnelBudget,
        direction: str,
    ) -> None:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    source.read(CHUNK_BYTES), timeout=IDLE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                raise EgressDenied(REASON_CONNECT_FAILED) from None
            except OSError:
                raise EgressDenied(REASON_CONNECT_FAILED) from None
            if not chunk:
                return
            self._total_bytes += len(chunk)
            if (
                not budget.add(direction, len(chunk))
                or self._total_bytes > MAX_TOTAL_BYTES
            ):
                raise EgressDenied(REASON_BUDGET_EXCEEDED)
            try:
                destination.write(chunk)
                await destination.drain()
            except OSError:
                raise EgressDenied(REASON_CONNECT_FAILED) from None


__all__ = ["HttpsEgressGateway", "parse_connect_request"]
