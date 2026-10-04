# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Independent Fake World truth model (PR 6).

Truth is what the scenario author declares objectively true in the fictional
universe: actors, aliases, organizations, locations, relationships, and events.
It is structurally separate from every rendered observation and is consumed
only by tests/evals — never by production Darkula code (architecture guard in
``tests/unit/testing/fake_world/test_architecture.py``).

Rendering exposes only a partial, noisy manifestation of this world. Truth may
know facts no rendered page states directly (this is the core PR 6 invariant:
``truth != observation``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from darkula.testing.fake_world.identifiers import (
    ActorId,
    AliasId,
    EventId,
    FakeWorldValidationError,
    LocationId,
    OrganizationId,
    RelationshipId,
    ScenarioId,
    ScenarioVersion,
    require_utc,
    split_ref,
)

#: Reference kinds resolved inside the truth model itself.
_TRUTH_REF_KINDS = frozenset(
    {"actor", "alias", "organization", "location", "relationship", "event"}
)


def _require_text(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FakeWorldValidationError(f"{field_name} must not be blank")


def _require_unique(ids: Iterable[object], *, field_name: str) -> None:
    seen: set[str] = set()
    for item in ids:
        value = str(item)
        if value in seen:
            raise FakeWorldValidationError(
                f"duplicate {field_name} identity: {value!r}"
            )
        seen.add(value)


@dataclass(frozen=True, slots=True)
class TruthActor:
    """One fictional world actor (a human operator or operator group)."""

    actor_id: ActorId
    primary_alias: str
    note: str = ""

    def __post_init__(self) -> None:
        _require_text(self.primary_alias, field_name="actor primary alias")


@dataclass(frozen=True, slots=True)
class TruthAlias:
    """One world-level alias belonging to a fictional actor.

    An actor may hold aliases that never surface in any rendered source;
    those hidden identities are truth-only facts.
    """

    alias_id: AliasId
    actor_id: ActorId
    alias: str

    def __post_init__(self) -> None:
        _require_text(self.alias, field_name="alias")


@dataclass(frozen=True, slots=True)
class TruthOrganization:
    """One fictional organization, canonical in the truth ontology."""

    organization_id: OrganizationId
    name: str
    kind: str
    location_id: LocationId

    def __post_init__(self) -> None:
        _require_text(self.name, field_name="organization name")
        _require_text(self.kind, field_name="organization kind")


@dataclass(frozen=True, slots=True)
class TruthLocation:
    """One canonical fictional location.

    ``region`` distinguishes ambiguous places explicitly (for example
    ``"Washington"`` for the State versus ``"Washington, D.C."`` for the
    federal district), which later extraction PRs can leverage.
    """

    location_id: LocationId
    name: str
    region: str
    country: str

    def __post_init__(self) -> None:
        _require_text(self.name, field_name="location name")
        _require_text(self.region, field_name="location region")
        _require_text(self.country, field_name="location country")


@dataclass(frozen=True, slots=True)
class TruthRelationship:
    """One objectively true relationship between world objects.

    References use stable ``kind:value`` strings and may span the truth and
    rendered worlds (for example ``post:p-host-creds-1``). Forum-side refs
    are validated by the scenario, because only the scenario knows both
    worlds.
    """

    relationship_id: RelationshipId
    kind: str
    source_ref: str
    target_ref: str
    note: str = ""

    def __post_init__(self) -> None:
        _require_text(self.kind, field_name="relationship kind")
        split_ref(self.source_ref)
        split_ref(self.target_ref)


@dataclass(frozen=True, slots=True)
class TruthEvent:
    """One fixed-timestamp fictional world event (canonical, UTC)."""

    event_id: EventId
    occurred_at: datetime
    description: str
    actor_ids: tuple[ActorId, ...] = ()

    def __post_init__(self) -> None:
        require_utc(self.occurred_at, field_name="event occurred_at")
        _require_text(self.description, field_name="event description")


@dataclass(frozen=True, slots=True)
class FakeWorldTruth:
    """Independent truth of one scenario version.

    Validation is intentionally self-contained for truth-internal
    references; references to forum objects are checked when the scenario
    (which knows both worlds) is constructed.
    """

    scenario_id: ScenarioId
    scenario_version: ScenarioVersion
    actors: tuple[TruthActor, ...] = ()
    aliases: tuple[TruthAlias, ...] = ()
    organizations: tuple[TruthOrganization, ...] = ()
    locations: tuple[TruthLocation, ...] = ()
    relationships: tuple[TruthRelationship, ...] = ()
    events: tuple[TruthEvent, ...] = ()

    _actors_by_id: dict[str, TruthActor] = field(init=False, repr=False)
    _aliases_by_id: dict[str, TruthAlias] = field(init=False, repr=False)
    _organizations_by_id: dict[str, TruthOrganization] = field(init=False, repr=False)
    _locations_by_id: dict[str, TruthLocation] = field(init=False, repr=False)
    _relationships_by_id: dict[str, TruthRelationship] = field(init=False, repr=False)
    _events_by_id: dict[str, TruthEvent] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _require_unique([a.actor_id for a in self.actors], field_name="actor")
        _require_unique([a.alias_id for a in self.aliases], field_name="alias")
        _require_unique(
            [o.organization_id for o in self.organizations],
            field_name="organization",
        )
        _require_unique(
            [loc.location_id for loc in self.locations],
            field_name="location",
        )
        _require_unique(
            [r.relationship_id for r in self.relationships],
            field_name="relationship",
        )
        _require_unique([e.event_id for e in self.events], field_name="event")

        object.__setattr__(
            self, "_actors_by_id", {str(a.actor_id): a for a in self.actors}
        )
        object.__setattr__(
            self, "_aliases_by_id", {str(a.alias_id): a for a in self.aliases}
        )
        object.__setattr__(
            self,
            "_organizations_by_id",
            {str(o.organization_id): o for o in self.organizations},
        )
        object.__setattr__(
            self,
            "_locations_by_id",
            {str(loc.location_id): loc for loc in self.locations},
        )
        object.__setattr__(
            self,
            "_relationships_by_id",
            {str(r.relationship_id): r for r in self.relationships},
        )
        object.__setattr__(
            self, "_events_by_id", {str(e.event_id): e for e in self.events}
        )

        for alias in self.aliases:
            self.actor(alias.actor_id)
        for organization in self.organizations:
            self.location(organization.location_id)
        for event in self.events:
            require_utc(event.occurred_at, field_name="event occurred_at")
            for actor_id in event.actor_ids:
                self.actor(actor_id)
        for relationship in self.relationships:
            self._validate_relationship_refs(relationship)

    def _validate_relationship_refs(self, relationship: TruthRelationship) -> None:
        for ref in (relationship.source_ref, relationship.target_ref):
            kind, value = split_ref(ref)
            if kind not in _TRUTH_REF_KINDS:
                # Forum-world kinds are resolved by the scenario validator,
                # which knows the rendered world too.
                continue
            if kind == "actor":
                self.actor(ActorId(value))
            elif kind == "alias":
                self.alias(AliasId(value))
            elif kind == "organization":
                self.organization(OrganizationId(value))
            elif kind == "location":
                self.location(LocationId(value))
            elif kind == "relationship":
                self.relationship(RelationshipId(value))
            elif kind == "event":
                self.event(EventId(value))

    def actor(self, actor_id: ActorId) -> TruthActor:
        """Return the actor with the given identity or fail closed."""
        try:
            return self._actors_by_id[str(actor_id)]
        except KeyError as exc:
            raise FakeWorldValidationError(
                f"unknown truth actor: {str(actor_id)!r}"
            ) from exc

    def alias(self, alias_id: AliasId) -> TruthAlias:
        """Return the world alias with the given identity or fail closed."""
        try:
            return self._aliases_by_id[str(alias_id)]
        except KeyError as exc:
            raise FakeWorldValidationError(
                f"unknown truth alias: {str(alias_id)!r}"
            ) from exc

    def organization(self, organization_id: OrganizationId) -> TruthOrganization:
        """Return the organization with the given identity or fail closed."""
        try:
            return self._organizations_by_id[str(organization_id)]
        except KeyError as exc:
            raise FakeWorldValidationError(
                f"unknown truth organization: {str(organization_id)!r}"
            ) from exc

    def location(self, location_id: LocationId) -> TruthLocation:
        """Return the location with the given identity or fail closed."""
        try:
            return self._locations_by_id[str(location_id)]
        except KeyError as exc:
            raise FakeWorldValidationError(
                f"unknown truth location: {str(location_id)!r}"
            ) from exc

    def relationship(self, relationship_id: RelationshipId) -> TruthRelationship:
        """Return the relationship with the given identity or fail closed."""
        try:
            return self._relationships_by_id[str(relationship_id)]
        except KeyError as exc:
            raise FakeWorldValidationError(
                f"unknown truth relationship: {str(relationship_id)!r}"
            ) from exc

    def event(self, event_id: EventId) -> TruthEvent:
        """Return the event with the given identity or fail closed."""
        try:
            return self._events_by_id[str(event_id)]
        except KeyError as exc:
            raise FakeWorldValidationError(
                f"unknown truth event: {str(event_id)!r}"
            ) from exc


__all__ = [
    "FakeWorldTruth",
    "TruthActor",
    "TruthAlias",
    "TruthEvent",
    "TruthLocation",
    "TruthOrganization",
    "TruthRelationship",
]
