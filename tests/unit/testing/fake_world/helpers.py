# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared helpers for Fake World tests (PR 6).

Canonical scenario access, login helpers, and small scenario builders used by
the validation-failure tests (FW5/FW6/FW7).

The canonical scenario is immutable and therefore safe to share across tests;
renderer/session state is always created fresh per test.
"""

from __future__ import annotations

from datetime import UTC, datetime

from darkula.testing.fake_world import (
    FakeWorldRenderer,
    FakeWorldRequest,
    FakeWorldScenario,
    FakeWorldSession,
    FakeWorldTruth,
    ForumAlias,
    ForumAttachment,
    ForumBoard,
    ForumPost,
    ForumSource,
    ForumThread,
    RenderedSourceResponse,
    SessionPolicy,
    TruthActor,
    TruthAlias,
    TruthLocation,
    Visibility,
)
from darkula.testing.fake_world.identifiers import (
    ActorId,
    AliasId,
    AttachmentId,
    BoardId,
    ForumAliasId,
    LocationId,
    PostId,
    ScenarioId,
    ScenarioVersion,
    SourceId,
    ThreadId,
)

SCENARIO_ID = ScenarioId("blackgate-core")
SCENARIO_VERSION = ScenarioVersion(1)
SOURCE_ID = SourceId("blackgate")
LOGIN_USERNAME = "zerofox77"
LOGIN_PASSWORD = "blackgate-test-password"

#: Fixed canonical instant for custom scenarios and posts.
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def canonical_scenario() -> FakeWorldScenario:
    """Return the shared immutable canonical BlackGate core v1 scenario."""
    from darkula.testing.fake_world.registry import get_scenario

    return get_scenario(SCENARIO_ID.value, version=SCENARIO_VERSION.value)


def fresh_renderer() -> FakeWorldRenderer:
    """Return a fresh renderer with canonical initial runtime state."""
    return FakeWorldRenderer()


def session_headers(session: FakeWorldSession) -> dict[str, str]:
    """Return deterministic cookie headers for an established session."""
    return {"cookie": f"blackgate_session={session.token}"}


def login(renderer: FakeWorldRenderer, scenario: FakeWorldScenario) -> FakeWorldSession:
    """Log in with the synthetic test account and return the session."""
    result = renderer.render(
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
    assert result.session_update is not None
    assert result.session_update.session is not None
    return result.session_update.session


def get(
    renderer: FakeWorldRenderer,
    scenario: FakeWorldScenario,
    path: str,
    *,
    session: FakeWorldSession | None = None,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> RenderedSourceResponse:
    """Render one deterministic GET and return the response."""
    return renderer.render(
        scenario,
        FakeWorldRequest.get(
            SOURCE_ID,
            path,
            query=query or {},
            headers=headers
            if headers is not None
            else session_headers(session)
            if session
            else {},
        ),
        session,
    ).response


# -- minimal custom-scenario builders (validation-failure tests) ----------
def make_truth(
    scenario_id: ScenarioId = SCENARIO_ID,
    version: ScenarioVersion = SCENARIO_VERSION,
) -> FakeWorldTruth:
    """Return a minimal valid truth for custom scenarios."""
    return FakeWorldTruth(
        scenario_id=scenario_id,
        scenario_version=version,
        actors=(TruthActor(actor_id=ActorId("actor-001"), primary_alias="alice"),),
        aliases=(
            TruthAlias(
                alias_id=AliasId("alias-001"),
                actor_id=ActorId("actor-001"),
                alias="alice",
            ),
        ),
        locations=(
            TruthLocation(
                location_id=LocationId("location-001"),
                name="Testville",
                region="Test Region",
                country="Testland",
            ),
        ),
    )


def make_alias(alias_id: str = "al-alice", name: str = "alice") -> ForumAlias:
    return ForumAlias(
        alias_id=ForumAliasId(alias_id),
        display_name=name,
        reputation=5,
        joined_at=T0,
    )


def make_post(
    post_id: str,
    *,
    thread_id: str = "thr-one",
    author: str = "al-alice",
    content: str = "hello",
    created_at: datetime = T0,
    visibility: Visibility = Visibility.PUBLIC,
    attachment_ids: tuple[AttachmentId, ...] = (),
) -> ForumPost:
    return ForumPost(
        post_id=PostId(post_id),
        thread_id=ThreadId(thread_id),
        author=ForumAliasId(author),
        created_at=created_at,
        content=content,
        visibility=visibility,
        attachment_ids=attachment_ids,
    )


def make_thread(
    thread_id: str = "thr-one",
    *,
    board_id: str = "board-a",
    title: str = "Thread",
    visibility: Visibility = Visibility.PUBLIC,
    posts: tuple[ForumPost, ...] = (),
    created_at: datetime = T0,
) -> ForumThread:
    return ForumThread(
        thread_id=ThreadId(thread_id),
        board_id=BoardId(board_id),
        title=title,
        visibility=visibility,
        created_at=created_at,
        posts=posts or (make_post("p-1", thread_id=thread_id),),
    )


def make_board(
    board_id: str = "board-a",
    *,
    visibility: Visibility = Visibility.PUBLIC,
    threads: tuple[ForumThread, ...] = (),
) -> ForumBoard:
    return ForumBoard(
        board_id=BoardId(board_id),
        name=f"Board {board_id}",
        description="Custom test board",
        visibility=visibility,
        threads=threads,
    )


def make_attachment(
    attachment_id: str = "att-a",
    *,
    post_id: str = "p-1",
    filename: str = "notes.txt",
) -> ForumAttachment:
    return ForumAttachment(
        attachment_id=AttachmentId(attachment_id),
        post_id=PostId(post_id),
        filename=filename,
        media_type="text/plain",
        body=b"fixture",
    )


def make_source(
    *,
    boards: tuple[ForumBoard, ...] = (),
    aliases: tuple[ForumAlias, ...] = (),
    attachments: tuple[ForumAttachment, ...] = (),
) -> ForumSource:
    """Return a minimal forum source with deterministic defaults."""
    return ForumSource(
        source_id=SOURCE_ID,
        title="Test Forum",
        description="Minimal custom test source",
        base_domain="test.example.test",
        boards=boards or (make_board(board_id="board-a"),),
        aliases=aliases or (make_alias(),),
        policy=SessionPolicy(
            login_username=LOGIN_USERNAME,
            login_password=LOGIN_PASSWORD,
            posts_per_page=10,
            session_max_protected_requests=12,
        ),
        routes=("/", "/login", "/register", "/logout", "/index"),
        attachments=attachments,
    )


def make_scenario(
    source: ForumSource | None = None,
    *,
    truth: FakeWorldTruth | None = None,
    scenario_id: ScenarioId = SCENARIO_ID,
    version: ScenarioVersion = SCENARIO_VERSION,
) -> FakeWorldScenario:
    """Return a minimal custom scenario (cross-refs validated)."""
    return FakeWorldScenario(
        scenario_id=scenario_id,
        scenario_version=version,
        title="Custom test scenario",
        description="Custom scenario built inside Fake World tests",
        sources=(source or make_source(),),
        truth=truth or make_truth(scenario_id, version),
        traceability=None,
        authored_at=T0,
    )
