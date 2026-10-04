# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Core-model tests for the Fake World (PR 6) — matrix FW1..FW10."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest
from tests.unit.testing.fake_world.helpers import (
    SCENARIO_ID,
    SCENARIO_VERSION,
    T0,
    canonical_scenario,
    get,
    make_alias,
    make_board,
    make_post,
    make_scenario,
    make_source,
    make_thread,
    make_truth,
)

from darkula.testing.fake_world import (
    FakeWorldRenderer,
    FakeWorldRequest,
    FakeWorldScenario,
    FakeWorldValidationError,
    ForumPost,
    TruthEvent,
    Visibility,
)
from darkula.testing.fake_world.identifiers import (
    EventId,
    ForumAliasId,
    PostId,
    ScenarioId,
    ScenarioVersion,
    ThreadId,
)
from darkula.testing.fake_world.registry import UnknownScenarioError


class TestScenarioIdentityAndVersioning:
    """FW1..FW4 — canonical loading and bounded identity."""

    def test_fw1_canonical_scenario_loads_valid_immutable(self) -> None:
        scenario = canonical_scenario()
        assert isinstance(scenario, FakeWorldScenario)
        assert scenario.scenario_id == ScenarioId("blackgate-core")
        assert scenario.scenario_version == ScenarioVersion(1)
        assert scenario.sources
        assert scenario.truth is not None
        assert scenario.traceability is not None
        with pytest.raises(FrozenInstanceError):
            scenario.title = "mutated"  # type: ignore[misc]

    def test_fw2_same_scenario_loaded_twice_is_semantically_identical(
        self,
    ) -> None:
        first = canonical_scenario()
        second = canonical_scenario()
        assert first == second
        assert first.truth == second.truth
        assert first.traceability is not None
        assert second.traceability is not None
        assert first.traceability.entries == second.traceability.entries

    def test_fw3_unknown_scenario_id_fails_deterministically(self) -> None:
        from darkula.testing.fake_world.registry import get_scenario

        with pytest.raises(UnknownScenarioError):
            get_scenario("no-such-scenario")
        with pytest.raises(KeyError):
            get_scenario("no-such-scenario")

    def test_fw4_unknown_version_fails_deterministically(self) -> None:
        from darkula.testing.fake_world.registry import get_scenario

        with pytest.raises(UnknownScenarioError):
            get_scenario("blackgate-core", version=99)
        with pytest.raises(FakeWorldValidationError):
            get_scenario("blackgate-core", version=0)
        with pytest.raises(FakeWorldValidationError):
            get_scenario("blackgate-core", version=-2)

    def test_scenario_id_value_validation(self) -> None:
        with pytest.raises(FakeWorldValidationError):
            ScenarioId("")
        with pytest.raises(FakeWorldValidationError):
            ScenarioId("bad id")
        with pytest.raises(FakeWorldValidationError):
            ScenarioId("control\x00char")
        with pytest.raises(FakeWorldValidationError):
            ScenarioVersion(0)

    def test_distinct_id_classes_never_compare_equal(self) -> None:
        post_id, thread_id = PostId("same"), ThreadId("same")
        assert post_id.value == thread_id.value  # same opaque value...
        assert post_id.__class__.__name__ != thread_id.__class__.__name__
        assert post_id == PostId("same")  # same-class equality still works


