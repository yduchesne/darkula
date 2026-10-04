# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Fake World scenario and observable forum model (PR 6).

Defines the immutable scenario container and the *source-observable* forum
structure (boards, threads, posts, aliases, quotes, attachments, policy).
Observable objects are what a real collector could see; they never embed
hidden truth or expected Darkula answers.

Cross-references are validated at scenario construction so an invalid
scenario fails fast instead of surfacing during arbitrary later rendering.

Semantic identity (``BoardId``/``ThreadId``/``PostId``/...) is distinct from
presentation identity (rendered URLs, display names, HTML ids, filenames).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from darkula.testing.fake_world.identifiers import (
    ActorId,
    AliasId,
    AttachmentId,
    BoardId,
    EventId,
    FakeWorldValidationError,
    ForumAliasId,
    LocationId,
    OrganizationId,
    PostId,
    RelationshipId,
    ScenarioId,
    ScenarioVersion,
    SourceId,
    ThreadId,
    require_utc,
    split_ref,
)

if TYPE_CHECKING:
    from darkula.testing.fake_world.traceability import TraceabilityManifest
    from darkula.testing.fake_world.truth import FakeWorldTruth


class Visibility(StrEnum):
    """Smallest deterministic visibility vocabulary for v1.

    - ``PUBLIC`` — rendered without any session;
    - ``REGISTERED`` — rendered only for a valid, non-expired session;
    - ``AUTHENTICATED`` — v1 marker for the most sensitive areas
      (attachments); same session rule as ``REGISTERED`` but kept distinct
      so later archetypes can layer stronger checks without renames.
    """

    PUBLIC = "PUBLIC"
    REGISTERED = "REGISTERED"
    AUTHENTICATED = "AUTHENTICATED"


def _require_text(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FakeWorldValidationError(f"{field_name} must not be blank")


def _require_unique_id(ids: Iterable[object], field_name: str) -> None:
    seen: set[str] = set()
    for item in ids:
        key = str(item)
        if key in seen:
            raise FakeWorldValidationError(f"duplicate {field_name} identity: {key!r}")
        seen.add(key)


@dataclass(frozen=True, slots=True)
class ForumAlias:
    """One rendered forum alias with its observable reputation."""

    alias_id: ForumAliasId
    display_name: str
    reputation: int
    joined_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.display_name, field_name="alias display name")
        if not isinstance(self.reputation, int) or self.reputation < 0:
            raise FakeWorldValidationError(
                "alias reputation must be a non-negative integer"
            )
        require_utc(self.joined_at, field_name="alias joined_at")


@dataclass(frozen=True, slots=True)
class ForumQuote:
    """One deterministic quote of another post."""

    target_post_id: PostId
    snippet: str

    def __post_init__(self) -> None:
        _require_text(self.snippet, field_name="quote snippet")


@dataclass(frozen=True, slots=True)
class ForumAttachment:
    """One attachment exposed by a rendered post.

    ``body`` is the harmless synthetic fixture payload (or ``None`` for a
    metadata-only attachment). The renderer treats ``filename`` strictly as
    inert data and never uses it as a filesystem path.
    """

    attachment_id: AttachmentId
    post_id: PostId
    filename: str
    media_type: str
    body: bytes | None = None
    description: str = ""

    def __post_init__(self) -> None:
        _require_text(self.filename, field_name="attachment filename")
        _require_text(self.media_type, field_name="attachment media type")
        if self.body is not None and not isinstance(self.body, bytes):
            raise FakeWorldValidationError("attachment body must be bytes or None")


@dataclass(frozen=True, slots=True)
class ForumPost:
    """One observable post.

    Deleted posts keep their content in the model (the fictional world knows
    what was written) but the renderer exposes only a deterministic
    unavailable placeholder.
    """

    post_id: PostId
    thread_id: ThreadId
    author: ForumAliasId
    created_at: datetime
    content: str
    visibility: Visibility = Visibility.PUBLIC
    edited_at: datetime | None = None
    edited_note: str = ""
    deleted: bool = False
    quote: ForumQuote | None = None
    attachment_ids: tuple[AttachmentId, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.content, field_name="post content")
        require_utc(self.created_at, field_name="post created_at")
        if self.edited_at is not None:
            require_utc(self.edited_at, field_name="post edited_at")
            if self.edited_at < self.created_at:
                raise FakeWorldValidationError(
                    "post edited_at must not precede created_at"
                )
        if self.quote is not None and self.quote.target_post_id == self.post_id:
            raise FakeWorldValidationError("a post must not quote itself")


