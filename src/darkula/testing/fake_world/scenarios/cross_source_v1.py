# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-source v1 — interconnected synthetic underground ecosystem (PR 15).

One stable scenario/version identity (``darkula-cross-source`` v1) containing
four fictional sources that share one truth world:

- BlackGate (canonical PR 6 forum, reused unmodified);
- AccessBay (synthetic access/credential marketplace);
- NightLeak (synthetic leak-publication site);
- ShadowTalk (secondary forum: whispers, cross-source quotes, noise).

Everything here is fictional: names, organizations, credentials, addresses,
posts, listings, identities, and timestamps. No live infrastructure is
mirrored and no real stolen/victim data is copied. Cross-source identity is
canonical only in :class:`FakeWorldTruth`; Darkula production code never sees
it and never merges observations across sources.
"""

from __future__ import annotations

from datetime import UTC, datetime

from darkula.testing.fake_world.identifiers import (
    ActorId,
    AliasId,
    BehaviorId,
    BoardId,
    EventId,
    ForumAliasId,
    LeakEntryId,
    ListingId,
    LocationId,
    OrganizationId,
    PostId,
    RelationshipId,
    ScenarioId,
    ScenarioVersion,
    SellerId,
    SourceId,
    ThreadId,
)
from darkula.testing.fake_world.model import (
    FakeWorldScenario,
    ForumAlias,
    ForumBoard,
    ForumPost,
    ForumQuote,
    ForumSource,
    ForumThread,
    LeakEntry,
    LeakPublicationState,
    LeakSource,
    ListingCategory,
    ListingState,
    MarketplaceListing,
    MarketplaceSeller,
    MarketplaceSource,
    SessionPolicy,
    Visibility,
)
from darkula.testing.fake_world.rendering import (
    LEAK_STATIC_ROUTES,
    MARKETPLACE_STATIC_ROUTES,
)
from darkula.testing.fake_world.scenarios.blackgate_v1 import build_blackgate_source_v1
from darkula.testing.fake_world.traceability import (
    BehaviorTraceability,
    TraceabilityManifest,
)
from darkula.testing.fake_world.truth import (
    FakeWorldTruth,
    TruthActor,
    TruthAlias,
    TruthEvent,
    TruthLocation,
    TruthOrganization,
    TruthRelationship,
)

SCENARIO_ID = ScenarioId("darkula-cross-source")
SCENARIO_VERSION = ScenarioVersion(1)

ACCESSBAY_SOURCE_ID = SourceId("accessbay")
NIGHTLEAK_SOURCE_ID = SourceId("nightleak")
SHADOWTALK_SOURCE_ID = SourceId("shadowtalk")

_AUTHORED_AT = datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)
_ACCESSBAY_USERNAME = "north_broker"
_ACCESSBAY_PASSWORD = "accessbay-test-password"  # nosec B105
_NIGHTLEAK_USERNAME = "nightleak_reader"
_NIGHTLEAK_PASSWORD = "nightleak-test-password"  # nosec B105
_SHADOWTALK_USERNAME = "shadowtalk_reader"
_SHADOWTALK_PASSWORD = "shadowtalk-test-password"  # nosec B105


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _accessbay_policy() -> SessionPolicy:
    return SessionPolicy(
        login_username=_ACCESSBAY_USERNAME,
        login_password=_ACCESSBAY_PASSWORD,
        posts_per_page=10,
        session_max_protected_requests=12,
    )


def _nightleak_policy() -> SessionPolicy:
    return SessionPolicy(
        login_username=_NIGHTLEAK_USERNAME,
        login_password=_NIGHTLEAK_PASSWORD,
        posts_per_page=10,
        session_max_protected_requests=12,
    )


def _shadowtalk_policy() -> SessionPolicy:
    return SessionPolicy(
        login_username=_SHADOWTALK_USERNAME,
        login_password=_SHADOWTALK_PASSWORD,
        posts_per_page=10,
        session_max_protected_requests=12,
    )


# ---------------------------------------------------------------------------
# AccessBay
# ---------------------------------------------------------------------------


def _accessbay_sellers() -> tuple[MarketplaceSeller, ...]:
    return (
        MarketplaceSeller(
            seller_id=SellerId("seller-zfox"),
            alias="zfox",
            reputation=418,
            joined_at=_dt(2025, 5, 2, 10, 0),
            note="Escrow only. Refuses unbrokered deals.",
        ),
        MarketplaceSeller(
            seller_id=SellerId("seller-broker-north"),
            alias="broker_north",
            reputation=74,
            joined_at=_dt(2026, 1, 12, 18, 30),
            note="New seller account; no escrow history yet.",
        ),
        MarketplaceSeller(
            seller_id=SellerId("seller-quietdata"),
            alias="quietdata",
            reputation=12,
            joined_at=_dt(2026, 2, 20, 8, 45),
        ),
    )


def _accessbay_listings() -> tuple[MarketplaceListing, ...]:
    return (
        MarketplaceListing(
            listing_id=ListingId("listing-cred-1"),
            seller_id=SellerId("seller-zfox"),
            title="Office 365 global admin - Mason Creek General Hospital (WA)",
            body=(
                "Full tenant takeover for Mason Creek General Hospital. "
                "Same package advertised on the old forum under zerofox77. "
                "Mailbox copies and user enumeration available on request. "
                "Escrow only; proof of access before payment."
            ),
            category=ListingCategory.CREDENTIALS,
            price="$750",
            created_at=_dt(2026, 2, 25, 7, 15),
            state=ListingState.ACTIVE,
            visibility=Visibility.REGISTERED,
            cross_references=("post:p-host-creds-1",),
            mirrors=("accessbay-mirror.example.test",),
        ),
        MarketplaceListing(
            listing_id=ListingId("listing-cred-2"),
            seller_id=SellerId("seller-zfox"),
            title="VPN access - regional clinic",
            body=(
                "Remote VPN access to a regional clinic network. Credentials "
                "rotate monthly; my load survives rotation."
            ),
            category=ListingCategory.ACCESS,
            price="$200",
            created_at=_dt(2026, 2, 26, 11, 0),
            updated_at=_dt(2026, 3, 1, 9, 30),
            state=ListingState.UPDATED,
            visibility=Visibility.REGISTERED,
        ),
        MarketplaceListing(
            listing_id=ListingId("listing-data-1"),
            seller_id=SellerId("seller-quietdata"),
            title="Patient records sample pack",
            body=(
                "Small synthetic sample pack of patient records for vetting "
                "buyers. Full set withheld until escrow clears."
            ),
            category=ListingCategory.DATA,
            price="$1200",
            created_at=_dt(2026, 2, 28, 16, 40),
            state=ListingState.STALE,
            duplicate_of=ListingId("listing-data-1b"),
            visibility=Visibility.REGISTERED,
        ),
        MarketplaceListing(
            listing_id=ListingId("listing-data-1b"),
            seller_id=SellerId("seller-quietdata"),
            title="Patient records sample pack (old copy)",
            body=(
                "Old copy of the patient records sample listing. Superseded "
                "by a newer listing; kept for archive."
            ),
            category=ListingCategory.DATA,
            price="$1200",
            created_at=_dt(2026, 2, 27, 12, 5),
            state=ListingState.SOLD,
            visibility=Visibility.REGISTERED,
        ),
        MarketplaceListing(
            listing_id=ListingId("listing-false-1"),
            seller_id=SellerId("seller-broker-north"),
            title="Mason Creek General Hospital - cheapest takeover",
            body=(
                "Undercutting everyone. I have the exact same Mason Creek "
                "General Hospital tenant and I am the original owner. "
                "No escrow, pay first."
            ),
            category=ListingCategory.CREDENTIALS,
            price="$300",
            created_at=_dt(2026, 3, 2, 20, 10),
            state=ListingState.ACTIVE,
            visibility=Visibility.REGISTERED,
        ),
        MarketplaceListing(
            listing_id=ListingId("listing-removed-1"),
            seller_id=SellerId("seller-broker-north"),
            title="Removed access bundle",
            body="This listing was withdrawn by the seller.",
            category=ListingCategory.ACCESS,
            price="$90",
            created_at=_dt(2026, 3, 3, 8, 0),
            state=ListingState.REMOVED,
            visibility=Visibility.REGISTERED,
        ),
    )


def _build_accessbay_source() -> MarketplaceSource:
    return MarketplaceSource(
        source_id=ACCESSBAY_SOURCE_ID,
        title="AccessBay",
        description=(
            "Fictional access/credential marketplace: sellers, listings, "
            "gating, duplicate/removed states, and cross-source references."
        ),
        base_domain="accessbay.example.test",
        sellers=_accessbay_sellers(),
        listings=_accessbay_listings(),
        policy=_accessbay_policy(),
        routes=MARKETPLACE_STATIC_ROUTES,
        mirrors=("accessbay-mirror.example.test",),
    )


# ---------------------------------------------------------------------------
# NightLeak
# ---------------------------------------------------------------------------


def _nightleak_entries() -> tuple[LeakEntry, ...]:
    return (
        LeakEntry(
            entry_id=LeakEntryId("leak-001"),
            victim="Mason Creek General Hospital",
            teaser=(
                "Mason Creek General Hospital: 14 GB of patient billing and "
                "internal mail. Full release after payment."
            ),
            body=(
                "Synthetic sample only. The fictional operator claims "
                "patient billing exports and internal mail archives. No real "
                "records exist; this is a deterministic Fake World fixture."
            ),
            state=LeakPublicationState.PUBLISHED,
            created_at=_dt(2026, 3, 2, 6, 0),
            operator_alias="zfox",
            sample_metadata="sample: 25 synthetic rows, sha256 reserved-for-fixture",
            cross_references=("listing:listing-cred-1", "post:p-host-creds-1"),
            mirrors=("nightleak-mirror.example.test",),
        ),
        LeakEntry(
            entry_id=LeakEntryId("leak-002"),
            victim="Edgewater Regional Care",
            teaser=(
                "Edgewater Regional Care: teaser posted. Full dataset "
                "withheld pending negotiation."
            ),
            state=LeakPublicationState.TEASER,
            created_at=_dt(2026, 3, 4, 9, 20),
            operator_alias="zfox",
            mirrors=("nightleak-mirror.example.test",),
        ),
        LeakEntry(
            entry_id=LeakEntryId("leak-003"),
            victim="Mason Creek General Hospital",
            teaser="Correction notice for the Mason Creek publication.",
            body=(
                "Correction: the earlier row count was wrong. Synthetic "
                "fixture updated deterministically."
            ),
            state=LeakPublicationState.CHANGED,
            created_at=_dt(2026, 3, 5, 12, 0),
            updated_at=_dt(2026, 3, 6, 8, 30),
            operator_alias="zfox",
            cross_references=("leak:leak-001",),
        ),
        LeakEntry(
            entry_id=LeakEntryId("leak-004"),
            victim="Redacted regional operator",
            teaser="Removed publication.",
            state=LeakPublicationState.REMOVED,
            created_at=_dt(2026, 3, 7, 15, 45),
            operator_alias="zfox",
        ),
    )


def _build_nightleak_source() -> LeakSource:
    return LeakSource(
        source_id=NIGHTLEAK_SOURCE_ID,
        title="NightLeak",
        description=(
            "Fictional leak-publication site: teaser/published/changed/"
            "removed states, operator alias, bounded sample metadata, and "
            "mirror references."
        ),
        base_domain="nightleak.example.test",
        entries=_nightleak_entries(),
        policy=_nightleak_policy(),
        routes=LEAK_STATIC_ROUTES,
        mirrors=("nightleak-mirror.example.test",),
    )


# ---------------------------------------------------------------------------
# ShadowTalk (forum)
# ---------------------------------------------------------------------------


def _shadowtalk_aliases() -> tuple[ForumAlias, ...]:
    return (
        ForumAlias(
            alias_id=ForumAliasId("al-st-zfox"),
            display_name="zfox_zero",
            reputation=53,
            joined_at=_dt(2025, 9, 9, 9, 9),
        ),
        ForumAlias(
            alias_id=ForumAliasId("al-st-debunk"),
            display_name="debunk_me",
            reputation=180,
            joined_at=_dt(2025, 2, 2, 12, 0),
        ),
        ForumAlias(
            alias_id=ForumAliasId("al-st-broker"),
            display_name="broker_north",
            reputation=9,
            joined_at=_dt(2026, 1, 15, 20, 0),
        ),
        ForumAlias(
            alias_id=ForumAliasId("al-st-lurker"),
            display_name="quietlurker",
            reputation=2,
            joined_at=_dt(2026, 2, 1, 8, 0),
        ),
    )


def _build_shadowtalk_thread() -> ForumThread:
    posts = (
        ForumPost(
            post_id=PostId("p-st-1"),
            thread_id=ThreadId("thr-st-crossref"),
            author=ForumAliasId("al-st-zfox"),
            created_at=_dt(2026, 3, 3, 10, 0),
            visibility=Visibility.REGISTERED,
            content=(
                "Listing is live on AccessBay as zfox. Same package I ran on "
                "the old forum. Serious buyers can verify the escrow history."
            ),
        ),
        ForumPost(
            post_id=PostId("p-st-2"),
            thread_id=ThreadId("thr-st-crossref"),
            author=ForumAliasId("al-st-debunk"),
            created_at=_dt(2026, 3, 3, 11, 30),
            visibility=Visibility.REGISTERED,
            quote=ForumQuote(
                target_post_id=PostId("p-st-1"),
                snippet="Same package I ran on the old forum.",
            ),
            content=(
                "Corroborating: the AccessBay wording matches the old forum "
                "listing template almost exactly. Treat the two as one "
                "seller, not two."
            ),
        ),
        ForumPost(
            post_id=PostId("p-st-3"),
            thread_id=ThreadId("thr-st-crossref"),
            author=ForumAliasId("al-st-broker"),
            created_at=_dt(2026, 3, 3, 13, 45),
            visibility=Visibility.REGISTERED,
            content=(
                "Rubbish. The hospital was seized by law enforcement last "
                "week and everyone here is selling a dead listing. I heard it "
                "from someone who works there."
            ),
        ),
        ForumPost(
            post_id=PostId("p-st-4"),
            thread_id=ThreadId("thr-st-crossref"),
            author=ForumAliasId("al-st-debunk"),
            created_at=_dt(2026, 3, 3, 14, 5),
            visibility=Visibility.REGISTERED,
            content=(
                "That seizure claim is unsupported. No source, no date, no "
                "record. Classic noise from a brand-new account."
            ),
        ),
        ForumPost(
            post_id=PostId("p-st-5"),
            thread_id=ThreadId("thr-st-crossref"),
            author=ForumAliasId("al-st-lurker"),
            created_at=_dt(2026, 3, 4, 2, 15),
            visibility=Visibility.REGISTERED,
            content=(
                "Mirror check: the AccessBay fallback domain rotates on the "
                "hour and matches the NightLeak mirror schedule. Both look "
                "like the same operator."
            ),
        ),
    )
    return ForumThread(
        thread_id=ThreadId("thr-st-crossref"),
        board_id=BoardId("board-st-crossref"),
        title="Cross-source corroboration and rumours",
        visibility=Visibility.REGISTERED,
        created_at=_dt(2026, 3, 3, 10, 0),
        posts=posts,
    )


def _build_shadowtalk_source() -> ForumSource:
    return ForumSource(
        source_id=SHADOWTALK_SOURCE_ID,
        title="ShadowTalk",
        description=(
            "Fictional secondary forum: cross-source quotes, corroboration, "
            "rumour/noise, stale claims, and mirror chatter."
        ),
        base_domain="shadowtalk.example.test",
        boards=(
            ForumBoard(
                board_id=BoardId("board-st-crossref"),
                name="Cross-source chatter",
                description="Quotes, corroboration, and rumour about other sources.",
                visibility=Visibility.REGISTERED,
                threads=(_build_shadowtalk_thread(),),
            ),
        ),
        aliases=_shadowtalk_aliases(),
        policy=_shadowtalk_policy(),
        routes=("/", "/login", "/register", "/logout", "/index"),
    )


# ---------------------------------------------------------------------------
# Truth
# ---------------------------------------------------------------------------


def _build_truth() -> FakeWorldTruth:
    return FakeWorldTruth(
        scenario_id=SCENARIO_ID,
        scenario_version=SCENARIO_VERSION,
        actors=(
            TruthActor(
                actor_id=ActorId("actor-001"),
                primary_alias="zerofox77",
                note="One actor observed under several unrelated handles.",
            ),
            TruthActor(
                actor_id=ActorId("actor-002"),
                primary_alias="debunk_me",
                note="Independent corroborator and debunker.",
            ),
        ),
        aliases=(
            TruthAlias(
                alias_id=AliasId("alias-001"),
                actor_id=ActorId("actor-001"),
                alias="zerofox77",
            ),
            TruthAlias(
                alias_id=AliasId("alias-002"),
                actor_id=ActorId("actor-001"),
                alias="zfox",
            ),
            TruthAlias(
                alias_id=AliasId("alias-003"),
                actor_id=ActorId("actor-001"),
                alias="zfox_zero",
            ),
            TruthAlias(
                alias_id=AliasId("alias-004"),
                actor_id=ActorId("actor-001"),
                alias="ghost_admin",
            ),
            TruthAlias(
                alias_id=AliasId("alias-005"),
                actor_id=ActorId("actor-002"),
                alias="debunk_me",
            ),
        ),
        organizations=(
            TruthOrganization(
                organization_id=OrganizationId("organization-001"),
                name="Mason Creek General Hospital",
                kind="healthcare",
                location_id=LocationId("location-001"),
            ),
            TruthOrganization(
                organization_id=OrganizationId("organization-002"),
                name="Edgewater Regional Care",
                kind="healthcare",
                location_id=LocationId("location-001"),
            ),
        ),
        locations=(
            TruthLocation(
                location_id=LocationId("location-001"),
                name="Mason Creek, Washington, United States",
                region="Washington",
                country="United States",
            ),
            TruthLocation(
                location_id=LocationId("location-002"),
                name="Washington, D.C., United States",
                region="Washington, D.C.",
                country="United States",
            ),
        ),
        relationships=(
            TruthRelationship(
                relationship_id=RelationshipId("relationship-001"),
                kind="actor_advertises_listing",
                source_ref="actor:actor-001",
                target_ref="post:p-host-creds-1",
                note="zerofox77 advertised the hospital listing on BlackGate.",
            ),
            TruthRelationship(
                relationship_id=RelationshipId("relationship-002"),
                kind="listing_targets_organization",
                source_ref="post:p-host-creds-1",
                target_ref="organization:organization-001",
                note="The BlackGate listing advertises hospital access.",
            ),
            TruthRelationship(
                relationship_id=RelationshipId("relationship-003"),
                kind="actor_owns_alias",
                source_ref="actor:actor-001",
                target_ref="alias:alias-002",
                note="zfox is the same actor as zerofox77.",
            ),
            TruthRelationship(
                relationship_id=RelationshipId("relationship-004"),
                kind="actor_advertises_listing",
                source_ref="actor:actor-001",
                target_ref="listing:listing-cred-1",
                note="Cross-source: the same package on AccessBay.",
            ),
            TruthRelationship(
                relationship_id=RelationshipId("relationship-005"),
                kind="leak_concerns_organization",
                source_ref="leak:leak-001",
                target_ref="organization:organization-001",
                note="NightLeak concerns the same hospital.",
            ),
            TruthRelationship(
                relationship_id=RelationshipId("relationship-006"),
                kind="actor_operates_leak",
                source_ref="actor:actor-001",
                target_ref="leak:leak-001",
                note="The zfox operator alias runs the NightLeak entry.",
            ),
            TruthRelationship(
                relationship_id=RelationshipId("relationship-007"),
                kind="actor_owns_alias",
                source_ref="actor:actor-001",
                target_ref="alias:alias-004",
                note="Hidden alias is truth-only.",
            ),
        ),
        events=(
            TruthEvent(
                event_id=EventId("event-001"),
                occurred_at=_dt(2026, 2, 11, 4, 12),
                description="actor-001 advertises hospital access on BlackGate.",
                actor_ids=(ActorId("actor-001"),),
            ),
            TruthEvent(
                event_id=EventId("event-002"),
                occurred_at=_dt(2026, 2, 25, 7, 15),
                description="The same actor reposts the package on AccessBay.",
                actor_ids=(ActorId("actor-001"),),
            ),
            TruthEvent(
                event_id=EventId("event-003"),
                occurred_at=_dt(2026, 3, 2, 6, 0),
                description="NightLeak publishes the fictional hospital entry.",
                actor_ids=(ActorId("actor-001"),),
            ),
            TruthEvent(
                event_id=EventId("event-004"),
                occurred_at=_dt(2026, 3, 6, 10, 0),
                description=(
                    "Truth-only: actor-001 privately sells Edgewater access "
                    "through a one-to-one channel that no source renders."
                ),
                actor_ids=(ActorId("actor-001"),),
            ),
            TruthEvent(
                event_id=EventId("event-005"),
                occurred_at=_dt(2026, 3, 3, 13, 45),
                description=(
                    "FALSE CLAIM: broker_north claims the hospital was seized. "
                    "No such seizure occurred in the fictional world."
                ),
            ),
            TruthEvent(
                event_id=EventId("event-006"),
                occurred_at=_dt(2026, 3, 4, 2, 15),
                description=(
                    "Mirror fact: AccessBay and NightLeak share rotating "
                    "mirror hosts operated by one actor."
                ),
                actor_ids=(ActorId("actor-001"),),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------


def _traceability() -> TraceabilityManifest:
    return TraceabilityManifest(
        scenario_id=SCENARIO_ID,
        scenario_version=SCENARIO_VERSION,
        entries=(
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-AB-LIST-001"),
                description="Marketplace index exposes deterministic listings.",
                archetype="Access/credential marketplace catalogue.",
                requirement="PR 15 evaluated recon/extraction source.",
                refs=("route:/listings", "listing:listing-cred-1"),
                test_ids=("FW15", "V15"),
                future_eval="Recon marketplace-structure eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-AB-LIST-002"),
                description="A registered-only listing redirects to login.",
                archetype="Gated marketplace listing.",
                requirement="PR 7 crawler authentication; PR 15 recon eval.",
                refs=("route:/listing/listing-cred-1",),
                test_ids=("FW15", "V15"),
                future_eval="Recon gating eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-AB-STATE-001"),
                description="Updated/duplicate/removed listings render states.",
                archetype="Marketplace lifecycle states.",
                requirement="PR 8 provenance; PR 15 extraction eval.",
                refs=(
                    "listing:listing-cred-2",
                    "listing:listing-data-1",
                    "listing:listing-removed-1",
                ),
                test_ids=("FW15",),
                future_eval="Extraction state eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-AB-FALSE-001"),
                description="One marketplace listing makes a false claim.",
                archetype="Noisy/false marketplace claim.",
                requirement="PR 15 noise tolerance eval.",
                refs=("listing:listing-false-1", "event:event-005"),
                test_ids=("FW15",),
                future_eval="SourceAnalyst noise eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-AB-MIRROR-001"),
                description="Marketplace renders a mirror reference.",
                archetype="Mirror/alternate-endpoint discovery.",
                requirement="PR 15 recon mirror eval.",
                refs=("listing:listing-cred-1",),
                test_ids=("FW15", "V15"),
                future_eval="Recon mirror eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-NL-LEAK-001"),
                description="Leak index/detail render deterministic entries.",
                archetype="Ransomware/data-leak publication site.",
                requirement="PR 15 evaluated extraction source.",
                refs=("route:/leaks", "leak:leak-001"),
                test_ids=("FW15", "V15"),
                future_eval="Recon leak-site eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-NL-STATE-001"),
                description="Teaser/changed/removed states render differently.",
                archetype="Leak publication lifecycle.",
                requirement="PR 15 temporal novelty eval.",
                refs=("leak:leak-002", "leak:leak-003", "leak:leak-004"),
                test_ids=("FW15",),
                future_eval="SourceAnalyst novelty eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-NL-MIRROR-001"),
                description="Leak site renders a mirror reference.",
                archetype="Mirror/alternate-endpoint discovery.",
                requirement="PR 15 recon mirror eval.",
                refs=("leak:leak-001",),
                test_ids=("FW15", "V15"),
                future_eval="Recon mirror eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-ST-FORUM-001"),
                description="Secondary forum reuses forum rendering.",
                archetype="Secondary corroboration forum.",
                requirement="PR 15 cross-source eval.",
                refs=("route:/index", "thread:thr-st-crossref"),
                test_ids=("FW15", "V15"),
                future_eval="Cross-source recon eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-ST-NOISE-001"),
                description="A quoted, unsupported seizure claim renders as noise.",
                archetype="Rumour and unsupported claim.",
                requirement="PR 15 FP-trap and noise eval.",
                refs=("post:p-st-3", "post:p-st-4"),
                test_ids=("FW15",),
                future_eval="Relationship/SourceAnalyst noise eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-XS-ACTOR-001"),
                description="One truth actor appears under distinct handles.",
                archetype="Cross-source actor aliasing.",
                requirement="PR 15 cross-source truth eval.",
                refs=(
                    "actor:actor-001",
                    "post:p-host-creds-1",
                    "listing:listing-cred-1",
                    "leak:leak-001",
                    "post:p-st-1",
                ),
                test_ids=("FW15", "V15"),
                future_eval="Cross-source correlation eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-XS-ORG-001"),
                description="One fictional organization appears in three sources.",
                archetype="Cross-source organization recurrence.",
                requirement="PR 15 cross-source extraction eval.",
                refs=(
                    "organization:organization-001",
                    "post:p-host-creds-1",
                    "listing:listing-cred-1",
                    "leak:leak-001",
                ),
                test_ids=("FW15", "V15"),
                future_eval="Cross-source extraction eval.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-XS-HIDDEN-001"),
                description="Hidden truth (alias/event) is never rendered.",
                archetype="Truth vs observation separation.",
                requirement="PR 15 truth-isolation eval.",
                refs=("truthalias:alias-004", "event:event-004"),
                test_ids=("FW15", "V15"),
                future_eval="Truth-isolation integration test.",
            ),
            BehaviorTraceability(
                behavior_id=BehaviorId("FW-XS-MIRROR-001"),
                description="Cross-source mirror fact recurs across sources.",
                archetype="Shared mirror operator.",
                requirement="PR 15 recon mirror eval.",
                refs=("event:event-006",),
                test_ids=("FW15",),
                future_eval="Recon mirror eval.",
            ),
        ),
    )


def build_cross_source_v1() -> FakeWorldScenario:
    """Construct (and fully validate) the ``darkula-cross-source`` v1 scenario."""
    return FakeWorldScenario(
        scenario_id=SCENARIO_ID,
        scenario_version=SCENARIO_VERSION,
        title="Darkula cross-source ecosystem",
        description=(
            "One fictional world spanning BlackGate, AccessBay, NightLeak, "
            "and ShadowTalk, with shared truth identities and noisy "
            "observations."
        ),
        sources=(
            build_blackgate_source_v1(),
            _build_accessbay_source(),
            _build_nightleak_source(),
            _build_shadowtalk_source(),
        ),
        truth=_build_truth(),
        traceability=_traceability(),
        authored_at=_AUTHORED_AT,
    )


__all__ = [
    "ACCESSBAY_SOURCE_ID",
    "NIGHTLEAK_SOURCE_ID",
    "SCENARIO_ID",
    "SCENARIO_VERSION",
    "SHADOWTALK_SOURCE_ID",
    "build_cross_source_v1",
]
