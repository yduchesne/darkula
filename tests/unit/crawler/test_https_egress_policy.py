# SPDX-License-Identifier: AGPL-3.0-only
"""PR 20 egress policy / DNS / CONNECT-parser tests (U20-20..U20-27)."""

from __future__ import annotations

import socket

import pytest

from https_egress.gateway import _HttpError, parse_connect_request
from https_egress.policy import (
    EgressDenied,
    GatewayConfig,
    is_public_unicast,
    validate_dns_answers,
)

HOST = "approved.example.org"
CONNECT = f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n\r\n".encode()


class TestDnsClassification:
    @pytest.mark.parametrize(
        "address",
        [
            "8.8.8.8",
            "1.1.1.1",
            "93.184.216.34",
            "2606:4700:4700::1111",
            "2a00:1450:4001:81b::200e",
        ],
    )
    def test_u20_22_safe_global_addresses_accepted(self, address: str) -> None:
        assert is_public_unicast(address) is True

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "0.0.0.0",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "192.0.0.1",
            "198.18.0.1",
            "192.0.2.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "255.255.255.255",
            "::1",
            "::",
            "fc00::1",
            "fe80::1",
            "::ffff:127.0.0.1",
        ],
    )
    def test_u20_22_unsafe_addresses_rejected(self, address: str) -> None:
        assert is_public_unicast(address) is False

    def test_u20_22_zone_identifier_rejected(self) -> None:
        assert is_public_unicast("fe80::1%eth0") is False

    def test_u20_22_mixed_answers_deny_whole_lookup(self) -> None:
        with pytest.raises(EgressDenied) as excinfo:
            validate_dns_answers(
                [
                    (socket.AF_INET, "8.8.8.8"),
                    (socket.AF_INET, "10.0.0.1"),
                ]
            )
        assert excinfo.value.reason == "denied_ip"

    def test_u20_23_aaaa_only_is_unsupported(self) -> None:
        with pytest.raises(EgressDenied) as excinfo:
            validate_dns_answers([(socket.AF_INET6, "2606:4700:4700::1111")])
        assert excinfo.value.reason == "unsupported_address_family"

    def test_u20_23_ipv4_mapped_rejected(self) -> None:
        with pytest.raises(EgressDenied):
            validate_dns_answers([(socket.AF_INET6, "::ffff:8.8.8.8")])

    def test_u20_24_sorted_numeric_ipv4_selection(self) -> None:
        selected = validate_dns_answers(
            [
                (socket.AF_INET, "93.184.216.35"),
                (socket.AF_INET, "1.2.3.4"),
                (socket.AF_INET, "93.184.216.34"),
            ]
        )
        assert selected == "1.2.3.4"

    def test_u20_24_duplicate_answers_are_deduplicated(self) -> None:
        assert (
            validate_dns_answers(
                [
                    (socket.AF_INET, "8.8.8.8"),
                    (socket.AF_INET, "8.8.8.8"),
                ]
            )
            == "8.8.8.8"
        )

    @pytest.mark.parametrize(
        "answers",
        [
            [],
            [(socket.AF_INET, "not-an-ip")],
            [(socket.AF_INET6, "8.8.8.8")],
            [(999, "8.8.8.8")],
        ],
    )
    def test_u20_26_malformed_or_empty_answers_deny(
        self, answers: list[tuple[int, str]]
    ) -> None:
        with pytest.raises(EgressDenied) as excinfo:
            validate_dns_answers(answers)
        assert excinfo.value.reason == "dns_failed"

    def test_u20_26_more_than_16_answers_deny(self) -> None:
        answers = [(socket.AF_INET, f"8.8.8.{index}") for index in range(1, 18)]
        with pytest.raises(EgressDenied) as excinfo:
            validate_dns_answers(answers)
        assert excinfo.value.reason == "dns_failed"


