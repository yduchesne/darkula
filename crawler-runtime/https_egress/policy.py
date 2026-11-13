# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure CONNECT-gateway policy, DNS vetting and numeric socket helpers.

Standard library only. The gateway never decodes TLS, never forwards a
client-chosen destination and never performs a hostname-based dial after
vetting: the exact approved FQDN is resolved afresh, **every** answer is
classified, and the socket connects to the selected numeric IPv4 address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from https_egress.public_policy import is_safe_public_host

#: The only CONNECT destination port supported by PR 20.
CONNECT_PORT = 443

#: The only listener port the gateway accepts from its environment.
LISTEN_PORT = 3128

#: Bound on a single DNS lookup and on the answers it may return.
DNS_TIMEOUT_SECONDS = 5.0
MAX_DNS_RESULTS = 16

#: Bound on a vetted numeric TCP connect attempt.
CONNECT_TIMEOUT_SECONDS = 5.0

#: Request-line/header parsing bounds.
MAX_HEADER_BYTES = 4096
MAX_HEADERS = 24
HEADER_TIMEOUT_SECONDS = 10.0

#: Per-execution gateway budgets.
MAX_TUNNELS = 32
MAX_BYTES_PER_DIRECTION = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
IDLE_TIMEOUT_SECONDS = 15.0
MAX_EXECUTION_SECONDS = 90.0
CHUNK_BYTES = 16 * 1024

#: Fixed, secret-free result categories (safe for telemetry).
REASON_ALLOWED = "allowed"
REASON_DENIED_TARGET = "denied_target"
REASON_DENIED_IP = "denied_ip"
REASON_DNS_FAILED = "dns_failed"
REASON_CONNECT_FAILED = "connect_failed"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
REASON_CANCELLED = "cancelled"
REASON_COMPLETED = "completed"

#: Environment variable names accepted by the gateway process.
ENV_HOST = "DARKULA_EGRESS_HOST"
ENV_BIND_IPV4 = "DARKULA_EGRESS_BIND_IPV4"
ENV_CONNECT_PORT = "DARKULA_EGRESS_CONNECT_PORT"
ENV_LISTEN_PORT = "DARKULA_EGRESS_LISTEN_PORT"
ENV_EXECUTION_TIMEOUT = "DARKULA_EGRESS_EXECUTION_TIMEOUT"

_REQUIRED_ENV = frozenset(
    {
        ENV_HOST,
        ENV_BIND_IPV4,
        ENV_CONNECT_PORT,
        ENV_LISTEN_PORT,
        ENV_EXECUTION_TIMEOUT,
    }
)


class EgressDenied(PermissionError):
    """A bounded gateway refusal with one fixed reason category."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Validated gateway configuration (secret-free topology only)."""

    host: str
    bind_ipv4: str
    connect_port: int = CONNECT_PORT
    listen_port: int = LISTEN_PORT
    execution_timeout_seconds: float = MAX_EXECUTION_SECONDS

    def __post_init__(self) -> None:
        if not is_safe_public_host(self.host):
            raise ValueError("invalid gateway host")
        try:
            address = ipaddress.IPv4Address(self.bind_ipv4)
        except ValueError:
            raise ValueError("invalid gateway bind address") from None
        if (
            address.is_unspecified
            or address.is_multicast
            or address.is_link_local
            or address.is_reserved
            or not (address.is_private or address.is_loopback)
        ):
            raise ValueError("gateway bind address must be a private internal IPv4")
        if self.connect_port != CONNECT_PORT:
            raise ValueError("gateway connect port must be 443")
        if self.listen_port < 1 or self.listen_port > 65535:
            raise ValueError("gateway listen port is out of range")
        if (
            self.execution_timeout_seconds < 1.0
            or self.execution_timeout_seconds > MAX_EXECUTION_SECONDS
        ):
            raise ValueError("gateway execution timeout is out of range")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> GatewayConfig:
        """Build the config from exactly the Podman-injected variables."""
        unknown = {
            name
            for name in environ
            if name.startswith("DARKULA_EGRESS_") and name not in _REQUIRED_ENV
        }
        if unknown:
            raise ValueError("unexpected gateway environment variable")
        missing = _REQUIRED_ENV - set(environ)
        if missing:
            raise ValueError("missing gateway environment variable")
        raw_connect = environ[ENV_CONNECT_PORT]
        raw_listen = environ[ENV_LISTEN_PORT]
        raw_timeout = environ[ENV_EXECUTION_TIMEOUT]
        if raw_connect != str(CONNECT_PORT) or raw_listen != str(LISTEN_PORT):
            raise ValueError("unsupported gateway port configuration")
        try:
            timeout = float(raw_timeout)
        except ValueError:
            raise ValueError("invalid gateway execution timeout") from None
        if raw_timeout.strip() != raw_timeout or timeout <= 0:
            raise ValueError("invalid gateway execution timeout")
        return cls(
            host=environ[ENV_HOST],
            bind_ipv4=environ[ENV_BIND_IPV4],
            connect_port=CONNECT_PORT,
            listen_port=LISTEN_PORT,
            execution_timeout_seconds=timeout,
        )


