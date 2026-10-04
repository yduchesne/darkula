# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula application-facing Crawler contract (PR 7).

``Crawler`` is the single application contract for bounded source
inspection. Implementation is split across the trust boundary: the trusted
:class:`~darkula.crawler.controller.CrawlerController` validates, derives a
least-capability sandbox policy, orchestrates the
:class:`~darkula.sandbox.contracts.Sandbox`, and decodes bounded runtime
output; a minimal sandboxed ``CrawlerRuntime`` performs hostile network
interaction and rendering. Application code must never depend on Podman,
Playwright, Chromium, container identities, Fake World types, or sandbox
implementation details.

Rules frozen here (PR 7 invariants):

- URL authorization uses parsed scheme/host/port **origin** semantics,
  never string-prefix matching;
- every budget (pages/requests/depth/time) is bounded and validated before
  sandbox creation;
- credentials are bounded execution-scoped material that never appears in
  logs or telemetry;
- results are bounded observations (pages + discovered links); PR 8 owns
  normalization, durable artifacts, and ObjectStore persistence;
- there is no normalization/evidence/extraction/persistence/collection
  semantics in this contract and no agent work (PR 10).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

#: Valid host character set for the parsed-origin model (ASCII, bounded).
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")

#: Bounded token shape for request identities (safe for logs/telemetry).
_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

#: Upper bound for a single credential field.
_MAX_CREDENTIAL_FIELD_LENGTH = 512

#: Upper bound on the number of observations in a result.
_MAX_OBSERVATIONS = 1000


class CrawlStatus(StrEnum):
    """Bounded outcome vocabulary of one crawl execution.

    Distinct from sandbox exit reasons: a crawl maps sandbox outcomes to
    these application-level statuses. ``CANCELLED`` is never materialized as
    a result status — caller cancellation propagates ``CancelledError``.
    """

    COMPLETED = "completed"
    WORKLOAD_FAILED = "workload_failed"
    TIMED_OUT = "timed_out"
    RESOURCE_LIMITED = "resource_limited"
    POLICY_VIOLATION = "policy_violation"
    TERMINATED = "terminated"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    INVALID_OUTPUT = "invalid_output"


class InvalidCrawlRequest(ValueError):
    """A CrawlRequest failed pre-sandbox validation.

    Raised by :class:`CrawlerController` before any sandbox/container is
    created; messages are bounded and never echo credentials.
    """


def _validate_request_id(raw: str) -> str:
    """Return a trimmed, bounded, token-shaped request identity."""
    if not isinstance(raw, str):
        raise InvalidCrawlRequest("request_id must be a string")
    value = raw.strip()
    if _ID_TOKEN_RE.fullmatch(value) is None:
        raise InvalidCrawlRequest(
            "request_id must match the bounded token shape "
            "'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'"
        )
    return value


def _parse_port(scheme: str, raw_port: str | None) -> int:
    """Return the explicit/default port for a parsed origin or fail closed."""
    if raw_port is None or raw_port == "":
        if scheme == "http":
            return 80
        if scheme == "https":
            return 443
        raise InvalidCrawlRequest(f"unsupported scheme: {scheme!r}")
    if not raw_port.isascii() or not raw_port.isdigit():
        raise InvalidCrawlRequest(f"invalid port in origin: {raw_port!r}")
    port = int(raw_port)
    if port < 1 or port > 65535:
        raise InvalidCrawlRequest(f"port out of range: {port}")
    return port


