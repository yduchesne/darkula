# SPDX-License-Identifier: AGPL-3.0-only
"""PR 20 CONNECT gateway socket/tunnel tests (U20-24..U20-31).

All listeners/connectors are local loopback fakes; no DNS lookup or public
network request is performed.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable

import pytest

from https_egress import gateway as gateway_module
from https_egress.gateway import HttpsEgressGateway
from https_egress.policy import (
    MAX_TUNNELS,
    EgressDenied,
    GatewayConfig,
)

HOST = "approved.example.org"
CONNECT = f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n\r\n".encode()

Resolver = Callable[[str], Awaitable[str]]
Connector = Callable[
    [str, int, float], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def start_gateway(
    *, resolver: Resolver, connector: Connector
) -> tuple[HttpsEgressGateway, int]:
    port = free_port()
    config = GatewayConfig(
        host=HOST,
        bind_ipv4="127.0.0.1",
        listen_port=port,
        execution_timeout_seconds=30.0,
    )
    gateway = HttpsEgressGateway(config, resolver=resolver, connector=connector)
    await gateway.start()
    return gateway, port


async def open_connect(
    port: int, raw: bytes = CONNECT
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    return reader, writer


async def read_status(reader: asyncio.StreamReader) -> bytes:
    line = await asyncio.wait_for(reader.readline(), 2.0)
    while True:
        header = await asyncio.wait_for(reader.readline(), 2.0)
        if header in (b"\r\n", b""):
            return line


async def echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        data = await reader.read(1024)
        writer.write(b"ECHO:" + data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    return server, port


async def silent_server() -> tuple[asyncio.AbstractServer, int, asyncio.Event]:
    stop = asyncio.Event()

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await stop.wait()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    return server, port, stop


class TestTunnel:
    @pytest.mark.asyncio
    async def test_u20_28_opaque_bytes_and_exact_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway_module, "IDLE_TIMEOUT_SECONDS", 0.5)
        outbound: list[tuple[str, int]] = []
        resolved: list[str] = []
        server, echo_port = await echo_server()

        async def resolver(host: str) -> str:
            resolved.append(host)
            return "8.8.8.8"

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            outbound.append((address, port))
            return await asyncio.open_connection("127.0.0.1", echo_port)

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        try:
            reader, writer = await open_connect(port)
            assert (
                await read_status(reader) == b"HTTP/1.1 200 Connection Established\r\n"
            )
            writer.write(b"\x16opaque-tls-bytes")
            await writer.drain()
            assert await reader.read() == b"ECHO:\x16opaque-tls-bytes"
            writer.close()
            await writer.wait_closed()
            assert resolved == [HOST]
            # The connector receives the vetted numeric IP, never a hostname.
            assert outbound == [("8.8.8.8", 443)]
        finally:
            await gateway.close()
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_u20_21_denied_target_makes_zero_outbound_calls(self) -> None:
        resolved: list[str] = []
        outbound: list[tuple[str, int]] = []

        async def resolver(host: str) -> str:
            resolved.append(host)
            return "8.8.8.8"

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            outbound.append((address, port))
            raise AssertionError("must not dial")

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        try:
            reader, writer = await open_connect(
                port,
                b"CONNECT evil.example.org:443 HTTP/1.1\r\n\r\n",
            )
            assert await read_status(reader) == b"HTTP/1.1 403 Forbidden\r\n"
            writer.close()
            await writer.wait_closed()
            assert resolved == []
            assert outbound == []
        finally:
            await gateway.close()

    @pytest.mark.asyncio
    async def test_u20_25_dns_rebind_second_connect_independently_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway_module, "IDLE_TIMEOUT_SECONDS", 0.5)
        calls = {"resolver": 0, "connector": 0}

        async def resolver(host: str) -> str:
            calls["resolver"] += 1
            if calls["resolver"] == 1:
                return "8.8.8.8"
            raise EgressDenied("denied_ip")

        server, echo_port = await echo_server()

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            calls["connector"] += 1
            return await asyncio.open_connection("127.0.0.1", echo_port)

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        try:
            first_reader, first_writer = await open_connect(port)
            assert await read_status(first_reader) == (
                b"HTTP/1.1 200 Connection Established\r\n"
            )
            first_writer.close()
            await first_writer.wait_closed()

            second_reader, second_writer = await open_connect(port)
            assert await read_status(second_reader) == b"HTTP/1.1 403 Forbidden\r\n"
            second_writer.close()
            await second_writer.wait_closed()
            assert calls == {"resolver": 2, "connector": 1}
        finally:
            await gateway.close()
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_u20_27_connect_error_never_retries(self) -> None:
        outbound: list[tuple[str, int]] = []

        async def resolver(host: str) -> str:
            return "8.8.8.8"

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            outbound.append((address, port))
            raise TimeoutError

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        try:
            reader, writer = await open_connect(port)
            assert await read_status(reader) == b"HTTP/1.1 502 Bad Gateway\r\n"
            writer.close()
            await writer.wait_closed()
            assert outbound == [("8.8.8.8", 443)]
        finally:
            await gateway.close()

    @pytest.mark.asyncio
    async def test_u20_26_dns_failure_returns_502(self) -> None:
        async def resolver(host: str) -> str:
            raise EgressDenied("dns_failed")

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            raise AssertionError("must not dial")

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        try:
            reader, writer = await open_connect(port)
            assert await read_status(reader) == b"HTTP/1.1 502 Bad Gateway\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await gateway.close()


class TestBudgetsAndLimits:
    @pytest.mark.asyncio
    async def test_u20_29_tunnel_cap_returns_429(self) -> None:
        async def resolver(host: str) -> str:
            return "8.8.8.8"

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            raise AssertionError("must not dial")

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        try:
            gateway._tunnels = MAX_TUNNELS
            reader, writer = await open_connect(port)
            assert await read_status(reader) == b"HTTP/1.1 429 Too Many Requests\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await gateway.close()

    @pytest.mark.asyncio
    async def test_u20_29_oversized_header_rejected(self) -> None:
        async def resolver(host: str) -> str:
            return "8.8.8.8"

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            raise AssertionError("must not dial")

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT " + b"a" * 5000)
            await writer.drain()
            assert await read_status(reader) == b"HTTP/1.1 400 Bad Request\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await gateway.close()

    @pytest.mark.asyncio
    async def test_u20_29_per_direction_byte_cap_closes_tunnel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway_module, "MAX_BYTES_PER_DIRECTION", 4)
        server, echo_port = await echo_server()

        async def resolver(host: str) -> str:
            return "8.8.8.8"

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            return await asyncio.open_connection("127.0.0.1", echo_port)

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await open_connect(port)
            assert await read_status(reader) == (
                b"HTTP/1.1 200 Connection Established\r\n"
            )
            writer.write(b"0123456789")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 2.0) == b""
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await gateway.close()
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_u20_29_idle_timeout_closes_tunnel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway_module, "IDLE_TIMEOUT_SECONDS", 0.1)
        server, silent_port, stop = await silent_server()

        async def resolver(host: str) -> str:
            return "8.8.8.8"

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            return await asyncio.open_connection("127.0.0.1", silent_port)

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        try:
            reader, writer = await open_connect(port)
            assert await read_status(reader) == (
                b"HTTP/1.1 200 Connection Established\r\n"
            )
            assert await asyncio.wait_for(reader.read(), 2.0) == b""
            writer.close()
            await writer.wait_closed()
        finally:
            await gateway.close()
            stop.set()
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_u20_30_cancellation_closes_active_tunnel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway_module, "IDLE_TIMEOUT_SECONDS", 0.5)
        server, silent_port, stop = await silent_server()

        async def resolver(host: str) -> str:
            return "8.8.8.8"

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            return await asyncio.open_connection("127.0.0.1", silent_port)

        gateway, port = await start_gateway(resolver=resolver, connector=connector)
        try:
            reader, writer = await open_connect(port)
            assert await read_status(reader) == (
                b"HTTP/1.1 200 Connection Established\r\n"
            )
            assert gateway.active_tunnels == 1
            tasks = list(gateway._tasks)
            assert tasks
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert gateway.active_tunnels == 0
            writer.close()
            await writer.wait_closed()
        finally:
            await gateway.close()
            stop.set()
            server.close()
            await server.wait_closed()


class TestBind:
    @pytest.mark.asyncio
    async def test_u20_31_binds_only_explicit_internal_address(self) -> None:
        async def resolver(host: str) -> str:
            return "8.8.8.8"

        async def connector(
            address: str, port: int, timeout_seconds: float
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            raise AssertionError("must not dial")

        gateway, _port = await start_gateway(resolver=resolver, connector=connector)
        try:
            sockets = gateway.sockets
            assert sockets
            assert {sock.getsockname()[0] for sock in sockets} == {"127.0.0.1"}
        finally:
            await gateway.close()
