# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Crawler contract tests (PR 7) — matrix C1-C6, C14-C17."""

from __future__ import annotations

import pytest

from darkula.crawler.contracts import (
    AllowedOrigin,
    CrawlCredentials,
    CrawlRequest,
    InvalidCrawlRequest,
)

ORIGIN = "http://blackgate.example.test:8080"


def make_request(**overrides: object) -> CrawlRequest:
    base: dict[str, object] = {
        "request_id": "crawl-001",
        "start_url": f"{ORIGIN}/",
        "allowed_origin": AllowedOrigin.parse(ORIGIN),
        "max_pages": 20,
        "max_requests": 60,
        "max_depth": 4,
        "timeout_seconds": 60.0,
    }
    base.update(overrides)
    return CrawlRequest(**base)  # type: ignore[arg-type]


class TestValidRequests:
    def test_c1_valid_same_origin_accepted(self) -> None:
        request = make_request()
        assert request.request_id == "crawl-001"
        assert request.max_pages == 20

    def test_c1_start_url_with_path_and_query_within_origin(self) -> None:
        request = make_request(start_url=f"{ORIGIN}/thread/thr-x?page=2")
        assert request.allowed_origin.contains(request.start_url)


class TestMalformedEndpoints:
    def test_c2_malformed_endpoint_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(start_url="not a url")

    def test_c2_relative_endpoint_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(start_url="/index")

    def test_c3_unsupported_scheme_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(start_url="ftp://blackgate.example.test/")

    def test_c2_userinfo_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(start_url="http://user@example.test/")

    def test_c4_initial_endpoint_outside_allowlist_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(
                start_url="http://elsewhere.example.test/",
                allowed_origin=AllowedOrigin.parse(ORIGIN),
            )

    def test_c16_prefix_confusion_rejected(self) -> None:
        # allowed.example.evil must never match allowed.example
        with pytest.raises(InvalidCrawlRequest):
            make_request(
                start_url="http://allowed.example.evil/",
                allowed_origin=AllowedOrigin.parse("http://allowed.example"),
            )

    def test_c17_unauthorized_port_change_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(
                start_url="http://blackgate.example.test:9999/",
                allowed_origin=AllowedOrigin.parse(ORIGIN),
            )

    def test_c4_out_of_range_port_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            AllowedOrigin.parse("http://blackgate.example.test:99999")

    def test_bad_request_id_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(request_id="has space")

    def test_empty_start_url_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(start_url="")


class TestBudgets:
    def test_c5_invalid_page_budget_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(max_pages=0)

    def test_c5_negative_page_budget_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(max_pages=-3)

    def test_c5_non_integer_budget_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(max_requests=10.5)  # still rejected by post_init

    def test_c6_invalid_depth_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(max_depth=0)

    def test_c6_invalid_timeout_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(timeout_seconds=0)

    def test_c6_negative_timeout_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            make_request(timeout_seconds=-1)


class TestCredentials:
    def test_blank_username_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            CrawlCredentials(username="  ", password="pw")

    def test_oversized_credentials_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            CrawlCredentials(username="u" * 600, password="pw")

    def test_control_characters_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            CrawlCredentials(username="u\nname", password="pw")


class TestOriginSemantics:
    def test_origin_case_insensitive_host_equal(self) -> None:
        assert AllowedOrigin.parse("http://BlackGate.EXAMPLE.test") == AllowedOrigin(
            scheme="http", host="blackgate.example.test", port=80
        )

    def test_default_port_normalized(self) -> None:
        assert AllowedOrigin.parse("http://blackgate.example.test").port == 80
        assert AllowedOrigin.parse("https://blackgate.example.test").port == 443

    def test_contains_uses_parsed_semantics(self) -> None:
        origin = AllowedOrigin.parse("http://blackgate.example.test:8080")
        assert origin.contains("http://blackgate.example.test:8080/board/x?p=2")
        assert not origin.contains("https://blackgate.example.test:8080/")
        assert not origin.contains("http://blackgate.example.test:9090/")

    def test_origin_rejects_bad_host(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            AllowedOrigin(scheme="http", host="-bad-", port=80)

    def test_from_url_parses_origin(self) -> None:
        origin = AllowedOrigin.from_url("http://blackgate.example.test:8080/index")
        assert origin.netloc() == "blackgate.example.test:8080"
        assert origin.as_origin_url() == "http://blackgate.example.test:8080"
