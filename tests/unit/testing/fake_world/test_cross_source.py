# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-source Fake World tests (PR 15) — matrix FW15-1..FW15-18.

Deterministic, offline tests over the ``darkula-cross-source`` v1 scenario
and the additive marketplace/leak/ShadowTalk archetypes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from darkula.testing.fake_world import (
    CrossSourceRenderer,
    FakeWorldRequest,
    FakeWorldScenario,
    FakeWorldSession,
    FakeWorldTruth,
    FakeWorldValidationError,
    ForumSource,
    LeakPublicationState,
    LeakSource,
    ListingState,
    MarketplaceListing,
    MarketplaceSeller,
    MarketplaceSource,
    RenderedSourceResponse,
    SessionPolicy,
    TruthActor,
    get_scenario,
)
from darkula.testing.fake_world.http_adapter import FakeWorldHttpService
from darkula.testing.fake_world.identifiers import (
    ActorId,
    AliasId,
    EventId,
    ListingId,
    OrganizationId,
    ScenarioId,
    ScenarioVersion,
    SellerId,
    SourceId,
)
from darkula.testing.fake_world.model import ListingCategory
from darkula.testing.fake_world.rendering import (
    MARKETPLACE_STATIC_ROUTES,
)
from darkula.testing.fake_world.truth import TruthAlias

SCENARIO_ID = "darkula-cross-source"
ACCESSBAY = SourceId("accessbay")
NIGHTLEAK = SourceId("nightleak")
SHADOWTALK = SourceId("shadowtalk")
BLACKGATE = SourceId("blackgate")
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def scenario() -> FakeWorldScenario:
    return get_scenario(SCENARIO_ID, version=1)


def renderer() -> CrossSourceRenderer:
    return CrossSourceRenderer()


def get(
    source: SourceId,
    path: str,
    *,
    session: FakeWorldSession | None = None,
    renderer_: CrossSourceRenderer | None = None,
) -> RenderedSourceResponse:
    return (
        (renderer_ or renderer())
        .render(scenario(), FakeWorldRequest.get(source, path), session)
        .response
    )


def _minimal_policy() -> SessionPolicy:
    return SessionPolicy(
        login_username="reader",
        login_password="reader-password",
        posts_per_page=5,
        session_max_protected_requests=5,
    )


def _minimal_truth() -> FakeWorldTruth:
    return FakeWorldTruth(
        scenario_id=ScenarioId("custom-cross"),
        scenario_version=ScenarioVersion(1),
        actors=(TruthActor(actor_id=ActorId("actor-001"), primary_alias="alice"),),
        aliases=(
            TruthAlias(
                alias_id=AliasId("alias-001"),
                actor_id=ActorId("actor-001"),
                alias="alice",
            ),
        ),
    )


def _minimal_scenario(source: MarketplaceSource | LeakSource) -> FakeWorldScenario:
    return FakeWorldScenario(
        scenario_id=ScenarioId("custom-cross"),
        scenario_version=ScenarioVersion(1),
        title="Custom cross",
        description="Custom cross-source test scenario",
        sources=(source,),
        truth=_minimal_truth(),
        traceability=None,
        authored_at=T0,
    )


def _seller(seller_id: str = "seller-a", alias: str = "alice") -> MarketplaceSeller:
    return MarketplaceSeller(
        seller_id=SellerId(seller_id),
        alias=alias,
        reputation=1,
        joined_at=T0,
    )


def _listing(
    listing_id: str = "listing-a", seller_id: str = "seller-a"
) -> MarketplaceListing:
    return MarketplaceListing(
        listing_id=ListingId(listing_id),
        seller_id=SellerId(seller_id),
        title="Listing",
        body="Body",
        category=ListingCategory.ACCESS,
        price="$1",
        created_at=T0,
        state=ListingState.ACTIVE,
    )


