# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Fake World truth-model tests (PR 6) — matrix G1, G5 and truth validation."""

from __future__ import annotations

from datetime import datetime

import pytest
from tests.unit.testing.fake_world.helpers import (
    T0,
    canonical_scenario,
    get,
    login,
    make_scenario,
    make_truth,
)

from darkula.testing.fake_world import (
    FakeWorldRenderer,
    FakeWorldValidationError,
)
from darkula.testing.fake_world.identifiers import (
    ActorId,
    AliasId,
    BehaviorId,
    EventId,
    LocationId,
    OrganizationId,
    RelationshipId,
    ScenarioId,
    ScenarioVersion,
)
from darkula.testing.fake_world.truth import (
    FakeWorldTruth,
    TruthActor,
    TruthAlias,
    TruthEvent,
    TruthOrganization,
    TruthRelationship,
)

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


class TestHospitalTruth:
    """G1/G2 — fictional hospital canonical location (Washington State)."""

    def test_g1_hospital_truth_is_canonical_washington_state(self) -> None:
        scenario = canonical_scenario()
        hospital = scenario.truth.organization(OrganizationId("organization-001"))
        assert hospital.name == "Mason Creek General Hospital"
        assert hospital.kind == "healthcare"
        location = scenario.truth.location(hospital.location_id)
        assert location.region == "Washington"
        assert location.country == "United States"
        # Washington State, clearly not Washington, D.C.
        assert "D.C." not in location.region

    def test_g2_rendered_listing_exposes_observable_clues(self) -> None:
        scenario = canonical_scenario()
        renderer = FakeWorldRenderer()
        session = login(renderer, scenario)
        page = get(
            renderer,
            scenario,
            "/thread/thr-hospital-creds",
            session=session,
        ).text
        assert "Mason Creek General Hospital" in page
        assert "Mason Creek, Washington" in page
        assert "mgmt.local" in page

    def test_g3_washington_dc_is_a_separate_location(self) -> None:
        scenario = canonical_scenario()
        dc = scenario.truth.location(LocationId("location-002"))
        assert dc.region == "Washington, D.C."
        assert dc.location_id != LocationId("location-001")
        # Both Washington tokens are observable somewhere in the forum.
        renderer = FakeWorldRenderer()
        session = login(renderer, scenario)
        dc_page = get(
            renderer,
            scenario,
            "/thread/thr-dc-conference",
            session=session,
        ).text
        assert "Washington, D.C." in dc_page
        # The two Washington tokens are observably distinct strings.
        assert "D.C." in dc_page

    def test_g4_scenario_makes_no_extraction_assertion(self) -> None:
        scenario = canonical_scenario()
        # The scenario never claims a resolved location or score; only the
        # truth record and rendered text exist.
        assert scenario.traceability is not None
        geo = scenario.traceability.behavior(BehaviorId("FW-BG-GEO-001"))
        assert "geo" in geo.requirement.lower()
        assert geo.future_eval is not None and "score" not in geo.future_eval

    def test_g5_truth_holds_hidden_actor_identity(self) -> None:
        scenario = canonical_scenario()
        actor = scenario.truth.actor(ActorId("actor-001"))
        assert actor.primary_alias == "zerofox77"
        aliases = {
            a.alias
            for a in scenario.truth.aliases
            if a.actor_id == ActorId("actor-001")
        }
        assert aliases == {"zerofox77", "zfox"}
        # The hidden alias is a truth-only fact: it must never be rendered.
        renderer = FakeWorldRenderer()
        pages = "\n".join(
            get(renderer, scenario, path).text
            for path in (
                "/",
                "/login",
                "/thread/thr-hospital-creds",
                "/thread/thr-dc-conference",
            )
        )
        assert "zfox" not in pages


