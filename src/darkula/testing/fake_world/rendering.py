# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic Fake World source renderer (PR 6).

The renderer is the external-world test boundary: it turns immutable scenario
state plus a transport-light request into a ``RenderedSourceResponse`` using
no network, no filesystem mutation, no wall clock, and no hidden global state.

Rules frozen here:

- identical ``(scenario, request, session)`` always produce the identical
  response;
- source-level failures (401/404/405/429/503/redirect) are returned as
  responses, never raised as exceptions;
- scenario/programming errors (unknown source, invalid request) raise a
  typed ``FakeWorldValidationError``;
- all runtime counters (login serial, scripted failures, session expiry,
  rate-limit views) live on the renderer instance and restart with
  ``reset()``, isolating tests from each other;
- rendered output is hostile/untrusted input by convention even though it is
  generated locally, and never contains truth-ontology details.

Rendered URLs, page numbers, display names, HTML ids, and filenames are
presentation identity, not semantic identity.
"""

from __future__ import annotations

import hashlib
import html as _html
import math
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Self

from darkula.testing.fake_world.identifiers import (
    AttachmentId,
    FakeWorldValidationError,
    SourceId,
)
from darkula.testing.fake_world.model import (
    FakeWorldScenario,
    ForumAttachment,
    ForumBoard,
    ForumPost,
    ForumSource,
    ForumThread,
    Visibility,
)

#: Name of the deterministic session cookie rendered by BlackGate.
_SESSION_COOKIE = "blackgate_session"

#: Header-safe characters allowed verbatim in RFC 5987 filenames.
_SAFE_HEADER_CHARS = "-._~"

_HTML = "text/html; charset=utf-8"
_PLAIN = "text/plain; charset=utf-8"


class HttpMethod(StrEnum):
    """Smallest deterministic HTTP-like method vocabulary."""

    GET = "GET"
    POST = "POST"


class FrozenParams:
    """Immutable, key-lowercased, deterministically ordered parameter map.

    Duplicate keys collapse last-wins (dict semantics); the resulting pairs
    are sorted by (key, value) so ordering is fully deterministic.
    """

    __slots__ = ("_pairs",)

    def __init__(self, pairs: Iterable[tuple[str, str]]) -> None:
        merged: dict[str, str] = {}
        for key, value in pairs:
            merged[key.lower()] = value
        self._pairs = tuple(sorted(merged.items()))

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, str]) -> Self:
        """Build from a mapping, normalizing key case and ordering."""
        return cls(mapping.items())

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return the first value for a case-insensitive key."""
        needle = key.lower()
        for pair_key, value in self._pairs:
            if pair_key == needle:
                return value
        return default

    def items(self) -> tuple[tuple[str, str], ...]:
        """Return all (key, value) pairs in canonical order."""
        return self._pairs

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key) is not None

    def __len__(self) -> int:
        return len(self._pairs)

    def __iter__(self) -> Iterator[str]:
        for pair_key, _ in self._pairs:
            yield pair_key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FrozenParams):
            return NotImplemented
        return self._pairs == other._pairs

    def __hash__(self) -> int:
        return hash(self._pairs)


