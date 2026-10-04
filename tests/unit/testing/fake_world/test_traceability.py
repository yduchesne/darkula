# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Traceability manifest tests (PR 6) — matrix T1..T6."""

from __future__ import annotations

import re

import pytest
from tests.unit.testing.fake_world.helpers import (
    SCENARIO_ID,
    SCENARIO_VERSION,
    canonical_scenario,
)

from darkula.testing.fake_world import (
    BehaviorTraceability,
    FakeWorldValidationError,
    TraceabilityManifest,
)
from darkula.testing.fake_world.identifiers import (
    BehaviorId,
)

_SCORE_LIKE = re.compile(
    r"(?i)(\bscore\b|\bthreshold\b|pass@|accuracy|f1\b|>\s?0\.\d|%\b)"
)


def _manifest() -> TraceabilityManifest:
    manifest = canonical_scenario().traceability
    assert manifest is not None
    return manifest


class TestTraceabilityManifest:
    def test_t1_behavior_ids_unique(self) -> None:
        manifest = _manifest()
        ids = [str(e.behavior_id) for e in manifest.entries]
        assert len(ids) == len(set(ids))
        # manifest construction itself rejects duplicates
        with pytest.raises(FakeWorldValidationError, match="duplicate behavior"):
            TraceabilityManifest(
                scenario_id=SCENARIO_ID,
                scenario_version=SCENARIO_VERSION,
                entries=(
                    BehaviorTraceability(
                        behavior_id=BehaviorId("FW-BG-AUTH-001"),
                        description="a",
                        archetype="x",
                        requirement="y",
                        refs=("route:/login",),
                        test_ids=("R9",),
                    ),
                    BehaviorTraceability(
                        behavior_id=BehaviorId("FW-BG-AUTH-001"),
                        description="b",
                        archetype="x",
                        requirement="y",
                        refs=("route:/login",),
                        test_ids=("R9",),
                    ),
                ),
            )

    def test_t2_scenario_version_linkage(self) -> None:
        manifest = _manifest()
        assert manifest.scenario_id == SCENARIO_ID
        assert manifest.scenario_version == SCENARIO_VERSION
        scenario = canonical_scenario()
        assert scenario.traceability is not None
        assert scenario.traceability.scenario_id == scenario.scenario_id
        assert scenario.traceability.scenario_version == scenario.scenario_version

    def test_t3_referenced_routes_and_objects_exist(self) -> None:
        # validate_refs already ran at scenario construction; exercising it
        # directly proves the linkage is operational.
        scenario = canonical_scenario()
        assert scenario.traceability is not None
        scenario.traceability.validate_refs(scenario)

    def test_t3_unknown_route_ref_fails(self) -> None:
        manifest = _manifest()
        manifest = TraceabilityManifest(
            scenario_id=SCENARIO_ID,
            scenario_version=SCENARIO_VERSION,
            entries=(
                BehaviorTraceability(
                    behavior_id=BehaviorId("FW-BG-TEST-999"),
                    description="dangling route",
                    archetype="x",
                    requirement="y",
                    refs=("route:/does-not-exist",),
                    test_ids=("T3",),
                ),
            ),
        )
        with pytest.raises(FakeWorldValidationError, match="route"):
            manifest.validate_refs(canonical_scenario())

    def test_t3_unknown_post_ref_fails(self) -> None:
        manifest = TraceabilityManifest(
            scenario_id=SCENARIO_ID,
            scenario_version=SCENARIO_VERSION,
            entries=(
                BehaviorTraceability(
                    behavior_id=BehaviorId("FW-BG-TEST-998"),
                    description="dangling post",
                    archetype="x",
                    requirement="y",
                    refs=("post:p-no-such-post",),
                    test_ids=("T3",),
                ),
            ),
        )
        with pytest.raises(FakeWorldValidationError, match="unknown post"):
            manifest.validate_refs(canonical_scenario())

    def test_t4_every_canonical_behavior_has_deterministic_test_mapping(
        self,
    ) -> None:
        manifest = _manifest()
        for entry in manifest.entries:
            assert entry.test_ids, str(entry.behavior_id)
            for test_id in entry.test_ids:
                assert re.fullmatch(r"^[A-Z][A-Z0-9-]{0,31}$", test_id), (
                    f"{entry.behavior_id} has invalid test id {test_id!r}"
                )

    def test_t4_blank_test_mapping_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="test"):
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-BG-TEST-997"),
                description="x",
                archetype="x",
                requirement="y",
                refs=("route:/",),
                test_ids=(),
            )

    def test_t4_invalid_test_id_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="invalid test id"):
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-BG-TEST-996"),
                description="x",
                archetype="x",
                requirement="y",
                refs=("route:/",),
                test_ids=("lowercase-test",),
            )

    def test_t5_requirement_and_archetype_non_blank(self) -> None:
        manifest = _manifest()
        for entry in manifest.entries:
            assert entry.description.strip()
            assert entry.archetype.strip()
            assert entry.requirement.strip()

    def test_t6_future_eval_is_metadata_only_no_invented_scores(self) -> None:
        manifest = _manifest()
        for entry in manifest.entries:
            if entry.future_eval is None:
                continue
            assert entry.future_eval.strip()
            assert _SCORE_LIKE.search(entry.future_eval) is None, (
                f"{entry.behavior_id} would invent a score/threshold: "
                f"{entry.future_eval!r}"
            )

    def test_t6_all_canonical_behaviors_record_future_eval(self) -> None:
        manifest = _manifest()
        # Every significant behavior records its eval applicability (PR 6
        # traceability follow-up) without inventing scores.
        for entry in manifest.entries:
            assert entry.future_eval is not None, str(entry.behavior_id)

    def test_behavior_lookup_fails_closed(self) -> None:
        manifest = _manifest()
        with pytest.raises(FakeWorldValidationError, match="unknown behavior"):
            manifest.behavior(BehaviorId("FW-BG-NOPE-000"))

    def test_empty_manifest_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="at least one entry"):
            TraceabilityManifest(
                scenario_id=SCENARIO_ID,
                scenario_version=SCENARIO_VERSION,
                entries=(),
            )
