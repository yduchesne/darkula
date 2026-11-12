# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, fixed-origin registry HTTP GET transport (PR 18 / PR 18-F).

This is **not** a general-purpose HTTP client and it is not the crawler
sandbox. It performs one bounded, anonymous, read-only GET against the
single hardcoded RansomLook origin using HTTPX asynchronous streaming:

- GET only; no request body, cookies, ``Authorization``, bearer token,
  user-defined headers, proxy, browser/renderer, Tor, or redirect following;
- standard verified TLS with environment-derived proxy/SSL settings disabled;
- ambient ``HTTP(S)_PROXY`` ignored (``trust_env=False`` and ``proxy=None``);
- redirects denied (any 3xx is returned as its status, ``Location`` is
  never followed or trusted);
- ``Accept-Encoding: identity`` and rejection of any other encoding;
- a total elapsed-time deadline covering connect, headers, raw body stream
  and close, plus a bounded raw byte read (``max_response_bytes``).

The caller (the RansomLook adapter) builds a validated relative path from
an encoded registry entry name. A full URL is never accepted from the
provider or configuration.

Unlike the previous blocking ``urllib``/``asyncio.to_thread`` implementation,
timeouts and caller cancellation cancel the actual async socket read and
close the connection; no worker thread can retain a live HTTP transfer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx

from darkula.app.registry_polling import RegistrySchemaError, RegistryTransportError

#: The only production origin this transport may contact (explicit :443).
RANSOMLOOK_ORIGIN = "https://www.ransomlook.io:443"

#: Conservative per-request limits shared with the adapter.
MAX_RELATIVE_PATH_LENGTH = 2048

#: Hard cap for one raw stream chunk read.
_READ_CHUNK_SIZE = 65536

_PATH_ALLOWED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~%!$&'()*+,;=:@/"
)

_REQUEST_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "Darkula/0.1 (read-only registry reader; RansomLook data CC BY 4.0)",
}


def validate_relative_path(path: str) -> str:
    """Return a bounded absolute-path string safe to append to the origin.

    Rejects schemes, authorities, query/fragment separators, backslashes,
    whitespace, control characters, dot segments, and characters that are
    not valid in a percent-encoded path. The caller must already have
    encoded untrusted entry names.
    """
    if not isinstance(path, str):
        raise ValueError("registry path must be a string")
    if not path.startswith("/"):
        raise ValueError("registry path must be absolute")
    if path.startswith("//"):
        raise ValueError("registry path must not start with '//'")
    if len(path) > MAX_RELATIVE_PATH_LENGTH:
        raise ValueError("registry path is too long")
    if "://" in path:
        raise ValueError("registry path must not contain a scheme")
    if "?" in path or "#" in path or "\\" in path:
        raise ValueError("registry path must not contain query/fragment/backslash")
    for character in path:
        if character not in _PATH_ALLOWED:
            raise ValueError("registry path contains a disallowed character")
    if ".." in path.split("/"):
        raise ValueError("registry path must not contain dot segments")
    if path == "/api/groups":
        return path
    if path.startswith("/api/group/"):
        remainder = path[len("/api/group/") :]
        if not remainder or "/" in remainder:
            raise ValueError("registry path must target exactly one group entry")
        return path
    raise ValueError("registry path is not an allowlisted public endpoint")


@dataclass(frozen=True, slots=True)
class RegistryHttpResponse:
    """One bounded registry HTTP response (transport-level)."""

    status_code: int
    body: bytes
    content_encoding: str | None = None
    retry_after: str | None = None


class RegistryHttpResponseLike(Protocol):
    """Read-only transport response shape (structural, fakes need no import)."""

    @property
    def status_code(self) -> int: ...

    @property
    def body(self) -> bytes: ...

    @property
    def content_encoding(self) -> str | None: ...

    @property
    def retry_after(self) -> str | None: ...