@dataclass(frozen=True, slots=True)
class FakeWorldRequest:
    """Transport-light deterministic request.

    ``query`` and ``headers`` are normalized (lowercased keys, sorted pairs).
    ``body`` is used only for login form submission.
    """

    source_id: SourceId
    method: HttpMethod
    path: str
    query: FrozenParams = field(default_factory=lambda: FrozenParams(()))
    headers: FrozenParams = field(default_factory=lambda: FrozenParams(()))
    body: bytes | None = None

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise FakeWorldValidationError("request path must start with '/'")
        if any(ord(ch) < 32 for ch in self.path):
            raise FakeWorldValidationError(
                "request path must not contain control characters"
            )

    @classmethod
    def get(
        cls,
        source_id: SourceId,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Self:
        """Build a deterministic GET request."""
        return cls(
            source_id=source_id,
            method=HttpMethod.GET,
            path=path,
            query=FrozenParams.from_mapping(query or {}),
            headers=FrozenParams.from_mapping(headers or {}),
        )

    @classmethod
    def post_form(
        cls,
        source_id: SourceId,
        path: str,
        *,
        form: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
    ) -> Self:
        """Build a deterministic URL-encoded form POST (login)."""
        payload = urllib.parse.urlencode(form).encode("utf-8")
        merged_headers = dict(headers or {})
        merged_headers.setdefault("content-type", "application/x-www-form-urlencoded")
        return cls(
            source_id=source_id,
            method=HttpMethod.POST,
            path=path,
            query=FrozenParams.from_mapping(query or {}),
            headers=FrozenParams.from_mapping(merged_headers),
            body=payload,
        )


@dataclass(frozen=True, slots=True)
class AttachmentDescriptor:
    """Semantic metadata of one rendered attachment response."""

    attachment_id: AttachmentId
    filename: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RenderedSourceResponse:
    """Deterministic response of one Fake World render."""

    status_code: int
    headers: FrozenParams
    media_type: str
    body: bytes
    redirect_location: str | None = None
    attachment: AttachmentDescriptor | None = None

    @property
    def text(self) -> str:
        """Decode the response body as UTF-8 text."""
        return self.body.decode("utf-8")


@dataclass(frozen=True, slots=True)
class FakeWorldSession:
    """Explicit deterministic session state.

    Returned to the caller through ``RenderResult.session_update``; never
    mutated globally.
    """

    token: str
    alias: str

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise FakeWorldValidationError("session token must not be blank")
        if not self.alias.strip():
            raise FakeWorldValidationError("session alias must not be blank")


@dataclass(frozen=True, slots=True)
class SessionUpdate:
    """Explicit successor session state (``None`` logs the caller out)."""

    session: FakeWorldSession | None = None


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Renderer outcome: response plus any explicit session transition."""

    response: RenderedSourceResponse
    session_update: SessionUpdate | None = None


def _escape(text: str) -> str:
    """Escape user/hostile content for HTML output."""
    return _html.escape(text, quote=True)


def _format_utc(value: datetime) -> str:
    """Render a fixed UTC timestamp deterministically."""
    return value.strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def _page(title: str, body: str) -> str:
    """Assemble one full deterministic HTML page."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{_escape(title)}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "<footer><p>BlackGate &copy; specimen forum</p></footer>\n"
        "</body>\n"
        "</html>\n"
    )


def _parse_page(raw: str | None) -> int | None:
    """Parse a thread page query value; invalid input is source-level 404."""
    if raw is None:
        return 1
    if not raw.isascii() or not raw.isdigit():
        return None
    return int(raw)


def _safe_next(raw: str | None) -> str:
    """Restrict login redirect targets to same-origin-like paths."""
    if raw is None or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def _login_url(path: str) -> str:
    return "/login?next=" + urllib.parse.quote(path, safe="/-_.~")


def _cookie_token(request: FakeWorldRequest) -> str | None:
    """Extract the deterministic session token from the cookie header."""
    cookie = request.headers.get("cookie")
    if cookie is None:
        return None
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip() == _SESSION_COOKIE:
            return value.strip()
    return None


def _attachment_header_name(filename: str) -> str:
    """Percent-encode an attachment filename for a header (inert data)."""
    return urllib.parse.quote(filename, safe=_SAFE_HEADER_CHARS)


def _parse_form(request: FakeWorldRequest) -> dict[str, str]:
    """Parse a URL-encoded login form deterministically (or fail closed)."""
    if request.body is None or request.body == b"":
        return {}
    try:
        text = request.body.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    return dict(urllib.parse.parse_qsl(text, keep_blank_values=True))


