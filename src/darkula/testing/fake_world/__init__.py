# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula Fake World (PR 6).

An executable, deterministic, fully synthetic underground ecosystem used as
the external-world test boundary. Core invariant:

    Fake World truth describes the complete fictional world, while source
    renderers expose only the observations a real collector could see;
    production Darkula code never receives hidden truth or expected answers.

Package layout:

- ``identifiers`` — bounded semantic identity value types and validation;
- ``model`` — immutable scenario and observable forum model;
- ``truth`` — independent world truth (test/evaluation-only); architecture
  guard enforces that no production package imports it;
- ``rendering`` — transport-light request/response seam and the
  deterministic ``FakeWorldRenderer``;
- ``traceability`` — stable behavior IDs and manifest validation;
- ``registry`` — canonical scenario lookup by ID/version;
- ``scenarios.blackgate_v1`` — the canonical ``blackgate-core`` v1 scenario.
"""

from darkula.testing.fake_world.identifiers import (
    FakeWorldValidationError,
    ScenarioId,
    ScenarioVersion,
)
from darkula.testing.fake_world.model import (
    FakeWorldScenario,
    FakeWorldSource,
    ForumAlias,
    ForumAttachment,
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
from darkula.testing.fake_world.registry import (
    UnknownScenarioError,
    get_scenario,
    list_scenarios,
)
from darkula.testing.fake_world.rendering import (
    CrossSourceRenderer,
    FakeWorldRenderer,
    FakeWorldRequest,
    FakeWorldSession,
    FrozenParams,
    HttpMethod,
    LeakRenderer,
    MarketplaceRenderer,
    RenderedSourceResponse,
    RenderResult,
    SessionUpdate,
)
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

__all__ = [
    "BehaviorTraceability",
    "CrossSourceRenderer",
    "FakeWorldRenderer",
    "FakeWorldRequest",
    "FakeWorldScenario",
    "FakeWorldSession",
    "FakeWorldSource",
    "FakeWorldTruth",
    "FakeWorldValidationError",
    "ForumAlias",
    "ForumAttachment",
    "ForumBoard",
    "ForumPost",
    "ForumQuote",
    "ForumSource",
    "ForumThread",
    "FrozenParams",
    "HttpMethod",
    "LeakEntry",
    "LeakPublicationState",
    "LeakRenderer",
    "LeakSource",
    "ListingCategory",
    "ListingState",
    "MarketplaceListing",
    "MarketplaceRenderer",
    "MarketplaceSeller",
    "MarketplaceSource",
    "RenderResult",
    "RenderedSourceResponse",
    "ScenarioId",
    "ScenarioVersion",
    "SessionPolicy",
    "SessionUpdate",
    "TraceabilityManifest",
    "TruthActor",
    "TruthAlias",
    "TruthEvent",
    "TruthLocation",
    "TruthOrganization",
    "TruthRelationship",
    "UnknownScenarioError",
    "Visibility",
    "get_scenario",
    "list_scenarios",
]
