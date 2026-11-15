# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure-stdlib public HTTPS authorization policy (PR 20).

This module is the **single** origin/path checker shared by the trusted
:class:`~darkula.crawler.controller.CrawlerController` and the sandboxed
``CrawlerRuntime`` (the Containerfile copies this exact file into
``/app/public_https_policy.py``). It therefore imports only the Python
standard library: no Darkula application modules, no database, no Playwright,
no networking/Pydantic.

Semantics frozen here (PR 20):

- a public HTTPS origin is an exact lowercase ASCII FQDN on port ``443``;
- IP literals, single-label names, private/internal/reserved names, metadata
  aliases and Unicode hostnames are never authorized;
- an authorized URL must match the exact approved host and the component
  boundary of the approved path prefix (``/docs/`` matches ``/docs/a`` but
  never ``/docs-evil`` or ``/docs``);
- percent-encoded paths are rejected wholesale (no decode-then-approve);
- fragments are rejected on outbound requests; queries are permitted because
  they never expand path authority.

Boolean checks never echo their input; the validator raises a fixed
``ValueError`` message.
"""

from __future__ import annotations

import ipaddress
import re
import string
from urllib.parse import urlsplit

#: Allowed characters in a live path (ASCII only, no percent escapes).
_PATH_CHARS = frozenset(string.ascii_letters + string.digits + "_-/.~")

#: Maximum DNS hostname length (RFC 1035 presentation form).
_MAX_HOST_LENGTH = 253

#: Maximum length of one DNS label.
_MAX_LABEL_LENGTH = 63

#: Maximum approved live path prefix length.
_MAX_PATH_PREFIX_LENGTH = 256

#: Reserved/special-use suffixes that never denote a public endpoint.
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".test",
    ".invalid",
    ".example",
    ".onion",
)

#: Exact names that are never public endpoints (metadata aliases included).
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "host.docker.internal",
        "host.containers.internal",
        "gateway.docker.internal",
        "metadata",
    }
)

#: Single DNS labels that are never accepted in any position.
_BLOCKED_LABELS = frozenset({"metadata", "internal", "localhost"})

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _has_forbidden_characters(value: str) -> bool:
    """Return whether a transport string contains controls or whitespace."""
    return any(ord(ch) < 33 or ord(ch) == 127 for ch in value)


def _valid_punycode_label(label: str) -> bool:
    """Validate the grammar of an explicit ASCII ``xn--`` label."""
    if not label.startswith("xn--"):
        return True
    encoded = label[4:]
    if not encoded:
        return False
    try:
        encoded.encode("ascii").decode("punycode")
    except UnicodeError, ValueError:
        return False
    return True


def is_safe_public_host(host: str) -> bool:
    """Return whether ``host`` is one canonical public ASCII DNS FQDN.

    Rejects (without echoing the input) IP literals, single-label names,
    uppercase/non-canonical forms, trailing dots, userinfo/whitespace,
    internal/reserved/metadata names and malformed labels.
    """
    if not isinstance(host, str):
        return False
    if not host or len(host) > _MAX_HOST_LENGTH:
        return False
    if not host.isascii() or _has_forbidden_characters(host):
        return False
    if host != host.strip() or host != host.lower():
        return False
    if host.endswith("."):
        return False
    if host in _BLOCKED_HOSTS:
        return False
    if any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        return False
    labels = host.split(".")
    if len(labels) < 2:
        return False
    if any(not label or len(label) > _MAX_LABEL_LENGTH for label in labels):
        return False
    if any(_LABEL_RE.fullmatch(label) is None for label in labels):
        return False
    if any(label in _BLOCKED_LABELS for label in labels):
        return False
    if any(not _valid_punycode_label(label) for label in labels):
        return False
    # Reject every IP literal (and numeric dotted shapes that are not valid IPs).
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    return not all(label.isdigit() for label in labels)


def _is_valid_live_path(path: str) -> bool:
    """Return whether ``path`` is a normalized, safe absolute live path."""
    if not isinstance(path, str) or not path:
        return False
    if not path.startswith("/"):
        return False
    if "%" in path or "\\" in path:
        return False
    if any(ch not in _PATH_CHARS for ch in path):
        return False
    if "//" in path:
        return False
    return all(segment not in (".", "..") for segment in path.split("/")[1:])


def validate_live_path_prefix(path: str) -> str:
    """Validate and return one approved live path prefix.

    Raises a fixed ``ValueError`` message (never echoing the input).
    """
    if not isinstance(path, str):
        raise ValueError("invalid live path prefix")
    if not path or len(path) > _MAX_PATH_PREFIX_LENGTH:
        raise ValueError("invalid live path prefix")
    if not path.isascii():
        raise ValueError("invalid live path prefix")
    if path != "/" and not path.endswith("/"):
        raise ValueError("invalid live path prefix")
    if not _is_valid_live_path(path):
        raise ValueError("invalid live path prefix")
    return path


def is_allowed_https_url(url: str, host: str, path_prefix: str) -> bool:
    """Return whether ``url`` is authorized by the exact public HTTPS grant.

    The host must match exactly (parsed origin semantics), the effective port
    must be ``443``, the scheme must be ``https``, no userinfo/fragment may be
    present, and the path must sit under the component boundary of
    ``path_prefix``. Malformed values return ``False``.
    """
    if not isinstance(url, str) or not isinstance(host, str):
        return False
    if not isinstance(path_prefix, str):
        return False
    if not url or _has_forbidden_characters(url) or any(ch.isspace() for ch in url):
        return False
    try:
        prefix = validate_live_path_prefix(path_prefix)
    except ValueError:
        return False
    if not is_safe_public_host(host):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.fragment:
        return False
    if port is None:
        port = 443
    if port != 443:
        return False
    if (parts.hostname or "") != host:
        return False
    path = parts.path or "/"
    if not _is_valid_live_path(path):
        return False
    if prefix == "/":
        return True
    return path.startswith(prefix)


__all__ = [
    "is_allowed_https_url",
    "is_safe_public_host",
    "validate_live_path_prefix",
]