class TestCrossReferenceValidation:
    """FW5..FW7 — fail at scenario construction."""

    def _scenario_from_threads(
        self, threads: tuple[ForumPost, ...], **kw: object
    ) -> FakeWorldScenario:
        thread = make_thread(posts=threads)
        board = make_board(threads=(thread,))
        source = make_source(boards=(board,))
        return make_scenario(source)

    def test_fw5_duplicate_post_id_rejected(self) -> None:
        duplicate = (
            make_post("p-dup"),
            make_post("p-dup", content="second"),
        )
        with pytest.raises(FakeWorldValidationError, match="duplicate post"):
            self._scenario_from_threads(duplicate)

    def test_fw5_duplicate_board_id_rejected(self) -> None:
        board = make_board()
        with pytest.raises(FakeWorldValidationError, match="duplicate board"):
            make_source(boards=(board, board))

    def test_fw5_duplicate_alias_id_rejected(self) -> None:
        alias = make_alias()
        with pytest.raises(FakeWorldValidationError, match="duplicate forum alias"):
            make_source(aliases=(alias, alias))

    def test_fw6_missing_thread_reference_rejected(self) -> None:
        thread = make_thread(posts=(make_post("p-1", thread_id="missing"),))
        source = make_source(boards=(make_board(threads=(thread,)),))
        with pytest.raises(FakeWorldValidationError, match="thread missing"):
            make_scenario(source)

    def test_fw6_missing_board_reference_rejected(self) -> None:
        thread = make_thread(board_id="no-board")
        source = make_source(boards=(make_board(threads=(thread,)),))
        with pytest.raises(FakeWorldValidationError, match="does not belong"):
            make_scenario(source)

    def test_fw6_missing_alias_reference_rejected(self) -> None:
        posts = (make_post("p-1", author="ghost"),)
        thread = make_thread(posts=posts)
        source = make_source(boards=(make_board(threads=(thread,)),))
        with pytest.raises(FakeWorldValidationError, match="unknown forum alias"):
            make_scenario(source)

    def test_fw6_missing_quote_target_rejected(self) -> None:
        from darkula.testing.fake_world import ForumQuote

        quoted = make_post("p-1")
        quote = ForumQuote(target_post_id=PostId("p-nonexistent"), snippet="x")
        posts = (
            quoted,
            ForumPost(
                post_id=PostId("p-2"),
                thread_id=ThreadId("thr-one"),
                author=ForumAliasId("al-alice"),
                created_at=T0,
                content="quoting the void",
                quote=quote,
            ),
        )
        with pytest.raises(FakeWorldValidationError, match="unknown post"):
            self._scenario_from_threads(posts)

    def test_fw6_missing_attachment_reference_rejected(self) -> None:
        from darkula.testing.fake_world.identifiers import AttachmentId

        posts = (make_post("p-1", attachment_ids=(AttachmentId("att-ghost"),)),)
        thread = make_thread(posts=posts)
        source = make_source(boards=(make_board(threads=(thread,)),))
        with pytest.raises(FakeWorldValidationError, match="unknown attachment"):
            make_scenario(source)

    def test_fw7_naive_post_timestamp_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="UTC"):
            ForumPost(
                post_id=PostId("p-naive"),
                thread_id=ThreadId("thr-one"),
                author=ForumAliasId("al-alice"),
                created_at=datetime(2026, 1, 1),  # naive
                content="naive",
            )

    def test_fw7_naive_truth_event_timestamp_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="UTC"):
            TruthEvent(
                event_id=EventId("event-x"),
                occurred_at=datetime(2026, 1, 1),  # naive
                description="naive event",
            )

    def test_fw7_edited_at_before_creation_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="edited_at"):
            ForumPost(
                post_id=PostId("p-edits"),
                thread_id=ThreadId("thr-one"),
                author=ForumAliasId("al-alice"),
                created_at=T0.replace(hour=1),
                edited_at=T0,
                content="impossible edit",
            )


class TestTruthScenarioAgreement:
    def test_truth_scenario_mismatch_rejected(self) -> None:
        truth = make_truth(ScenarioId("other-core"), SCENARIO_VERSION)
        with pytest.raises(FakeWorldValidationError, match="truth scenario_id"):
            make_scenario(truth=truth)

    def test_truth_version_mismatch_rejected(self) -> None:
        truth = make_truth(SCENARIO_ID, ScenarioVersion(2))
        with pytest.raises(FakeWorldValidationError, match="truth scenario_version"):
            make_scenario(truth=truth)


class TestRendererContract:
    def test_unknown_source_is_a_programming_error(self) -> None:
        scenario = canonical_scenario()
        renderer = FakeWorldRenderer()
        from darkula.testing.fake_world.identifiers import SourceId

        request = FakeWorldRequest.get(SourceId("other-source"), "/")
        with pytest.raises(FakeWorldValidationError, match="unknown source"):
            renderer.render(scenario, request)

    def test_invalid_request_path_rejected(self) -> None:
        from darkula.testing.fake_world import HttpMethod
        from darkula.testing.fake_world.identifiers import SourceId

        with pytest.raises(FakeWorldValidationError, match="must start with"):
            FakeWorldRequest(
                source_id=SourceId("blackgate"),
                method=HttpMethod.GET,
                path="no-leading-slash",
            )

    def test_fw10_mutation_never_observable(self) -> None:
        scenario = canonical_scenario()
        with pytest.raises(FrozenInstanceError):
            scenario.sources = ()  # type: ignore[misc]
        source = scenario.sources[0]
        with pytest.raises(FrozenInstanceError):
            source.boards = ()  # type: ignore[misc]

    def test_canonical_scenario_uses_fixed_utc_times(self) -> None:
        scenario = canonical_scenario()
        assert scenario.authored_at.tzinfo is not None
        for post in scenario._all_posts():
            assert post.created_at.tzinfo is not None
        for event in scenario.truth.events:
            assert event.occurred_at.tzinfo is not None

    def test_visibility_vocabulary_is_closed(self) -> None:
        assert {v.value for v in Visibility} == {
            "PUBLIC",
            "REGISTERED",
            "AUTHENTICATED",
        }

    def test_get_helper_renders_without_session(self) -> None:
        scenario = canonical_scenario()
        response = get(FakeWorldRenderer(), scenario, "/")
        assert response.status_code == 200