def _redirect(
    location: str,
    *,
    extra_headers: Mapping[str, str] | None = None,
    status: int = 302,
) -> RenderedSourceResponse:
    """Build a deterministic redirect response."""
    payload = f"Redirecting to {_escape(location)}".encode()
    headers = {
        "location": location,
        "content-type": _HTML,
        "content-length": str(len(payload)),
    }
    headers.update(extra_headers or {})
    return RenderedSourceResponse(
        status_code=status,
        headers=FrozenParams.from_mapping(headers),
        media_type=_HTML,
        body=payload,
        redirect_location=location,
    )


def _find_board_or_404(scenario: FakeWorldScenario, board_id: str) -> ForumBoard | None:
    """Look up one board; unknown ids become source-level 404."""
    try:
        return scenario.find_board(board_id)
    except FakeWorldValidationError:
        return None


def _find_thread_or_404(
    scenario: FakeWorldScenario, thread_id: str
) -> ForumThread | None:
    """Look up one thread; unknown ids become source-level 404."""
    try:
        return scenario.find_thread(thread_id)
    except FakeWorldValidationError:
        return None


def _find_post_or_404(scenario: FakeWorldScenario, post_id: str) -> ForumPost | None:
    """Look up one post; unknown ids become source-level 404."""
    try:
        return scenario.find_post(post_id)
    except FakeWorldValidationError:
        return None


def _find_attachment_or_404(
    scenario: FakeWorldScenario, attachment_id: str
) -> ForumAttachment | None:
    """Look up one attachment; unknown ids become source-level 404."""
    try:
        return scenario.find_attachment(attachment_id)
    except FakeWorldValidationError:
        return None