class TestTruthValidation:
    def test_duplicate_actor_id_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="duplicate actor"):
            FakeWorldTruth(
                scenario_id=ScenarioId("x-test"),
                scenario_version=ScenarioVersion(1),
                actors=(
                    TruthActor(actor_id=ActorId("actor-1"), primary_alias="a"),
                    TruthActor(actor_id=ActorId("actor-1"), primary_alias="b"),
                ),
            )

    def test_alias_unknown_actor_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="unknown truth actor"):
            FakeWorldTruth(
                scenario_id=ScenarioId("x-test"),
                scenario_version=ScenarioVersion(1),
                aliases=(
                    TruthAlias(
                        alias_id=AliasId("alias-1"),
                        actor_id=ActorId("ghost"),
                        alias="ghost",
                    ),
                ),
            )

    def test_organization_unknown_location_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="unknown truth location"):
            FakeWorldTruth(
                scenario_id=ScenarioId("x-test"),
                scenario_version=ScenarioVersion(1),
                organizations=(
                    TruthOrganization(
                        organization_id=OrganizationId("org-1"),
                        name="Orphan Org",
                        kind="healthcare",
                        location_id=LocationId("location-missing"),
                    ),
                ),
            )

    def test_relationship_unknown_actor_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="unknown truth actor"):
            FakeWorldTruth(
                scenario_id=ScenarioId("x-test"),
                scenario_version=ScenarioVersion(1),
                actors=(),
                relationships=(
                    TruthRelationship(
                        relationship_id=RelationshipId("rel-1"),
                        kind="owns",
                        source_ref="actor:actor-ghost",
                        target_ref="actor:actor-other",
                    ),
                ),
            )

    def test_relationship_bad_ref_syntax_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="kind:value"):
            TruthRelationship(
                relationship_id=RelationshipId("rel-1"),
                kind="owns",
                source_ref="no-colon-here",
                target_ref="actor:actor-1",
            )

    def test_naive_event_timestamp_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="UTC"):
            TruthEvent(
                event_id=EventId("event-1"),
                occurred_at=datetime(2026, 1, 1),
                description="naive",
            )

    def test_event_unknown_actor_rejected(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="unknown truth actor"):
            FakeWorldTruth(
                scenario_id=ScenarioId("x-test"),
                scenario_version=ScenarioVersion(1),
                actors=(TruthActor(actor_id=ActorId("actor-1"), primary_alias="a"),),
                events=(
                    TruthEvent(
                        event_id=EventId("event-1"),
                        occurred_at=T0,
                        description="event",
                        actor_ids=(ActorId("actor-ghost"),),
                    ),
                ),
            )

    def test_truth_only_fact_not_rendered_anywhere(self) -> None:
        """FW8 — hidden facts stay out of every rendered observation."""
        scenario = canonical_scenario()
        renderer = FakeWorldRenderer()
        seen: list[str] = []
        for path in (
            "/",
            "/login",
            "/register",
            "/index",
            "/board/board-access",
            "/board/board-chatter",
            "/thread/thr-hospital-creds",
            "/thread/thr-dc-conference",
            "/thread/thr-welcome",
            "/legacy/board-announcements",
        ):
            response = get(renderer, scenario, path)
            seen.append(f"== {path} ==\n{response.text}")
            seen.append(str(sorted(response.headers.items())))
        blob = "\n".join(seen)
        for token in _HIDDEN_TOKENS:
            assert token not in blob, f"truth token leaked: {token}"

    def test_truth_objectives_do_not_derived_from_post_claims(self) -> None:
        scenario = canonical_scenario()
        # The forum contains conflicting/stale claims (e.g. a closed listing);
        # truth records the canonical fictional world independently.
        closing = scenario.find_post("p-host-creds-11")
        assert "Closing this listing" in closing.content
        truth_event = scenario.truth.event(EventId("event-003"))
        assert truth_event.description == "Seed listing publicly closed."


class TestTruthIndependence:
    def test_truth_and_renderer_are_structurally_separate(self) -> None:
        # The renderer module must not import truth types; truth is exposed
        # only through the scenario container to tests/evals.
        import darkula.testing.fake_world.rendering as rendering
        import darkula.testing.fake_world.truth as truth_module

        assert "FakeWorldTruth" not in rendering.__dict__
        assert "TruthActor" not in rendering.__dict__
        assert "FakeWorldTruth" in truth_module.__dict__

    def test_minimal_truth_round_trip(self) -> None:
        truth = make_truth()
        assert truth.scenario_id == ScenarioId("blackgate-core")
        assert truth.scenario_version == ScenarioVersion(1)
        make_scenario(truth=truth)  # constructs and validates fine
