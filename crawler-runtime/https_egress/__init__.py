# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Least-privilege public HTTPS CONNECT gateway (PR 20).

The gateway is a dedicated egress security component inside a disposable
Podman container. It is not an agent HTTP tool, a trusted-application HTTP
client, or a general forwarding proxy.
"""

from https_egress.gateway import HttpsEgressGateway, parse_connect_request
from https_egress.policy import (
    CONNECT_PORT,
    LISTEN_PORT,
    EgressDenied,
    GatewayConfig,
    is_public_unicast,
    numeric_connect,
    resolve_public_ipv4,
    validate_dns_answers,
)

__all__ = [
    "CONNECT_PORT",
    "LISTEN_PORT",
    "EgressDenied",
    "GatewayConfig",
    "HttpsEgressGateway",
    "is_public_unicast",
    "numeric_connect",
    "parse_connect_request",
    "resolve_public_ipv4",
    "validate_dns_answers",
]