class TestConnectParser:
    def test_u20_20_canonical_connect_accepted(self) -> None:
        parse_connect_request(CONNECT, HOST)
        parse_connect_request(
            f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode(), HOST
        )
        with pytest.raises(_HttpError):
            parse_connect_request(f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode(), HOST)

    @pytest.mark.parametrize(
        "raw",
        [
            "CONNECT other.example.org:443 HTTP/1.1\r\n\r\n",
            f"CONNECT {HOST}.evil.com:443 HTTP/1.1\r\n\r\n",
            f"CONNECT {HOST}:8443 HTTP/1.1\r\n\r\n",
            "CONNECT 93.184.216.34:443 HTTP/1.1\r\n\r\n",
            f"GET {HOST}:443 HTTP/1.1\r\n\r\n",
            f"POST {HOST}:443 HTTP/1.1\r\n\r\n",
            f"CONNECT https://{HOST}:443 HTTP/1.1\r\n\r\n",
            f"CONNECT {HOST}:443 HTTP/1.0\r\n\r\n",
            f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}\r\nHost: {HOST}\r\n\r\n",
            f"CONNECT {HOST}:443 HTTP/1.1\r\nContent-Length: 3\r\n\r\n",
            f"CONNECT {HOST}:443 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
            f"CONNECT {HOST}:443 HTTP/1.1\r\nAuthorization: Bearer x\r\n\r\n",
            f"CONNECT {HOST}:443 HTTP/1.1\r\n X-Folded: y\r\n\r\n",
            f"CONNECT {HOST}:443 HTTP/1.1\r\nBrokenHeader\r\n\r\n",
        ],
    )
    def test_u20_21_non_canonical_requests_rejected(self, raw: str) -> None:
        with pytest.raises(_HttpError) as excinfo:
            parse_connect_request(raw.encode(), HOST)
        assert excinfo.value.response.startswith(b"HTTP/1.1 4")

    def test_u20_21_non_ascii_and_nul_rejected(self) -> None:
        for raw in (
            f"CONNECT {HOST}:443 HTTP/1.1\r\nX: \xff\r\n\r\n".encode("latin-1"),
            f"CONNECT {HOST}:443 HTTP/1.1\r\nX: a\x00b\r\n\r\n".encode(),
        ):
            with pytest.raises(_HttpError):
                parse_connect_request(raw, HOST)


class TestGatewayConfig:
    def test_u20_31_wildcard_bind_rejected(self) -> None:
        with pytest.raises(ValueError):
            GatewayConfig(host=HOST, bind_ipv4="0.0.0.0")
        with pytest.raises(ValueError):
            GatewayConfig(host=HOST, bind_ipv4="8.8.8.8")
        with pytest.raises(ValueError):
            GatewayConfig(host=HOST, bind_ipv4="::")

    def test_u20_31_private_bind_accepted(self) -> None:
        config = GatewayConfig(host=HOST, bind_ipv4="10.89.1.10")
        assert config.bind_ipv4 == "10.89.1.10"

    def test_u20_11_environment_rejects_missing_extra_and_bad_ports(self) -> None:
        good = {
            "DARKULA_EGRESS_HOST": HOST,
            "DARKULA_EGRESS_BIND_IPV4": "10.89.1.10",
            "DARKULA_EGRESS_CONNECT_PORT": "443",
            "DARKULA_EGRESS_LISTEN_PORT": "3128",
            "DARKULA_EGRESS_EXECUTION_TIMEOUT": "90",
        }
        assert GatewayConfig.from_environment(good).host == HOST
        with pytest.raises(ValueError):
            GatewayConfig.from_environment({**good, "DARKULA_EGRESS_EXTRA": "1"})
        missing = dict(good)
        missing.pop("DARKULA_EGRESS_HOST")
        with pytest.raises(ValueError):
            GatewayConfig.from_environment(missing)
        with pytest.raises(ValueError):
            GatewayConfig.from_environment(
                {**good, "DARKULA_EGRESS_CONNECT_PORT": "8443"}
            )
        with pytest.raises(ValueError):
            GatewayConfig.from_environment(
                {**good, "DARKULA_EGRESS_LISTEN_PORT": "8080"}
            )
        with pytest.raises(ValueError):
            GatewayConfig.from_environment({**good, "DARKULA_EGRESS_HOST": "x.onion"})