@dataclass(frozen=True, slots=True)
class AllowedOrigin:
    """Parsed scheme://host:port navigation origin (authorization boundary).

    Equality uses parsed origin semantics: ``http://BlackGate.EXAMPLE.test``
    and ``http://blackgate.example.test:80`` are the same origin;
    ``http://allowed.example.evil`` differs because the host differs
    (guarding against ``allowed.example``/``allowed.example.evil`` prefix
    confusion).
    """

    scheme: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if self.scheme not in ("http", "https"):
            raise InvalidCrawlRequest(
                f"origin scheme must be http or https (got {self.scheme!r})"
            )
        if _HOST_RE.fullmatch(self.host) is None or len(self.host) > 253:
            raise InvalidCrawlRequest(f"invalid origin host: {self.host!r}")
        if self.port < 1 or self.port > 65535:
            raise InvalidCrawlRequest(f"invalid origin port: {self.port}")

    @classmethod
    def parse(cls, url: str) -> AllowedOrigin:
        """Parse an absolute URL into its navigation origin or fail closed.

        Rejects userinfo, non-http(s) schemes, malformed hosts/ports, and
        anything that is not a clean absolute URL. The URL ``path/query`` is
        ignored for origin purposes but must be absent for ``parse`` to be
        used on pure origins; :meth:`from_url` is the general entry point.
        """
        if not isinstance(url, str) or not url.strip():
            raise InvalidCrawlRequest("origin must not be blank")
        try:
            parts = urlsplit(url.strip())
            # Accessing hostname/port performs additional parsing; any
            # failure is a malformed origin.
            _ = parts.hostname
            raw_port = parts.port
            port = _parse_port(
                parts.scheme, str(raw_port) if raw_port is not None else None
            )
        except ValueError as exc:
            raise InvalidCrawlRequest(f"malformed origin: {url.strip()!r}") from exc
        if parts.scheme not in ("http", "https"):
            raise InvalidCrawlRequest(
                f"origin scheme must be http or https (got {parts.scheme!r})"
            )
        if parts.username is not None or parts.password is not None:
            raise InvalidCrawlRequest("origin must not contain userinfo")
        host = (parts.hostname or "").strip().lower()
        if not host or _HOST_RE.fullmatch(host) is None or len(host) > 253:
            raise InvalidCrawlRequest(f"invalid origin host: {host!r}")
        return cls(scheme=parts.scheme, host=host, port=port)

    @classmethod
    def from_url(cls, url: str) -> AllowedOrigin:
        """Parse the origin of an absolute (possibly path-bearing) URL."""
        if not isinstance(url, str) or not url.strip():
            raise InvalidCrawlRequest("url must not be blank")
        try:
            parts = urlsplit(url.strip())
        except ValueError as exc:
            raise InvalidCrawlRequest(f"malformed url: {url.strip()!r}") from exc
        origin_url = f"{parts.scheme}://{parts.netloc}"
        return cls.parse(origin_url)

    def netloc(self) -> str:
        """Return the ``host:port`` network location (port always explicit)."""
        return f"{self.host}:{self.port}"

    def as_origin_url(self) -> str:
        """Return the canonical ``scheme://host:port`` origin string."""
        return f"{self.scheme}://{self.netloc()}"

    def contains(self, url: str) -> bool:
        """Return whether an absolute URL belongs to this origin exactly.

        Uses parsed scheme/host/port equality; never string-prefix matching.
        """
        try:
            origin = AllowedOrigin.from_url(url)
        except InvalidCrawlRequest:
            return False
        return origin == self


@dataclass(frozen=True, slots=True)
class CrawlCredentials:
    """Bounded, execution-scoped source authentication material (BlackGate).

    The synthetic Fake World login crosses into the sandbox by design
    (narrowly scoped source authentication); never log it, never place it in
    telemetry, never persist it. PR 7 deliberately has no secret-vault
    framework.
    """

    username: str
    password: str

    def __post_init__(self) -> None:
        if not isinstance(self.username, str) or not self.username.strip():
            raise InvalidCrawlRequest("credentials username must not be blank")
        if not isinstance(self.password, str):
            raise InvalidCrawlRequest("credentials password must be a string")
        if len(self.username) > _MAX_CREDENTIAL_FIELD_LENGTH:
            raise InvalidCrawlRequest("credentials username exceeds the bound")
        if len(self.password) > _MAX_CREDENTIAL_FIELD_LENGTH:
            raise InvalidCrawlRequest("credentials password exceeds the bound")
        if any(ord(ch) < 32 for ch in self.username + self.password):
            raise InvalidCrawlRequest("credentials must not contain control characters")


