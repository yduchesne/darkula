# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, fixed-origin registry HTTP GET transport (PR 18).

This is **not** a general-purpose HTTP client and it is not the crawler
sandbox. It performs one bounded, anonymous, read-only GET against the
single hardcoded RansomLook origin, with:

- GET only; no request body, cookies, ``Authorization``, bearer token,
  proxy, browser/renderer, Tor, or arbitrary header propagation;
- standard verified TLS;
- ambient ``HTTP(S)_PROXY`` ignored (explicit empty ``ProxyHandler``);
- redirects denied (any 3xx is returned as its status, ``Location`` is
  never followed or trusted);
- ``Accept-Encoding: identity`` and rejection of any other encoding;
- a bounded response read (``max_response_bytes + 1``) and a per-request
  timeout.

The caller (the RansomLook adapter) builds a validated relative path from
an encoded registry entry name. A full URL is never accepted from the
provider or configuration.

Calling :func:`asyncio.to_thread` means cancelling the awaiting task may
leave at most one bounded, read-only request completing in its worker
thread. That thread holds no unit of work, database lock, credential, or
persistence capability.
"""

from __future__ import annotations

import asyncio
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from darkula.app.registry_polling import RegistrySchemaError, RegistryTransportError

#: The only production origin this transport may contact (explicit :443).
RANSOMLOOK_ORIGIN = "https://www.ransomlook.io:443"

#: Conservative per-request limits shared with the adapter.
MAX_RELATIVE_PATH_LENGTH = 2048

_PATH_ALLOWED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~%!$&'()*+,;=:@/"
)


def validate_relative_path(path: str) -> str:
    """Return a bounded absolute-path string safe to append to the origin.

    Rejects schemes, authorities, query/fragment separators, backslashes,
    whitespace, control characters, and characters that are not valid in a
    percent-encoded path. The caller must already have encoded untrusted
    entry names.
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


class _DenyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Deny every redirect instead of following an untrusted ``Location``."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _read_bounded(stream: object, max_response_bytes: int) -> bytes:
    """Read at most ``max_response_bytes + 1`` bytes and enforce the bound."""
    read: Callable[[int], bytes] = stream.read  # type: ignore[attr-defined]
    body = read(max_response_bytes + 1)
    if len(body) > max_response_bytes:
        raise RegistrySchemaError("registry response exceeds the bounded size")
    return body


def _blocking_get(
    url: str,
    *,
    timeout_seconds: float,
    max_response_bytes: int,
) -> RegistryHttpResponse:
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _DenyRedirectHandler(),
        urllib.request.HTTPSHandler(context=context),
    )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": (
                "Darkula/0.1 (read-only registry reader; RansomLook data CC BY 4.0)"
            ),
        },
    )
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        # Non-2xx statuses (including any 3xx denied above) arrive here.
        try:
            body = _read_bounded(exc, max_response_bytes)
            encoding = exc.headers.get("Content-Encoding") if exc.headers else None
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
        finally:
            exc.close()
        return RegistryHttpResponse(
            status_code=int(exc.code),
            body=body,
            content_encoding=encoding,
            retry_after=retry_after,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryTransportError("registry request failed") from exc
    with response:
        encoding = response.headers.get("Content-Encoding")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise RegistrySchemaError(
                    "registry response declared an invalid length"
                ) from exc
            if declared > max_response_bytes:
                raise RegistrySchemaError("registry response exceeds the bounded size")
        body = _read_bounded(response, max_response_bytes)
        return RegistryHttpResponse(
            status_code=int(response.status),
            body=body,
            content_encoding=encoding,
            retry_after=response.headers.get("Retry-After"),
        )


class StdlibRegistryHttpTransport:
    """Standard-library, fixed-origin, bounded read-only GET transport."""

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
        return await asyncio.to_thread(
            _blocking_get,
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
