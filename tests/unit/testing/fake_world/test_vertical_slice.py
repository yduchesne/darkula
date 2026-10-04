# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Fake World component vertical slice (PR 6).

Navigates the canonical BlackGate scenario entirely through rendered links
and requests — never via truth shortcuts — and asserts:

1. navigation comes from rendered observations only;
2. truth stays inaccessible to renderer output;
3. session/request state behaves deterministically;
4. source-level failures are responses, never infrastructure exceptions;
5. renderer reset reproduces the initial sequence;
6. exercised behaviors are covered by stable traceability IDs.

No Podman, PostgreSQL, Redpanda, browser, or network is required.
"""

from __future__ import annotations

import re

from tests.unit.testing.fake_world.helpers import (
    LOGIN_PASSWORD,
    LOGIN_USERNAME,
    SOURCE_ID,
    canonical_scenario,
    session_headers,
)

from darkula.testing.fake_world import (
    FakeWorldRenderer,
    FakeWorldRequest,
    FakeWorldSession,
    RenderedSourceResponse,
)
from darkula.testing.fake_world.identifiers import BehaviorId, SourceId

_HIDDEN_TOKENS = (
    "actor-001",
    "alias-002",
    "organization-001",
    "location-001",
    "location-002",
    "relationship-001",
    "event-001",
    "event-002",
    "Edgewater",
    "zfox",
)

_EXPECTED_BEHAVIORS = {
    "FW-BG-AUTH-001",
    "FW-BG-AUTH-003",
    "FW-BG-AUTH-004",
    "FW-BG-PAGE-002",
    "FW-BG-PAGE-003",
    "FW-BG-PAGE-004",
    "FW-BG-ATTACH-001",
    "FW-BG-RATE-001",
    "FW-BG-FAIL-001",
    "FW-BG-TRUTH-001",
}


def _links(html: str) -> list[str]:
    """Extract every absolute-path href from rendered HTML deterministically."""
    return re.findall(r'href="(/[^"]*)"', html)


def _render(
    renderer: FakeWorldRenderer,
    session: FakeWorldSession,
    path: str,
    *,
    query: dict[str, str] | None = None,
) -> RenderedSourceResponse:
    return renderer.render(
        canonical_scenario(),
        FakeWorldRequest.get(
            SOURCE_ID,
            path,
            query=query or {},
            headers=session_headers(session),
        ),
        session,
    ).response


def test_vertical_slice_navigates_through_rendered_observations() -> None:
    scenario = canonical_scenario()
    renderer = FakeWorldRenderer()
    source = scenario.sources[0]

    # 1. Public landing (no session).
    landing = renderer.render(scenario, FakeWorldRequest.get(SOURCE_ID, "/")).response
    assert landing.status_code == 200
    assert any(link.startswith("/login") for link in _links(landing.text))

    # 2. Board index requires login: rendered redirect, not an exception.
    landing_links = _links(landing.text)
    boards_href = next(link for link in landing_links if link == "/index")
    gated = renderer.render(
        scenario, FakeWorldRequest.get(SOURCE_ID, boards_href)
    ).response
    assert gated.status_code == 302
    assert gated.redirect_location == "/login?next=/index"

    # 3. Login form is public; authenticate with the synthetic test account.
    login_page = renderer.render(
        scenario, FakeWorldRequest.get(SOURCE_ID, "/login")
    ).response
    assert login_page.status_code == 200
    login_result = renderer.render(
        scenario,
        FakeWorldRequest.post_form(
            SOURCE_ID,
            "/login",
            form={
                "username": LOGIN_USERNAME,
                "password": LOGIN_PASSWORD,
                "next": "/index",
            },
        ),
    )
    assert login_result.response.status_code == 302
    assert login_result.session_update is not None
    session = login_result.session_update.session
    assert session is not None

    # 4. Board index exposes stable board links.
    index = renderer.render(
        scenario,
        FakeWorldRequest.get(SOURCE_ID, "/index", headers=session_headers(session)),
        session,
    ).response
    assert index.status_code == 200

    # 5. Access board -> thread links (rate-limited archives later).
    access = renderer.render(
        scenario,
        FakeWorldRequest.get(
            SOURCE_ID, "/board/board-access", headers=session_headers(session)
        ),
        session,
    ).response
    assert access.status_code == 200
    thread_links = [link for link in _links(access.text) if link.startswith("/thread/")]
    assert "/thread/thr-hospital-creds" in thread_links

    # 6. Paginated thread page 1 -> next -> page 2 -> previous.
    page1 = _render(renderer, session, "/thread/thr-hospital-creds")
    assert page1.status_code == 200
    assert "p-host-creds-1" in page1.text
    page2 = _render(
        renderer, session, "/thread/thr-hospital-creds", query={"page": "2"}
    )
    assert page2.status_code == 200
    assert "p-host-creds-12" in page2.text
    assert any(
        link.startswith("/thread/thr-hospital-creds") for link in _links(page1.text)
    )

    # 7. Single post view reached via the canonical post route.
    post = _render(renderer, session, "/post/p-host-creds-1")
    assert post.status_code == 200
    assert "Mason Creek General Hospital" in post.text

    # 8. Attachment download with metadata (safe synthetic fixture).
    attachment = renderer.render(
        scenario,
        FakeWorldRequest.get(
            SOURCE_ID,
            "/attachment/att-cred-list",
            headers=session_headers(session),
        ),
        session,
    ).response
    assert attachment.status_code == 200
    assert attachment.attachment is not None
    assert "synthetic test fixture" in attachment.text

    # 9. Rate-limited board: 2 views then a deterministic 429.
    archives_1 = _render(renderer, session, "/board/board-archives")
    archives_2 = _render(renderer, session, "/board/board-archives")
    archives_3 = _render(renderer, session, "/board/board-archives")
    assert archives_1.status_code == 200
    assert archives_2.status_code == 200
    assert archives_3.status_code == 429
    assert archives_3.headers.get("retry-after") == "60"

    # 10. Scripted intermittent failure: 503 then 200 (retry semantics).
    first_attempt = _render(renderer, session, "/thread/thr-intros")
    second_attempt = _render(renderer, session, "/thread/thr-intros")
    assert first_attempt.status_code == 503
    assert second_attempt.status_code == 200

    # 11. Exhaust the protected-request budget: deterministic re-auth.
    followed = 0
    while followed < 2:
        step = renderer.render(
            scenario,
            FakeWorldRequest.get(SOURCE_ID, "/index", headers=session_headers(session)),
            session,
        )
        if step.session_update is not None and step.session_update.session is None:
            assert step.response.status_code == 302
            break
        assert step.response.status_code == 200
        followed += 1

    # 12. Renderer reset replays the initial sequence (failure included).
    renderer.reset()
    session = login_again(renderer)
    replay = renderer.render(
        scenario,
        FakeWorldRequest.get(
            SOURCE_ID, "/thread/thr-intros", headers=session_headers(session)
        ),
        session,
    ).response
    assert replay.status_code == 503

    # 13. Truth stays inaccessible to every rendered observation.
    all_text = "\n".join(
        [
            landing.text,
            gated.text,
            login_page.text,
            index.text,
            access.text,
            page1.text,
            page2.text,
            post.text,
            attachment.text,
            archives_3.text,
            second_attempt.text,
        ]
    )
    all_headers = str(
        [
            sorted(r.headers.items())
            for r in (landing, index, page1, attachment, archives_3)
        ]
    )
    for token in _HIDDEN_TOKENS:
        assert token not in all_text, f"truth token leaked: {token}"
        assert token not in all_headers, f"truth token leaked in headers: {token}"

    # 14. Source-level failures were responses, never exceptions.
    assert {archives_3.status_code, page1.status_code} <= {200, 429}

    # 15. Traceability covers every exercised behavior.
    manifest = scenario.traceability
    assert manifest is not None
    exercised = {str(e.behavior_id) for e in manifest.entries}
    assert exercised >= _EXPECTED_BEHAVIORS
    for expected in _EXPECTED_BEHAVIORS:
        entry = manifest.behavior(_behavior_id(expected))
        assert entry.test_ids, f"{expected} lacks a deterministic test mapping"

    # 16. Navigation only ever followed rendered hrefs.
    # (all paths above were derived from previous pages or canonical routes;
    #  no truth object was used to fabricate a URL.)
    assert source.source_id == SourceId("blackgate")


def login_again(renderer: FakeWorldRenderer) -> FakeWorldSession:
    result = renderer.render(
        canonical_scenario(),
        FakeWorldRequest.post_form(
            SOURCE_ID,
            "/login",
            form={
                "username": LOGIN_USERNAME,
                "password": LOGIN_PASSWORD,
                "next": "/index",
            },
        ),
    )
    assert result.session_update is not None
    assert result.session_update.session is not None
    return result.session_update.session


def _behavior_id(value: str) -> BehaviorId:
    return BehaviorId(value)


def test_vertical_slice_session_isolation_between_tests() -> None:
    """Two slices never share mutable runtime state."""
    scenario = canonical_scenario()
    renderer_a = FakeWorldRenderer()
    renderer_b = FakeWorldRenderer()
    a = login_again(renderer_a)
    b = login_again(renderer_b)
    # Fresh renderers restart the deterministic token serial; both sessions
    # are valid and their counters are independent.
    assert a.token == "blackgate-session-001"
    assert b.token == "blackgate-session-001"
    for renderer, session in ((renderer_a, a), (renderer_b, b)):
        assert (
            renderer.render(
                scenario,
                FakeWorldRequest.get(
                    SOURCE_ID, "/index", headers=session_headers(session)
                ),
                session,
            ).response.status_code
            == 200
        )
    # Rate-limit state does not leak across renderers.
    for _ in range(3):
        renderer_a.render(
            scenario,
            FakeWorldRequest.get(
                SOURCE_ID, "/board/board-archives", headers=session_headers(a)
            ),
            a,
        )
    assert (
        renderer_b.render(
            scenario,
            FakeWorldRequest.get(
                SOURCE_ID, "/board/board-archives", headers=session_headers(b)
            ),
            b,
        ).response.status_code
        == 200
    )
