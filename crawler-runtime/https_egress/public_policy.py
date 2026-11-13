# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Gateway-local access to the shared pure public-HTTPS policy module."""

from __future__ import annotations

try:  # package import (application/tests)
    from darkula.crawler import public_https_policy as _policy
except ImportError:  # pragma: no cover - gateway image flat copy at /app
    import public_https_policy as _policy  # type: ignore[no-redef,import-not-found]

is_safe_public_host = _policy.is_safe_public_host

__all__ = ["is_safe_public_host"]
