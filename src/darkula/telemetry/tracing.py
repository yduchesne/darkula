# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical OpenTelemetry tracer access (PR 3).

The instrumentation scope is the stable ``darkula`` scope. ``get_tracer`` is
the injectable module seam used by the decorators and by deterministic tests:
passing an explicit ``tracer_provider`` binds the tracer to an in-memory
provider without touching the process-global OpenTelemetry state.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.util.types import Attributes

#: Stable OpenTelemetry instrumentation scope for all Darkula telemetry.
INSTRUMENTATION_SCOPE = "darkula"

#: Instrumentation/library version reported to OpenTelemetry.
_TELEMETRY_VERSION = "0.1.0"

#: Re-exported for callers that construct spans/detach attributes directly.
AttributesT = Attributes


def get_tracer(
    *,
    tracer_provider: trace.TracerProvider | None = None,
) -> trace.Tracer:
    """Return the canonical Darkula tracer.

    With no provider, the tracer is bound to the process-global provider (an
    API no-op proxy when none has been configured). Passing an explicit
    provider is the deterministic unit-test and composition seam and never
    manipulates global OpenTelemetry state.
    """
    if tracer_provider is not None:
        return trace.get_tracer(
            INSTRUMENTATION_SCOPE,
            _TELEMETRY_VERSION,
            tracer_provider=tracer_provider,
        )
    return trace.get_tracer(INSTRUMENTATION_SCOPE, _TELEMETRY_VERSION)


__all__ = [
    "INSTRUMENTATION_SCOPE",
    "AttributesT",
    "get_tracer",
]