class FakeWorldRenderer:
    """Deterministic, offline, resettable Fake World source renderer.

    One renderer owns all mutable runtime state (login serial, protected-
    request counters, scripted failures, rate-limit views). Scenario
    definitions are immutable and may be shared; create a fresh renderer per
    test and call ``reset()`` to replay an identical sequence.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Restore canonical initial runtime state (fresh test isolation)."""
        self._session_counts: dict[str, int] = {}
        self._rate_counts: dict[str, int] = {}
        self._attempts: dict[str, int] = {}
        self._login_serial = 0

    def render(
        self,
        scenario: FakeWorldScenario,
        request: FakeWorldRequest,
        session: FakeWorldSession | None = None,
    ) -> RenderResult:
        """Render one deterministic response for the request."""
        source = scenario.source(request.source_id)
        token = self._presented_token(request, session)
        path = request.path
        method = request.method

        if path == "/":
            return RenderResult(
                self._ok_html("BlackGate", _landing_body(scenario, source))
            )
        if path == "/login":
            if method == HttpMethod.GET:
                return RenderResult(
                    self._ok_html("Log in — BlackGate", _login_body(source))
                )
            if method == HttpMethod.POST:
                return self._login_submit(source, request)
            return RenderResult(self._method_not_allowed())
        if method != HttpMethod.GET:
            return RenderResult(self._method_not_allowed())
        if path == "/register":
            return RenderResult(
                self._ok_html("Register — BlackGate", _register_body(source))
            )
        if path == "/logout":
            return RenderResult(_redirect("/"), SessionUpdate(None))
        if path.startswith("/legacy/"):
            return self._render_legacy(scenario, path)
        if path == "/index":
            gate = self._page_gate(source, session, token, path)
            if gate is not None:
                return gate
            return RenderResult(
                self._ok_html(
                    "Board index — BlackGate", _board_index_body(scenario, source)
                )
            )
        if path.startswith("/board/"):
            return self._render_board(scenario, source, path, session, token)
        if path.startswith("/thread/"):
            return self._render_thread(scenario, source, path, request, session, token)
        if path.startswith("/post/"):
            return self._render_post(scenario, source, path, session, token)
        if path.startswith("/attachment/"):
            return self._render_attachment(scenario, source, path, session, token)
        if path.startswith("/old-thread/"):
            return self._render_old_thread(scenario, path)
        return RenderResult(self._not_found())

    # -- authentication/session ------------------------------------------
    def _presented_token(
        self,
        request: FakeWorldRequest,
        session: FakeWorldSession | None,
    ) -> str | None:
        if session is None:
            return None
        cookie_token = _cookie_token(request)
        if cookie_token is not None and cookie_token != session.token:
            return None
        return session.token

    def _login_submit(
        self, source: ForumSource, request: FakeWorldRequest
    ) -> RenderResult:
        fields = _parse_form(request)
        username = fields.get("username", "")
        password = fields.get("password", "")
        policy = source.policy
        if username == policy.login_username and password == policy.login_password:
            self._login_serial += 1
            token = f"blackgate-session-{self._login_serial:03d}"
            target = _safe_next(fields.get("next"))
            session = FakeWorldSession(token=token, alias=policy.login_username)
            response = _redirect(
                target,
                extra_headers={
                    "set-cookie": f"{_SESSION_COOKIE}={token}; Path=/; HttpOnly"
                },
            )
            return RenderResult(response, SessionUpdate(session))
        return RenderResult(
            self._ok_html(
                "Log in — BlackGate",
                _login_body(source, error="Invalid username or password."),
            )
        )

    def _page_gate(
        self,
        source: ForumSource,
        session: FakeWorldSession | None,
        token: str | None,
        path: str,
    ) -> RenderResult | None:
        """Gate a protected page; returns a redirect/text RenderResult."""
        if session is None or token is None:
            return RenderResult(_redirect(_login_url(path)))
        used = self._session_counts.get(token, 0) + 1
        if used > source.policy.session_max_protected_requests:
            self._session_counts[token] = used
            return RenderResult(_redirect(_login_url(path)), SessionUpdate(None))
        self._session_counts[token] = used
        return None

    def _attachment_gate(
        self,
        source: ForumSource,
        session: FakeWorldSession | None,
        token: str | None,
    ) -> RenderResult | None:
        """Gate attachments: 401 without a session, re-auth when expired."""
        if session is None or token is None:
            return RenderResult(
                self._plain(
                    401,
                    "Authentication required to download attachments.",
                    {"www-authenticate": 'Basic realm="BlackGate"'},
                )
            )
        used = self._session_counts.get(token, 0) + 1
        if used > source.policy.session_max_protected_requests:
            self._session_counts[token] = used
            return RenderResult(
                self._plain(401, "Session expired. Please log in again."),
                SessionUpdate(None),
            )
        self._session_counts[token] = used
        return None

    # -- route handlers ----------------------------------------------------
    def _render_legacy(self, scenario: FakeWorldScenario, path: str) -> RenderResult:
        board_id = path[len("/legacy/") :]
        if not board_id or "/" in board_id:
            return RenderResult(self._not_found())
        board = _find_board_or_404(scenario, board_id)
        if board is None:
            return RenderResult(self._not_found())
        if board.visibility is not Visibility.PUBLIC:
            return RenderResult(_redirect(_login_url(path)))
        return RenderResult(
            self._ok_html("Legacy announcements — BlackGate", _legacy_body(board))
        )

    def _render_board(
        self,
        scenario: FakeWorldScenario,
        source: ForumSource,
        path: str,
        session: FakeWorldSession | None,
        token: str | None,
    ) -> RenderResult:
        board_id = path[len("/board/") :]
        if not board_id or "/" in board_id:
            return RenderResult(self._not_found())
        board = _find_board_or_404(scenario, board_id)
        if board is None:
            return RenderResult(self._not_found())
        if board.visibility is not Visibility.PUBLIC:
            gate = self._page_gate(source, session, token, path)
            if gate is not None:
                return gate
        limited = self._rate_limit_check(board, token)
        if limited is not None:
            return RenderResult(limited)
        return RenderResult(
            self._ok_html(
                f"{board.name} — BlackGate",
                _board_page_body(scenario, board),
            )
        )

    def _render_thread(
        self,
        scenario: FakeWorldScenario,
        source: ForumSource,
        path: str,
        request: FakeWorldRequest,
        session: FakeWorldSession | None,
        token: str | None,
    ) -> RenderResult:
        thread_id = path[len("/thread/") :]
        if not thread_id or "/" in thread_id:
            return RenderResult(self._not_found())
        thread = _find_thread_or_404(scenario, thread_id)
        if thread is None:
            return RenderResult(self._not_found())
        if thread.visibility is not Visibility.PUBLIC:
            gate = self._page_gate(source, session, token, path)
            if gate is not None:
                return gate
        failure = self._failure_check(thread, path)
        if failure is not None:
            return RenderResult(failure)
        page = _parse_page(request.query.get("page"))
        per_page = source.policy.posts_per_page
        total_pages = max(1, math.ceil(len(thread.posts) / per_page))
        if page is None or page < 1 or page > total_pages:
            return RenderResult(self._not_found())
        return RenderResult(
            self._ok_html(
                f"{thread.title} — BlackGate",
                _thread_page_body(scenario, thread, page, per_page),
            )
        )

    def _render_post(
        self,
        scenario: FakeWorldScenario,
        source: ForumSource,
        path: str,
        session: FakeWorldSession | None,
        token: str | None,
    ) -> RenderResult:
        post_id = path[len("/post/") :]
        if not post_id or "/" in post_id:
            return RenderResult(self._not_found())
        post = _find_post_or_404(scenario, post_id)
        if post is None:
            return RenderResult(self._not_found())
        thread = scenario.find_thread(post.thread_id.value)
        if thread.visibility is not Visibility.PUBLIC:
            gate = self._page_gate(source, session, token, path)
            if gate is not None:
                return gate
        per_page = source.policy.posts_per_page
        page = _page_of_post(thread, post, per_page)
        body = _post_page_body(scenario, thread, post, page, per_page)
        return RenderResult(self._ok_html(f"{thread.title} — BlackGate", body))

    def _render_attachment(
        self,
        scenario: FakeWorldScenario,
        source: ForumSource,
        path: str,
        session: FakeWorldSession | None,
        token: str | None,
    ) -> RenderResult:
        attachment_id = path[len("/attachment/") :]
        if not attachment_id or "/" in attachment_id:
            return RenderResult(self._not_found())
        attachment = _find_attachment_or_404(scenario, attachment_id)
        if attachment is None:
            return RenderResult(self._not_found())
        gate = self._attachment_gate(source, session, token)
        if gate is not None:
            return gate
        body = attachment.body or b""
        headers = {
            "content-type": attachment.media_type,
            "content-length": str(len(body)),
            "content-disposition": (
                "attachment; filename*=UTF-8''"
                + _attachment_header_name(attachment.filename)
            ),
        }
        descriptor = AttachmentDescriptor(
            attachment_id=attachment.attachment_id,
            filename=attachment.filename,
            media_type=attachment.media_type,
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
        return RenderResult(
            RenderedSourceResponse(
                status_code=200,
                headers=FrozenParams.from_mapping(headers),
                media_type=attachment.media_type,
                body=body,
                attachment=descriptor,
            )
        )

    def _render_old_thread(
        self, scenario: FakeWorldScenario, path: str
    ) -> RenderResult:
        thread_id = path[len("/old-thread/") :]
        if not thread_id or "/" in thread_id:
            return RenderResult(self._not_found())
        thread = _find_thread_or_404(scenario, thread_id)
        if thread is None:
            return RenderResult(self._not_found())
        return RenderResult(_redirect(f"/thread/{thread.thread_id}"))

    # -- runtime behavior --------------------------------------------------
    def _failure_check(
        self, thread: ForumThread, path: str
    ) -> RenderedSourceResponse | None:
        if thread.failure_schedule is None:
            return None
        attempts = self._attempts.get(path, 0) + 1
        self._attempts[path] = attempts
        if attempts <= thread.failure_schedule.failing_requests:
            return self._plain(
                thread.failure_schedule.status_code,
                "Service temporarily unavailable. Please retry in a moment.",
                {"retry-after": "5"},
            )
        return None

    def _rate_limit_check(
        self, board: ForumBoard, token: str | None
    ) -> RenderedSourceResponse | None:
        if board.rate_limit is None or token is None:
            return None
        views = self._rate_counts.get(token, 0) + 1
        self._rate_counts[token] = views
        if views > board.rate_limit.max_views_per_session:
            return self._plain(
                429,
                "Too many requests. "
                f"Retry after {board.rate_limit.retry_after_seconds} seconds.",
                {"retry-after": str(board.rate_limit.retry_after_seconds)},
            )
        return None

    # -- response helpers --------------------------------------------------
    @classmethod
    def _html(cls, status: int, title: str, body: str) -> RenderedSourceResponse:
        payload = _page(title, body).encode("utf-8")
        headers = {
            "content-type": _HTML,
            "content-length": str(len(payload)),
        }
        return RenderedSourceResponse(
            status_code=status,
            headers=FrozenParams.from_mapping(headers),
            media_type=_HTML,
            body=payload,
        )

    @classmethod
    def _ok_html(cls, title: str, body: str) -> RenderedSourceResponse:
        return cls._html(200, title, body)

    @classmethod
    def _plain(
        cls,
        status: int,
        text: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> RenderedSourceResponse:
        payload = text.encode("utf-8")
        headers = {
            "content-type": _PLAIN,
            "content-length": str(len(payload)),
        }
        headers.update(extra_headers or {})
        return RenderedSourceResponse(
            status_code=status,
            headers=FrozenParams.from_mapping(headers),
            media_type=_PLAIN,
            body=payload,
        )

    @classmethod
    def _not_found(cls) -> RenderedSourceResponse:
        return cls._html(
            404,
            "Not found — BlackGate",
            "<h1>Not found</h1><p>The requested page does not exist.</p>",
        )

    @classmethod
    def _method_not_allowed(cls) -> RenderedSourceResponse:
        return cls._plain(405, "Method not allowed.", {"allow": "GET, POST"})


def _landing_body(scenario: FakeWorldScenario, source: ForumSource) -> str:
    n_members = len(source.aliases)
    n_threads = len(
        tuple(thread for board in source.boards for thread in board.threads)
    )
    n_posts = len(
        tuple(
            post
            for board in source.boards
            for thread in board.threads
            for post in thread.posts
        )
    )
    return (
        f"<header><h1>{_escape(source.title)}</h1>"
        '<p class="tagline">A secured trading community.</p></header>'
        '<nav><a href="/login">Log in</a> &middot; '
        '<a href="/register">Register</a> &middot; '
        '<a href="/index">Board index</a> &middot; '
        '<a href="/legacy/board-announcements">Legacy interface</a></nav>'
        f'<section class="stats"><p>{n_members} members &middot; '
        f"{n_threads} threads &middot; {n_posts} posts</p></section>"
        f'<section class="preview"><h2>Latest announcement</h2>'
        f"{_public_preview(scenario, source)}</section>"
    )


def _public_preview(scenario: FakeWorldScenario, source: ForumSource) -> str:
    for board in source.boards:
        for thread in board.threads:
            if thread.visibility.value == "PUBLIC" and thread.posts:
                first = thread.posts[0]
                alias = scenario.find_alias(first.author.value)
                return (
                    f'<div class="preview-post"><p>{_escape(alias.display_name)}: '
                    f"{_escape(first.content[:160])}</p>"
                    f'<p><a href="/thread/{thread.thread_id}">Read full thread '
                    f"&mdash; {_escape(thread.title)}</a></p></div>"
                )
    return "<p>No public announcements yet.</p>"  # pragma: no cover - v1 has one


def _login_body(source: ForumSource, *, error: str | None = None) -> str:
    invite = _escape(source.policy.invite_only_message)
    body = [
        "<h1>Log in</h1>",
        '<form action="/login" method="post">',
        '<input type="hidden" name="next" value="/index">',
        '<label>Username <input type="text" name="username"></label>',
        '<label>Password <input type="password" name="password"></label>',
        '<button type="submit">Log in</button>',
        "</form>",
        f'<p class="register-note">{invite}</p>',
    ]
    if error is not None:
        body.insert(1, f'<p class="error">{_escape(error)}</p>')
    return "\n".join(body)


def _register_body(source: ForumSource) -> str:
    return f"<h1>Registration</h1><p>{_escape(source.policy.invite_only_message)}</p>"


def _board_index_body(scenario: FakeWorldScenario, source: ForumSource) -> str:
    rows = []
    for board in source.boards:
        n_threads = len(board.threads)
        n_posts = sum(len(t.posts) for t in board.threads)
        rows.append(
            f'<li class="board"><a href="/board/{board.board_id}">'
            f"{_escape(board.name)}</a> "
            f'<span class="meta">{n_threads} threads &middot; '
            f"{n_posts} posts</span>"
            f"<p>{_escape(board.description)}</p></li>"
        )
    return (
        "<h1>Board index</h1>"
        '<nav><a href="/index">Board index</a> &middot; '
        '<a href="/logout">Log out</a></nav>'
        '<ul class="boards">' + "\n".join(rows) + "</ul>"
    )


def _board_page_body(scenario: FakeWorldScenario, board: ForumBoard) -> str:
    rows = []
    for thread in board.threads:
        replies = len(thread.posts) - 1
        last = _format_utc(thread.posts[-1].created_at)
        rows.append(
            f'<li class="thread"><a href="/thread/{thread.thread_id}">'
            f"{_escape(thread.title)}</a> "
            f'<span class="meta">{replies} replies &middot; last {last}</span></li>'
        )
    return (
        f"<h1>{_escape(board.name)}</h1>"
        f"<p>{_escape(board.description)}</p>"
        '<nav><a href="/index">Board index</a></nav>'
        '<ul class="threads">' + "\n".join(rows) + "</ul>"
    )


def _thread_page_body(
    scenario: FakeWorldScenario,
    thread: ForumThread,
    page: int,
    posts_per_page: int,
) -> str:
    total_pages = max(1, math.ceil(len(thread.posts) / posts_per_page))
    start = (page - 1) * posts_per_page
    page_posts = thread.posts[start : start + posts_per_page]
    posts_html = "\n".join(
        _post_html(scenario, thread, post, posts_per_page) for post in page_posts
    )
    board = scenario.find_board(thread.board_id.value)
    nav: list[str] = []
    if page > 1:
        canonical = f"/thread/{thread.thread_id}"
        if page - 1 > 1:
            canonical = f"{canonical}?page={page - 1}"
        nav.append(f'<a href="{canonical}">&laquo; Previous</a>')
    if page < total_pages:
        nav.append(
            f'<a href="/thread/{thread.thread_id}?page={page + 1}">Next &raquo;</a>'
        )
    nav_html = " ".join(nav)
    back_nav = (
        f'<nav><a href="/board/{board.board_id}">'
        f"Back to {_escape(board.name)}</a></nav>"
    )
    return (
        f"<h1>{_escape(thread.title)}</h1>"
        f'<p class="pagination-meta">Page {page} of {total_pages}</p>'
        f'<div class="posts">{posts_html}</div>'
        f'<nav class="pagination">{nav_html}</nav>'
        f"{back_nav}"
    )


def _post_page_body(
    scenario: FakeWorldScenario,
    thread: ForumThread,
    post: ForumPost,
    page: int,
    posts_per_page: int,
) -> str:
    board = scenario.find_board(thread.board_id.value)
    return (
        f"<h1>{_escape(thread.title)}</h1>"
        f'<div class="posts">{_post_html(scenario, thread, post, posts_per_page)}</div>'
        f'<nav><a href="/thread/{thread.thread_id}?page={page}">'
        f"Back to thread (page {page})</a> &middot; "
        f'<a href="/board/{board.board_id}">Back to {_escape(board.name)}</a></nav>'
    )


def _post_html(
    scenario: FakeWorldScenario,
    thread: ForumThread,
    post: ForumPost,
    posts_per_page: int,
) -> str:
    if post.deleted:
        return (
            f'<div class="post deleted" id="post-{post.post_id}">'
            "<p>This post was removed by a moderator.</p></div>"
        )
    alias = scenario.find_alias(post.author.value)
    parts = [
        f'<div class="post" id="post-{post.post_id}">',
        '<div class="post-header">'
        f'<span class="author">{_escape(alias.display_name)}</span> '
        f'<span class="rep">(Rep: {alias.reputation})</span> '
        f'<span class="date">{_format_utc(post.created_at)}</span>'
        "</div>",
    ]
    if post.quote is not None:
        quoted = scenario.find_post(post.quote.target_post_id.value)
        quoted_alias = scenario.find_alias(quoted.author.value)
        quoted_thread = scenario.find_thread(quoted.thread_id.value)
        quoted_page = _page_of_post(quoted_thread, quoted, posts_per_page)
        url = (
            f"/thread/{quoted_thread.thread_id}?page={quoted_page}"
            f"#post-{quoted.post_id}"
        )
        parts.append(
            '<blockquote class="quote">'
            f'<a href="{url}">{_escape(quoted_alias.display_name)} wrote:</a>'
            f"<p>{_escape(post.quote.snippet)}</p>"
            "</blockquote>"
        )
    parts.append(f'<div class="content">{_escape(post.content)}</div>')
    if post.edited_at is not None:
        parts.append(
            '<p class="edit-note">Last edited '
            f"{_format_utc(post.edited_at)} by "
            f"{_escape(alias.display_name)} &mdash; "
            f"{_escape(post.edited_note)}</p>"
        )
    if post.attachment_ids:
        attachment_links = []
        for attachment_id in post.attachment_ids:
            attachment = scenario.find_attachment(attachment_id.value)
            size = len(attachment.body or b"")
            attachment_links.append(
                f'<li><a href="/attachment/{attachment.attachment_id}">'
                f"{_escape(attachment.filename)}</a> "
                f'<span class="meta">({attachment.media_type}, {size} B)</span></li>'
            )
        parts.append('<ul class="attachments">' + "\n".join(attachment_links) + "</ul>")
    parts.append("</div>")
    return "\n".join(parts)


def _page_of_post(thread: ForumThread, post: ForumPost, posts_per_page: int) -> int:
    index = next(i for i, candidate in enumerate(thread.posts) if candidate == post)
    return index // posts_per_page + 1


def _legacy_body(board: ForumBoard) -> str:
    """Deliberately malformed-but-browser-tolerable legacy board page.

    This page intentionally violates well-formedness: an unclosed ``<h2>``,
    an unclosed ``<ul>`` with unescaped attribute quotes, and raw ``<`` and
    ``&`` characters in text. It is stable and tagged by behavior
    ``FW-BG-MALFORMED-001``.
    """
    first_thread = board.threads[0]
    lines = [
        '<div class="legacy-main">',
        "<h2>Legacy BlackGate announcements",
        '<ul class="legacy-list">',
        f'<li><a href="/thread/{first_thread.thread_id}" '
        'title="open "the rules" here">Welcome & house rules</a>',
        "<li>mirror maintenance window this weekend, price stays < 300",
        "</div>",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "AttachmentDescriptor",
    "FakeWorldRenderer",
    "FakeWorldRequest",
    "FakeWorldSession",
    "FrozenParams",
    "HttpMethod",
    "RenderResult",
    "RenderedSourceResponse",
    "SessionUpdate",
]