def _validate_budget(value: int, *, name: str, minimum: int, maximum: int) -> int:
    """Validate one integer budget against a closed range."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidCrawlRequest(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise InvalidCrawlRequest(f"{name} must be within [{minimum}, {maximum}]")
    return value


@dataclass(frozen=True, slots=True)
class CrawlRequest:
    """One bounded crawl authorization.

    ``allowed_origin`` is the navigation boundary the requester grants; the
    controller validates that ``start_url`` belongs to it and derives a
    least-capability sandbox policy from it. Everything outside the origin is
    denied at both crawler logic and sandbox network topology.
    """

    request_id: str
    start_url: str
    allowed_origin: AllowedOrigin
    credentials: CrawlCredentials | None = None
    max_pages: int = 20
    max_requests: int = 60
    max_depth: int = 4
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _validate_request_id(self.request_id))
        # Normalize/validate the start URL and enforce origin membership.
        if not isinstance(self.start_url, str) or not self.start_url.strip():
            raise InvalidCrawlRequest("start_url must not be blank")
        try:
            parts = urlsplit(self.start_url.strip())
        except ValueError as exc:
            raise InvalidCrawlRequest("malformed start_url") from exc
        if parts.scheme not in ("http", "https") or parts.netloc == "":
            raise InvalidCrawlRequest("start_url must be an absolute http(s) URL")
        if parts.username is not None or parts.password is not None:
            raise InvalidCrawlRequest("start_url must not contain userinfo")
        if any(ord(ch) < 32 for ch in self.start_url):
            raise InvalidCrawlRequest("start_url must not contain control characters")
        if not self.allowed_origin.contains(self.start_url):
            raise InvalidCrawlRequest(
                "start_url is outside the allowed origin; "
                "authorized navigation is origin-scoped"
            )
        if self.timeout_seconds <= 0 or self.timeout_seconds > 86400:
            raise InvalidCrawlRequest("timeout_seconds must be within (0, 86400]")
        object.__setattr__(
            self,
            "max_pages",
            _validate_budget(
                self.max_pages, name="max_pages", minimum=1, maximum=10000
            ),
        )
        object.__setattr__(
            self,
            "max_requests",
            _validate_budget(
                self.max_requests, name="max_requests", minimum=1, maximum=100000
            ),
        )
        object.__setattr__(
            self,
            "max_depth",
            _validate_budget(self.max_depth, name="max_depth", minimum=1, maximum=64),
        )


@dataclass(frozen=True, slots=True)
class CrawlPageObservation:
    """One bounded observed page (presentation identity, not evidence).

    ``title`` and ``text_excerpt`` are bounded hostile/untrusted content by
    convention — never telemetry, never truth. ``url`` may carry a query
    string (pagination); PR 8 owns normalization.
    """

    url: str
    depth: int
    http_status: int | None
    rendered: bool
    title: str
    text_excerpt: str


@dataclass(frozen=True, slots=True)
class CrawlResult:
    """Bounded outcome of one crawl execution.

    ``status`` distinguishes completed/workload-failed/timed-out/
    resource-limited/policy-violated/terminated/sandbox-unavailable/
    invalid-output. ``reason`` is a bounded human-safe failure detail with no
    credentials, no raw hostile body, and no container internals.
    """

    request_id: str
    status: CrawlStatus
    pages: tuple[CrawlPageObservation, ...] = ()
    discovered_links: tuple[str, ...] = ()
    requests: int = 0
    duration_seconds: float = 0.0
    reason: str | None = None
    protocol_version: int | None = None

    def __post_init__(self) -> None:
        if len(self.pages) > _MAX_OBSERVATIONS:
            raise InvalidCrawlRequest(
                f"crawl result exceeds the {_MAX_OBSERVATIONS}-observation bound"
            )
        if self.requests < 0:
            raise InvalidCrawlRequest("crawl result requests must be non-negative")
        object.__setattr__(self, "request_id", _validate_request_id(self.request_id))


class Crawler(ABC):
    """Darkula-owned application-facing crawler capability.

    One contract; no competing crawler abstractions. Implementations must
    validate before creating sandbox resources, must never browse directly,
    and must preserve cancellation (``asyncio.CancelledError`` propagates).
    """

    @abstractmethod
    async def crawl(self, request: CrawlRequest) -> CrawlResult:
        """Execute one bounded crawl and return its typed result."""
        raise NotImplementedError


__all__ = [
    "AllowedOrigin",
    "CrawlCredentials",
    "CrawlPageObservation",
    "CrawlRequest",
    "CrawlResult",
    "CrawlStatus",
    "Crawler",
    "InvalidCrawlRequest",
]
