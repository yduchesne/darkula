# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Fake World HTTP adapter tests (PR 7) — matrix H1-H12.

Exercise the real HTTP translation seam (`FakeWorldHttpService.handle`)
without sockets; the threaded stdlib server wrapping it is covered by the
integration slice.
"""

from __future__ import annotations

import urllib.parse

from darkula.testing.fake_world.http_adapter import (
    SESSION_COOKIE,
    FakeWorldHttpService,
    HttpWireResponse,
)
from darkula.testing.fake_world.rendering import FakeWorldRenderer

LOGIN_USERNAME = "zerofox77"
LOGIN_PASSWORD = "blackgate-test-password"


def service() -> FakeWorldHttpService:
    return FakeWorldHttpService(FakeWorldRenderer())


def _cookie_from(wire: HttpWireResponse) -> str:
    for name, value in wire.headers:
        if name.lower() == "set-cookie":
            return value
    raise AssertionError(f"no set-cookie header in {wire.status_code} response")


def login(svc: FakeWorldHttpService) -> str:
    """Establish a session through real HTTP semantics and return its cookie."""
    body = urllib.parse.urlencode(
        {"username": LOGIN_USERNAME, "password": LOGIN_PASSWORD, "next": "/index"}
    ).encode()
    wire = svc.handle(
        "POST",
        "/login",
        headers={"content-type": "application/x-www-form-urlencoded"},
        body=body,
    )
    assert wire.status_code == 302
    return _cookie_from(wire)


class TestRoutes:
    def test_h1_landing_get(self) -> None:
        wire = service().handle("GET", "/")
        assert wire.status_code == 200
        assert b"BlackGate" in wire.body

    def test_h1_landing_links(self) -> None:
        wire = service().handle("GET", "/")
        assert b'href="/login"' in wire.body
        assert b'href="/index"' in wire.body

    def test_h2_login_post_cookie_and_redirect(self) -> None:
        svc = service()
        cookie = login(svc)
        assert cookie.startswith(f"{SESSION_COOKIE}=")

    def test_h3_protected_page_200_with_session(self) -> None:
        svc = service()
        cookie = login(svc)
        wire = svc.handle("GET", "/board/board-access", headers={"cookie": cookie})
        assert wire.status_code == 200
        assert b"Access &amp; Credentials" in wire.body

    def test_h4_expired_session_renders_reauth(self) -> None:
        svc = service()
        cookie = login(svc)
        # Exhaust the protected-request budget (12 per session).
        wire = None
        for _ in range(13):
            wire = svc.handle("GET", "/board/board-access", headers={"cookie": cookie})
        assert wire is not None
        assert wire.status_code == 302
        assert "location" in {k.lower() for k, _ in wire.headers}

    def test_h5_pagination_query_preserved(self) -> None:
        svc = service()
        cookie = login(svc)
        wire = svc.handle(
            "GET",
            "/thread/thr-hospital-creds",
            query={"page": "2"},
            headers={"cookie": cookie},
        )
        assert wire.status_code == 200
        wrapper = {}
        for name, value in wire.headers:
            wrapper[name.lower()] = value
        assert "Page 2 of 2" in wire.body.decode()

    def test_h6_attachment_safe_body_type_disposition(self) -> None:
        svc = service()
        cookie = login(svc)
        wire = svc.handle(
            "GET", "/attachment/att-cred-list", headers={"cookie": cookie}
        )
        assert wire.status_code == 200
        lowered = {name.lower() for name, _ in wire.headers}
        assert "content-disposition" in lowered
        assert wire.body

    def test_h6_attachment_without_session_401(self) -> None:
        wire = service().handle("GET", "/attachment/att-cred-list")
        assert wire.status_code == 401

    def test_h7_redirect_real_http_semantics(self) -> None:
        svc = service()
        wire = svc.handle("GET", "/old-thread/thr-hospital-creds")
        assert wire.status_code == 302
        locations = [v for k, v in wire.headers if k.lower() == "location"]
        assert locations and "/thread/thr-hospital-creds" in locations[0]

    def test_h7_protected_page_redirects_to_login(self) -> None:
        wire = service().handle("GET", "/board/board-access")
        assert wire.status_code == 302
        locations = [v for k, v in wire.headers if k.lower() == "location"]
        assert locations[0].startswith("/login?next=")


class TestRuntimeBehavior:
    def test_h8_rate_limit_429_with_retry_after(self) -> None:
        svc = service()
        cookie = login(svc)
        # Archives board allows 2 views per session.
        svc.handle("GET", "/board/board-archives", headers={"cookie": cookie})
        svc.handle("GET", "/board/board-archives", headers={"cookie": cookie})
        wire = svc.handle("GET", "/board/board-archives", headers={"cookie": cookie})
        assert wire.status_code == 429
        lowered = {k.lower(): v for k, v in wire.headers}
        assert lowered["retry-after"] == "60"

    def test_h9_failure_script_503_then_200(self) -> None:
        svc = service()
        cookie = login(svc)
        # thr-intros has a 1-request failing schedule.
        first = svc.handle("GET", "/thread/thr-intros", headers={"cookie": cookie})
        assert first.status_code == 503
        second = svc.handle("GET", "/thread/thr-intros", headers={"cookie": cookie})
        assert second.status_code == 200

    def test_h10_malformed_html_served(self) -> None:
        wire = service().handle("GET", "/legacy/board-announcements")
        assert wire.status_code == 200
        assert b"<h2>Legacy BlackGate announcements" in wire.body

    def test_h12_new_server_deterministic_reset(self) -> None:
        svc_a = service()
        cookie_a = login(svc_a)
        assert (
            svc_a.handle(
                "GET", "/board/board-access", headers={"cookie": cookie_a}
            ).status_code
            == 200
        )
        # A brand-new server has no sessions and starts canonical state.
        svc_b = service()
        wire = svc_b.handle("GET", "/board/board-access")
        assert wire.status_code == 302  # re-auth required, deterministic


class TestTruthNonExposure:
    def test_h11_hidden_truth_never_crosses_http(self) -> None:
        svc = service()
        cookie = login(svc)
        requests = [
            ("GET", "/", None, None),
            ("GET", "/index", {"cookie": cookie}, None),
            ("GET", "/board/board-access", {"cookie": cookie}, None),
            ("GET", "/board/board-archives", {"cookie": cookie}, None),
            ("GET", "/thread/thr-hospital-creds", {"cookie": cookie}, None),
            ("GET", "/thread/thr-hospital-creds", {"cookie": cookie}, {"page": "2"}),
            ("GET", "/thread/thr-intros", {"cookie": cookie}, None),
            ("GET", "/attachment/att-cred-list", {"cookie": cookie}, None),
            ("GET", "/legacy/board-announcements", None, None),
        ]
        # Truth-only internal identifiers (never present in any rendered
        # observation; hidden from HTTP by construction).
        truth_only = ("actor-001", "alias-002", "relationship-001")
        for method, path, headers, query in requests:
            wire = svc.handle(method, path, headers=headers or {}, query=query or {})
            assert wire.status_code != 500, path
            for token in truth_only:
                assert token.encode() not in wire.body, (
                    f"truth token {token!r} leaked through {path}"
                )