class TestScenarioStructure:
    """FW15-11..FW15-17, FW15-1."""

    def test_fw15_1_accessbay_constructed_validly(self) -> None:
        source = scenario().marketplace_source(ACCESSBAY)
        assert isinstance(source, MarketplaceSource)
        assert source.base_domain == "accessbay.example.test"
        assert len(source.sellers) == 3
        assert len(source.listings) == 6

    def test_fw15_11_one_actor_distinct_aliases(self) -> None:
        truth = scenario().truth
        zfox = truth.alias(AliasId("alias-002"))
        zero = truth.alias(AliasId("alias-003"))
        assert zfox.actor_id == zero.actor_id == ActorId("actor-001")
        # Distinct observations across sources.
        assert scenario().find_seller("seller-zfox").alias == "zfox"
        assert scenario().find_alias("al-st-zfox").display_name == "zfox_zero"

    def test_fw15_12_organization_across_three_sources(self) -> None:
        loaded = scenario()
        organization = loaded.truth.organization(OrganizationId("organization-001"))
        assert organization.name == "Mason Creek General Hospital"
        blackgate = loaded.find_post("p-host-creds-1").content
        accessbay = loaded.find_listing("listing-cred-1").body
        nightleak = loaded.find_leak_entry("leak-001").victim
        assert organization.name in blackgate
        assert "Mason Creek General Hospital" in accessbay
        assert organization.name in nightleak

    def test_fw15_13_hidden_truth_never_rendered(self) -> None:
        loaded = scenario()
        rendered = []
        for source in loaded.sources:
            for path in source.routes:
                response = get(source.source_id, path)
                rendered.append(response.text)
        blob = "\n".join(rendered)
        assert "ghost_admin" not in blob
        assert "event-004" not in blob

    def test_fw15_14_false_claim_rendered_not_truth(self) -> None:
        loaded = scenario()
        listing = loaded.find_listing("listing-false-1")
        assert "original owner" in listing.body
        event = loaded.truth.event(EventId("event-005"))
        assert "FALSE CLAIM" in event.description

    def test_fw15_15_behavior_manifest_ids_valid(self) -> None:
        manifest = scenario().traceability
        assert manifest is not None
        ids = {entry.behavior_id.value for entry in manifest.entries}
        assert {"FW-AB-LIST-001", "FW-NL-LEAK-001", "FW-ST-FORUM-001"} <= ids
        assert any(item.startswith("FW-XS-") for item in ids)
        # BlackGate behavior IDs remain registrable and unchanged elsewhere.
        assert all(
            entry.behavior_id.value.startswith("FW-") for entry in manifest.entries
        )

    def test_fw15_17_forced_utc_timestamps(self) -> None:
        assert scenario().authored_at.utcoffset() == UTC.utcoffset(None)
        for listing in scenario().marketplace_source(ACCESSBAY).listings:
            assert listing.created_at.utcoffset() == UTC.utcoffset(None)


class TestMarketplaceRendering:
    """FW15-2..FW15-6."""

    def test_fw15_2_duplicate_listing_identity_fails(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="duplicate"):
            MarketplaceSource(
                source_id=SourceId("dup"),
                title="Dup",
                description="d",
                base_domain="dup.example.test",
                sellers=(_seller(),),
                listings=(_listing("listing-a"), _listing("listing-a")),
                policy=_minimal_policy(),
                routes=MARKETPLACE_STATIC_ROUTES,
            )

    def test_fw15_3_unknown_seller_ref_fails(self) -> None:
        source = MarketplaceSource(
            source_id=SourceId("bad"),
            title="Bad",
            description="d",
            base_domain="bad.example.test",
            sellers=(_seller(),),
            listings=(_listing("listing-a", seller_id="seller-missing"),),
            policy=_minimal_policy(),
            routes=MARKETPLACE_STATIC_ROUTES,
        )
        with pytest.raises(FakeWorldValidationError, match="unknown seller"):
            _minimal_scenario(source)

    def test_fw15_4_same_request_deterministic(self) -> None:
        first = get(ACCESSBAY, "/listings")
        second = get(ACCESSBAY, "/listings")
        assert first.body == second.body
        assert first.status_code == second.status_code == 200

    def test_fw15_5_gated_listing_redirects_to_login(self) -> None:
        response = get(ACCESSBAY, "/listing/listing-cred-1")
        assert response.status_code == 302
        assert "/login" in (response.redirect_location or "")

    def test_fw15_5_gated_listing_visible_with_session(self) -> None:
        session = FakeWorldSession(token="t", alias="north_broker")
        response = get(ACCESSBAY, "/listing/listing-cred-1", session=session)
        assert response.status_code == 200
        assert "Mason Creek General Hospital" in response.text

    def test_fw15_6_listing_states_render(self) -> None:
        session = FakeWorldSession(token="t", alias="north_broker")
        updated = get(ACCESSBAY, "/listing/listing-cred-2", session=session)
        assert "UPDATED" in updated.text
        removed = get(ACCESSBAY, "/listing/listing-removed-1", session=session)
        assert "removed" in removed.text
        stale = get(ACCESSBAY, "/listing/listing-data-1", session=session)
        assert "duplicate" in stale.text

    def test_unknown_listing_is_404(self) -> None:
        session = FakeWorldSession(token="t", alias="north_broker")
        response = get(ACCESSBAY, "/listing/nope", session=session)
        assert response.status_code == 404

    def test_seller_page_lists_listings(self) -> None:
        session = FakeWorldSession(token="t", alias="north_broker")
        response = get(ACCESSBAY, "/seller/seller-zfox", session=session)
        assert response.status_code == 200
        assert "zfox" in response.text

    def test_login_flow_deterministic(self) -> None:
        r = renderer()
        post = FakeWorldRequest.post_form(
            ACCESSBAY,
            "/login",
            form={
                "username": "north_broker",
                "password": "accessbay-test-password",
                "next": "/listings",
            },
        )
        result = r.render(scenario(), post)
        assert result.response.status_code == 302
        assert result.session_update is not None
        bad = r.render(
            scenario(),
            FakeWorldRequest.post_form(
                ACCESSBAY, "/login", form={"username": "x", "password": "y"}
            ),
        )
        assert "Invalid username" in bad.response.text