class RegistryHttpTransport(Protocol):
    """Injectable registry transport contract (tests fake only this)."""

    async def get(
        self,
        path: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> RegistryHttpResponseLike: ...


def _validate_content_encoding(encoding: str | None) -> None:
    """Reject any response encoding other than an absent/identity encoding."""
    if encoding is None:
        return
    if encoding.strip().lower() in ("", "identity"):
        return
    raise RegistrySchemaError("registry response used an unsupported encoding")


def _declared_length(content_length: str | None, *, max_response_bytes: int) -> None:
    """Reject an invalid or over-cap declared ``Content-Length``."""
    if content_length is None:
        return
    candidate = content_length.strip()
    if not candidate or not all("0" <= character <= "9" for character in candidate):
        raise RegistrySchemaError("registry response declared an invalid length")
    if int(candidate) > max_response_bytes:
        raise RegistrySchemaError("registry response exceeds the bounded size")


async def _async_get_url(
    url: str,
    *,
    timeout_seconds: float,
    max_response_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RegistryHttpResponse:
    """Perform one bounded physical GET with an elapsed-time deadline.

    ``transport`` exists only as a private deterministic-test seam; production
    callers never supply it and no public path accepts an arbitrary URL.
    """
    try:
        async with (
            asyncio.timeout(timeout_seconds),
            httpx.AsyncClient(
                verify=True,
                trust_env=False,
                proxy=None,
                follow_redirects=False,
                http1=True,
                http2=False,
                timeout=httpx.Timeout(timeout_seconds),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                transport=transport,
            ) as client,
            client.stream(
                "GET",
                url,
                headers=_REQUEST_HEADERS,
                follow_redirects=False,
            ) as response,
        ):
            encoding = response.headers.get("Content-Encoding")
            retry_after = response.headers.get("Retry-After")
            if response.status_code != 200:
                # Never consume an error/redirect body.
                return RegistryHttpResponse(
                    status_code=response.status_code,
                    body=b"",
                    content_encoding=encoding,
                    retry_after=retry_after,
                )
            _validate_content_encoding(encoding)
            _declared_length(
                response.headers.get("Content-Length"),
                max_response_bytes=max_response_bytes,
            )
            body = bytearray()
            async for chunk in response.aiter_raw(
                chunk_size=min(_READ_CHUNK_SIZE, max_response_bytes + 1)
            ):
                if len(body) + len(chunk) > max_response_bytes:
                    raise RegistrySchemaError(
                        "registry response exceeds the bounded size"
                    )
                body.extend(chunk)
            return RegistryHttpResponse(
                status_code=200,
                body=bytes(body),
                content_encoding=encoding,
                retry_after=retry_after,
            )
    except TimeoutError, httpx.TransportError:
        raise RegistryTransportError("registry request failed") from None


class StdlibRegistryHttpTransport:
    """Fixed-origin, bounded read-only async HTTPX GET transport.

    The historical class name is retained for compatibility; the
    implementation is HTTPX asynchronous streaming with a per-physical-attempt
    elapsed deadline and no ``asyncio.to_thread``/``urllib`` worker.
    """

    def __init__(self, *, origin: str = RANSOMLOOK_ORIGIN) -> None:
        if origin != RANSOMLOOK_ORIGIN:
            raise ValueError("the registry origin is fixed and not configurable")
        self._origin = origin

    def build_url(self, path: str) -> str:
        """Return the fixed-origin absolute URL for a validated path."""
        return self._origin + validate_relative_path(path)

    async def get(
        self,
        path: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> RegistryHttpResponse:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        url = self.build_url(path)
        return await _async_get_url(
            url,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )


__all__ = [
    "MAX_RELATIVE_PATH_LENGTH",
    "RANSOMLOOK_ORIGIN",
    "RegistryHttpResponse",
    "RegistryHttpResponseLike",
    "RegistryHttpTransport",
    "StdlibRegistryHttpTransport",
    "validate_relative_path",
]