@dataclass(frozen=True, slots=True)
class ThreadFailureSchedule:
    """Deterministic scripted failure for one thread route.

    The first ``failing_requests`` requests return ``status_code`` (for
    example 503), later requests succeed. Counters are local to one renderer
    instance so tests stay isolated and replayable after ``reset()``.
    """

    status_code: int
    failing_requests: int = 1

    def __post_init__(self) -> None:
        if self.status_code < 500 or self.status_code > 599:
            raise FakeWorldValidationError(
                "scripted failure status must be a 5xx response"
            )
        if self.failing_requests < 1:
            raise FakeWorldValidationError(
                "failing_requests must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class BoardRateLimit:
    """Deterministic rate-limit policy for one board route.

    The first ``max_views_per_session`` views within one session succeed;
    every later view returns 429 with the fixed ``retry_after_seconds``.
    No wall clock is involved.
    """

    max_views_per_session: int
    retry_after_seconds: int

    def __post_init__(self) -> None:
        if self.max_views_per_session < 1:
            raise FakeWorldValidationError(
                "rate limit max_views_per_session must be a positive integer"
            )
        if self.retry_after_seconds < 0:
            raise FakeWorldValidationError(
                "rate limit retry_after_seconds must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class ForumThread:
    """One observable thread with fixed, chronologically ordered posts."""

    thread_id: ThreadId
    board_id: BoardId
    title: str
    visibility: Visibility
    created_at: datetime
    posts: tuple[ForumPost, ...]
    failure_schedule: ThreadFailureSchedule | None = None

    def __post_init__(self) -> None:
        _require_text(self.title, field_name="thread title")
        require_utc(self.created_at, field_name="thread created_at")
        if not self.posts:
            raise FakeWorldValidationError("thread must contain at least one post")
        _require_unique_id([p.post_id for p in self.posts], "post")
        previous: datetime | None = None
        for post in self.posts:
            if previous is not None and post.created_at < previous:
                raise FakeWorldValidationError(
                    f"thread {self.thread_id} posts must be in chronological "
                    "creation order"
                )
            previous = post.created_at


@dataclass(frozen=True, slots=True)
class ForumBoard:
    """One observable board exposing ordered threads."""

    board_id: BoardId
    name: str
    description: str
    visibility: Visibility
    threads: tuple[ForumThread, ...]
    rate_limit: BoardRateLimit | None = None

    def __post_init__(self) -> None:
        _require_text(self.name, field_name="board name")
        _require_text(self.description, field_name="board description")
        _require_unique_id([t.thread_id for t in self.threads], "thread")


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Login/session/pagination policy of one fictional source.

    ``login_username`` / ``login_password`` are synthetic, clearly test-only
    credentials safe for repository inclusion; they never appear in any
    production Darkula configuration.
    """

    login_username: str
    login_password: str
    posts_per_page: int
    session_max_protected_requests: int
    invite_only_message: str = (
        "Registration is invite-only. Existing members can invite trusted "
        "contacts through the referral system."
    )

    def __post_init__(self) -> None:
        _require_text(self.login_username, field_name="login username")
        _require_text(self.login_password, field_name="login password")
        if self.posts_per_page < 1:
            raise FakeWorldValidationError("posts_per_page must be a positive integer")
        if self.session_max_protected_requests < 1:
            raise FakeWorldValidationError(
                "session_max_protected_requests must be a positive integer"
            )
        _require_text(self.invite_only_message, field_name="invite-only message")


@dataclass(frozen=True, slots=True)
class ForumSource:
    """One fictional source (for example the BlackGate forum).

    ``attachments`` is the canonical attachment registry of the source;
    posts reference attachments by stable identity.
    """

    source_id: SourceId
    title: str
    description: str
    base_domain: str
    boards: tuple[ForumBoard, ...]
    aliases: tuple[ForumAlias, ...]
    policy: SessionPolicy
    routes: tuple[str, ...]
    attachments: tuple[ForumAttachment, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.title, field_name="source title")
        _require_text(self.base_domain, field_name="source base domain")
        _require_unique_id([b.board_id for b in self.boards], "board")
        _require_unique_id([a.alias_id for a in self.aliases], "forum alias")
        _require_unique_id([a.attachment_id for a in self.attachments], "attachment")
        if not self.routes:
            raise FakeWorldValidationError(
                "source must declare the static routes its renderer implements"
            )


@dataclass(frozen=True, slots=True)
class FakeWorldScenario:
    """One immutable scenario version: identity, sources, truth, manifest."""

    scenario_id: ScenarioId
    scenario_version: ScenarioVersion
    title: str
    description: str
    sources: tuple[ForumSource, ...]
    truth: FakeWorldTruth
    traceability: TraceabilityManifest | None
    authored_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.title, field_name="scenario title")
        require_utc(self.authored_at, field_name="scenario authored_at")
        if not self.sources:
            raise FakeWorldValidationError("scenario must contain a source")
        if self.truth.scenario_id != self.scenario_id:
            raise FakeWorldValidationError(
                "truth scenario_id must equal the scenario_id"
            )
        if self.truth.scenario_version.value != self.scenario_version.value:
            raise FakeWorldValidationError(
                "truth scenario_version must equal the scenario_version"
            )
        _validate_scenario(self)

    # -- lookup helpers --------------------------------------------------
    def source(self, source_id: SourceId) -> ForumSource:
        """Return the source with the given identity or fail closed."""
        for source in self.sources:
            if source.source_id == source_id:
                return source
        raise FakeWorldValidationError(f"unknown source: {str(source_id)!r}")

    def find_board(self, board_id_str: str) -> ForumBoard:
        """Return one board across the scenario, or fail closed."""
        for board in self._all_boards():
            if board.board_id.value == board_id_str:
                return board
        raise FakeWorldValidationError(f"unknown board: {board_id_str!r}")

    def find_thread(self, thread_id_str: str) -> ForumThread:
        """Return one thread across the scenario, or fail closed."""
        for thread in self._all_threads():
            if thread.thread_id.value == thread_id_str:
                return thread
        raise FakeWorldValidationError(f"unknown thread: {thread_id_str!r}")

    def find_post(self, post_id_str: str) -> ForumPost:
        """Return one post across the scenario, or fail closed."""
        for post in self._all_posts():
            if post.post_id.value == post_id_str:
                return post
        raise FakeWorldValidationError(f"unknown post: {post_id_str!r}")

    def find_alias(self, alias_id_str: str) -> ForumAlias:
        """Return one forum alias across the scenario, or fail closed."""
        for alias in self._all_aliases():
            if alias.alias_id.value == alias_id_str:
                return alias
        raise FakeWorldValidationError(f"unknown forum alias: {alias_id_str!r}")

    def find_attachment(self, attachment_id_str: str) -> ForumAttachment:
        """Return one attachment across the scenario, or fail closed."""
        for attachment in self._all_attachments():
            if attachment.attachment_id.value == attachment_id_str:
                return attachment
        raise FakeWorldValidationError(f"unknown attachment: {attachment_id_str!r}")

    def _all_boards(self) -> tuple[ForumBoard, ...]:
        return tuple(board for source in self.sources for board in source.boards)

    def _all_threads(self) -> tuple[ForumThread, ...]:
        return tuple(
            thread
            for source in self.sources
            for board in source.boards
            for thread in board.threads
        )

    def _all_posts(self) -> tuple[ForumPost, ...]:
        return tuple(
            post
            for source in self.sources
            for board in source.boards
            for thread in board.threads
            for post in thread.posts
        )

    def _all_aliases(self) -> tuple[ForumAlias, ...]:
        return tuple(alias for source in self.sources for alias in source.aliases)

    def _all_attachments(self) -> tuple[ForumAttachment, ...]:
        return tuple(
            attachment for source in self.sources for attachment in source.attachments
        )


#: Truth relationship reference kinds that resolve to forum objects.
_FORUM_RELATIONSHIP_KINDS = frozenset(
    {"board", "thread", "post", "attachment", "alias"}
)

#: Truth relationship reference kinds that resolve inside the truth model.
_TRUTH_RELATIONSHIP_KINDS = frozenset(
    {"actor", "alias", "organization", "location", "relationship", "event"}
)


def _validate_scenario(scenario: FakeWorldScenario) -> None:
    """Validate every cross-reference at scenario construction.

    Post/thread/board/alias/quote/attachment refs are checked here; truth
    relationships spanning both worlds are resolved; the traceability
    manifest must reference only existing routes/objects.
    """
    _require_unique_id([source.source_id for source in scenario.sources], "source")

    for source in scenario.sources:
        source_alias_ids = {str(a.alias_id) for a in source.aliases}
        source_attachment_ids = {str(a.attachment_id) for a in source.attachments}
        for board in source.boards:
            for thread in board.threads:
                if str(thread.board_id) != str(board.board_id):
                    raise FakeWorldValidationError(
                        f"thread {thread.thread_id} references board "
                        f"{thread.board_id} which does not belong to board "
                        f"{board.board_id}"
                    )
                post_ids: set[str] = set()
                for post in thread.posts:
                    if str(post.thread_id) != str(thread.thread_id):
                        raise FakeWorldValidationError(
                            f"post {post.post_id} references thread "
                            f"{post.thread_id} which does not contain it"
                        )
                    if str(post.author) not in source_alias_ids:
                        raise FakeWorldValidationError(
                            f"post {post.post_id} references unknown forum "
                            f"alias {post.author}"
                        )
                    if str(post.post_id) in post_ids:
                        raise FakeWorldValidationError(
                            f"duplicate post identity: {post.post_id!r}"
                        )
                    post_ids.add(str(post.post_id))
                    if post.quote is not None:
                        scenario.find_post(post.quote.target_post_id.value)
                    for attachment_id in post.attachment_ids:
                        if str(attachment_id) not in source_attachment_ids:
                            raise FakeWorldValidationError(
                                f"post {post.post_id} references unknown "
                                f"attachment {attachment_id}"
                            )
                        attachment = scenario.find_attachment(attachment_id.value)
                        if str(attachment.post_id) != str(post.post_id):
                            raise FakeWorldValidationError(
                                f"attachment {attachment.attachment_id} is "
                                f"listed on post {post.post_id} but owned by "
                                f"post {attachment.post_id}"
                            )
    _validate_truth_relationships(scenario)
    if scenario.traceability is not None:
        scenario.traceability.validate_refs(scenario)


def _validate_truth_relationships(scenario: FakeWorldScenario) -> None:
    """Resolve every truth relationship reference, both worlds."""
    truth = scenario.truth
    for relationship in truth.relationships:
        for ref in (relationship.source_ref, relationship.target_ref):
            kind, value = split_ref(ref)
            if kind in _TRUTH_RELATIONSHIP_KINDS:
                if kind == "actor":
                    truth.actor(ActorId(value))
                elif kind == "alias":
                    truth.alias(AliasId(value))
                elif kind == "organization":
                    truth.organization(OrganizationId(value))
                elif kind == "location":
                    truth.location(LocationId(value))
                elif kind == "relationship":
                    truth.relationship(RelationshipId(value))
                elif kind == "event":
                    truth.event(EventId(value))
            elif kind in _FORUM_RELATIONSHIP_KINDS:
                if kind == "board":
                    scenario.find_board(value)
                elif kind == "thread":
                    scenario.find_thread(value)
                elif kind == "post":
                    scenario.find_post(value)
                elif kind == "attachment":
                    scenario.find_attachment(value)
                elif kind == "alias":
                    scenario.find_alias(value)
            else:
                raise FakeWorldValidationError(
                    f"relationship {relationship.relationship_id} references "
                    f"unknown kind {kind!r} in {ref!r}"
                )


__all__ = [
    "BoardRateLimit",
    "FakeWorldScenario",
    "ForumAlias",
    "ForumAttachment",
    "ForumBoard",
    "ForumPost",
    "ForumQuote",
    "ForumSource",
    "ForumThread",
    "SessionPolicy",
    "ThreadFailureSchedule",
    "Visibility",
]