class TestLeakRendering:
    """FW15-7..FW15-9."""

    def test_fw15_7_index_deterministic(self) -> None:
        first = get(NIGHTLEAK, "/leaks")
        second = get(NIGHTLEAK, "/leaks")
        assert first.body == second.body
        assert "Mason Creek General Hospital" in first.text

    def test_fw15_8_detail_renders_claim(self) -> None:
        response = get(NIGHTLEAK, "/leak/leak-001")
        assert response.status_code == 200
        assert "patient billing" in response.text

    def test_fw15_9_states_render(self) -> None:
        teaser = get(NIGHTLEAK, "/leak/leak-002")
        assert "withheld" in teaser.text
        changed = get(NIGHTLEAK, "/leak/leak-003")
        assert "updated" in changed.text.lower()
        removed = get(NIGHTLEAK, "/leak/leak-004")
        assert "removed" in removed.text.lower()
        assert get(NIGHTLEAK, "/leak/missing").status_code == 404

    def test_leak_source_model_fields(self) -> None:
        source = scenario().leak_source(NIGHTLEAK)
        assert source.entries[0].state is LeakPublicationState.PUBLISHED
        assert source.mirrors == ("nightleak-mirror.example.test",)


class TestShadowTalk:
    """FW15-10."""

    def test_fw15_10_shadowtalk_uses_forum_rendering(self) -> None:
        source = scenario().source(SHADOWTALK)
        assert isinstance(source, ForumSource)
        assert get(SHADOWTALK, "/").status_code == 200
        assert get(SHADOWTALK, "/index").status_code == 302  # gated
        session = FakeWorldSession(token="t", alias="shadowtalk_reader")
        index = get(SHADOWTALK, "/index", session=session)
        assert index.status_code == 200
        thread = get(SHADOWTALK, "/thread/thr-st-crossref", session=session)
        assert "corroborating" in thread.text.lower()

    def test_cross_source_quote_and_noise_render(self) -> None:
        session = FakeWorldSession(token="t", alias="shadowtalk_reader")
        thread = get(SHADOWTALK, "/thread/thr-st-crossref", session=session)
        assert "seized" in thread.text.lower()
        assert "unsupported" in thread.text.lower()


class TestMirrorBehaviour:
    """Mirror references are observable in marketplace/leak sources."""

    def test_mirrors_render_on_landing(self) -> None:
        assert "accessbay-mirror.example.test" in get(ACCESSBAY, "/").text
        assert "nightleak-mirror.example.test" in get(NIGHTLEAK, "/").text


class TestCrossSourceRendererDispatch:
    """The single dispatcher preserves shared request/response contracts."""

    def test_unknown_source_fails_closed(self) -> None:
        with pytest.raises(FakeWorldValidationError, match="unknown source"):
            renderer().render(scenario(), FakeWorldRequest.get(SourceId("nope"), "/"))

    def test_reset_restores_state(self) -> None:
        r = renderer()
        r.render(scenario(), FakeWorldRequest.get(BLACKGATE, "/"))
        r.reset()
        assert r.render(scenario(), FakeWorldRequest.get(BLACKGATE, "/")).response


class TestCrossSourceHttpAdapter:
    """New sources reach the shared HTTP translation seam."""

    def _service(self) -> FakeWorldHttpService:
        return FakeWorldHttpService(
            renderer(),
            scenario_id=SCENARIO_ID,
            version=1,
            source_id="blackgate",
            host_map={
                "accessbay.example.test": "accessbay",
                "nightleak.example.test": "nightleak",
                "shadowtalk.example.test": "shadowtalk",
            },
        )

    def test_blackgate_is_default(self) -> None:
        wire = self._service().handle("GET", "/")
        assert wire.status_code == 200
        assert b"BlackGate" in wire.body

    def test_accessbay_dispatched_by_host(self) -> None:
        wire = self._service().handle(
            "GET", "/listings", headers={"host": "accessbay.example.test"}
        )
        assert wire.status_code == 200
        assert b"Listings" in wire.body

    def test_nightleak_dispatched_by_host(self) -> None:
        wire = self._service().handle(
            "GET", "/leaks", headers={"host": "nightleak.example.test:8080"}
        )
        assert wire.status_code == 200
        assert b"Mason Creek General Hospital" in wire.body

    def test_shadowtalk_dispatched_by_host(self) -> None:
        wire = self._service().handle(
            "GET", "/", headers={"host": "shadowtalk.example.test"}
        )
        assert wire.status_code == 200
        assert b"ShadowTalk" in wire.body
