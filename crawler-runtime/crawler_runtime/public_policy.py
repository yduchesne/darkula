# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime-local access to the shared pure public-HTTPS policy module.

The Containerfile copies ``src/darkula/crawler/public_https_policy.py`` to
``/app/public_https_policy.py``; repository tests import the package copy.
This shim resolves both layouts without duplicating policy logic.
"""

from __future__ import annotations

try:  # package import (application/tests)
    from darkula.crawler import public_https_policy as _policy
except ImportError:  # pragma: no cover - runtime image flat copy at /app
    import public_https_policy as _policy  # type: ignore[no-redef,import-not-found]

is_allowed_https_url = _policy.is_allowed_https_url
is_safe_public_host = _policy.is_safe_public_host
validate_live_path_prefix = _policy.validate_live_path_prefix

__all__ = [
    "is_allowed_https_url",
    "is_safe_public_host",
    "validate_live_path_prefix",
]