def _is_safe_global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Apply every mandatory deny-class check before trusting an answer."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return False
        if getattr(address, "scope_id", None):
            return False
        if address.is_site_local:
            return False
    if (
        address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
    ):
        return False
    return bool(address.is_global)


def is_public_unicast(address: str) -> bool:
    """Return whether ``address`` is one safe global unicast address."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return _is_safe_global(parsed)


def validate_dns_answers(answers: Iterable[tuple[int, str]]) -> str:
    """Vet every DNS answer and return the selected numeric IPv4 string.

    All answers must be safe global unicast addresses; a mixed safe/unsafe
    answer denies the whole lookup. PR 20 dials IPv4 only, so an AAAA-only
    answer is refused rather than re-resolved.
    """
    pairs = list(answers)
    if not pairs or len(pairs) > MAX_DNS_RESULTS:
        raise EgressDenied(REASON_DNS_FAILED)
    unique: dict[bytes, ipaddress.IPv4Address | ipaddress.IPv6Address] = {}
    for family, address in pairs:
        if family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(
            address, str
        ):
            raise EgressDenied(REASON_DNS_FAILED)
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise EgressDenied(REASON_DNS_FAILED) from None
        if (family == socket.AF_INET) != (parsed.version == 4):
            raise EgressDenied(REASON_DNS_FAILED)
        unique[parsed.packed] = parsed
    if len(unique) > MAX_DNS_RESULTS:
        raise EgressDenied(REASON_DNS_FAILED)
    parsed_answers = list(unique.values())
    if any(not _is_safe_global(answer) for answer in parsed_answers):
        raise EgressDenied(REASON_DENIED_IP)
    ipv4_answers = [
        answer for answer in parsed_answers if isinstance(answer, ipaddress.IPv4Address)
    ]
    if not ipv4_answers:
        raise EgressDenied("unsupported_address_family")
    ipv4_answers.sort(key=int)
    return str(ipv4_answers[0])


async def resolve_public_ipv4(host: str) -> str:
    """Resolve ``host`` afresh and return one vetted numeric IPv4 address."""
    loop = asyncio.get_running_loop()
    try:
        results = await asyncio.wait_for(
            loop.getaddrinfo(
                host,
                CONNECT_PORT,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            ),
            timeout=DNS_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError) as exc:
        raise EgressDenied(REASON_DNS_FAILED) from exc
    answers: list[tuple[int, str]] = []
    for family, _type, _proto, _canonical, sockaddr in results:
        if not sockaddr:
            raise EgressDenied(REASON_DNS_FAILED)
        answers.append((family, str(sockaddr[0])))
    return validate_dns_answers(answers)


async def numeric_connect(
    address: str, port: int, timeout_seconds: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to a vetted numeric IPv4 address; never re-resolve."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(
            loop.sock_connect(sock, (address, port)), timeout_seconds
        )
        reader, writer = await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise
    return reader, writer


__all__ = [
    "CHUNK_BYTES",
    "CONNECT_PORT",
    "CONNECT_TIMEOUT_SECONDS",
    "DNS_TIMEOUT_SECONDS",
    "ENV_BIND_IPV4",
    "ENV_CONNECT_PORT",
    "ENV_EXECUTION_TIMEOUT",
    "ENV_HOST",
    "ENV_LISTEN_PORT",
    "HEADER_TIMEOUT_SECONDS",
    "IDLE_TIMEOUT_SECONDS",
    "LISTEN_PORT",
    "MAX_BYTES_PER_DIRECTION",
    "MAX_DNS_RESULTS",
    "MAX_EXECUTION_SECONDS",
    "MAX_HEADERS",
    "MAX_HEADER_BYTES",
    "MAX_TOTAL_BYTES",
    "MAX_TUNNELS",
    "REASON_ALLOWED",
    "REASON_BUDGET_EXCEEDED",
    "REASON_CANCELLED",
    "REASON_COMPLETED",
    "REASON_CONNECT_FAILED",
    "REASON_DENIED_IP",
    "REASON_DENIED_TARGET",
    "REASON_DNS_FAILED",
    "EgressDenied",
    "GatewayConfig",
    "is_public_unicast",
    "numeric_connect",
    "resolve_public_ipv4",
    "validate_dns_answers",
]
