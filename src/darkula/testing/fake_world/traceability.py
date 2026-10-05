# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Behavior traceability manifest (PR 6).

Every significant canonical Fake World behavior carries a stable behavior ID,
for example ``FW-BG-AUTH-001``, and a machine-readable record linking:

    behavior ID -> scenario/version -> archetype/rationale ->
    requirement/future capability -> deterministic test ID(s) ->
    future evaluation applicability (metadata only, never invented scores)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from darkula.testing.fake_world.identifiers import (
    ActorId,
    AliasId,
    BehaviorId,
    EventId,
    FakeWorldValidationError,
    LocationId,
    OrganizationId,
    RelationshipId,
    ScenarioId,
    ScenarioVersion,
    split_ref,
)

if TYPE_CHECKING:
    from darkula.testing.fake_world.model import FakeWorldScenario

#: Deterministic test matrix identifiers used in ``test_ids``, for example
#: ``R14`` (rendering matrix), ``FW1`` (core-model matrix), ``G2``
#: (geography matrix), ``T1`` (traceability matrix), or ``VSLICE`` (the
#: component vertical-slice test).
TEST_ID_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,31}$")

#: Dynamic route prefixes whose embedded semantic id must resolve.
_DYNAMIC_ROUTE_PREFIXES = (
    "/thread/",
    "/board/",
    "/post/",
    "/attachment/",
    "/listing/",
    "/seller/",
    "/leak/",
)

#: Reference kinds resolved against the rendered forum world.
_FORUM_REF_KINDS = frozenset({"board", "thread", "post", "attachment", "alias"})

#: Reference kinds resolved against the rendered marketplace world.
_MARKETPLACE_REF_KINDS = frozenset({"listing", "seller"})

#: Reference kinds resolved against the rendered leak world.
_LEAK_REF_KINDS = frozenset({"leak"})

#: Reference kinds resolved against the independent truth model.
_TRUTH_REF_KINDS = frozenset(
    {"actor", "organization", "location", "relationship", "event", "truthalias"}
)


def _require_text(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FakeWorldValidationError(f"{field_name} must not be blank")


@dataclass(frozen=True, slots=True)
class BehaviorTraceability:
    """Traceability record for one significant canonical behavior.

    ``refs`` are stable ``kind:value`` strings: ``route:/login``,
    ``thread:thr-hospital-creds``, ``post:post-001``,
    ``organization:org-001``, and so on.
    ``future_eval`` is prose describing which later evaluation may reuse the
    behavior — never a score or threshold (deliberately un-invented here).
    """

    behavior_id: BehaviorId
    description: str
    archetype: str
    requirement: str
    refs: tuple[str, ...]
    test_ids: tuple[str, ...]
    future_eval: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.description, field_name="behavior description")
        _require_text(self.archetype, field_name="behavior archetype")
        _require_text(self.requirement, field_name="behavior requirement")
        if not self.refs:
            raise FakeWorldValidationError(
                f"{self.behavior_id} must reference at least one route/object"
            )
        for ref in self.refs:
            split_ref(ref)  # validates syntax
        if not self.test_ids:
            raise FakeWorldValidationError(
                f"{self.behavior_id} must map to at least one deterministic test"
            )
        for test_id in self.test_ids:
            if TEST_ID_RE.fullmatch(test_id) is None:
                raise FakeWorldValidationError(
                    f"{self.behavior_id} has invalid test id {test_id!r}; "
                    "test ids must match the documented deterministic test "
                    "matrix convention"
                )
        if self.future_eval is not None:
            _require_text(self.future_eval, field_name="future_eval")


@dataclass(frozen=True, slots=True)
class TraceabilityManifest:
    """Validated, ordered collection of behavior traceability records."""

    scenario_id: ScenarioId
    scenario_version: ScenarioVersion
    entries: tuple[BehaviorTraceability, ...]

    _by_id: dict[str, BehaviorTraceability] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.entries:
            raise FakeWorldValidationError(
                "traceability manifest must contain at least one entry"
            )
        seen: set[str] = set()
        for entry in self.entries:
            key = str(entry.behavior_id)
            if key in seen:
                raise FakeWorldValidationError(f"duplicate behavior id: {key!r}")
            seen.add(key)
        object.__setattr__(
            self, "_by_id", {str(e.behavior_id): e for e in self.entries}
        )

    def behavior(self, behavior_id: BehaviorId) -> BehaviorTraceability:
        """Return one traceability record or fail closed."""
        try:
            return self._by_id[str(behavior_id)]
        except KeyError as exc:
            raise FakeWorldValidationError(
                f"unknown behavior id: {str(behavior_id)!r}"
            ) from exc

    def validate_refs(self, scenario: FakeWorldScenario) -> None:
        """Verify every behavior reference resolves in the scenario.

        Runs at scenario construction (fail fast) and is also exercised
        directly by traceability tests (T3).
        """
        for entry in self.entries:
            for ref in entry.refs:
                kind, value = split_ref(ref)
                if kind == "route":
                    self._validate_route_ref(scenario, value)
                elif kind in _FORUM_REF_KINDS:
                    self._validate_forum_ref(scenario, kind, value)
                elif kind in _MARKETPLACE_REF_KINDS:
                    self._validate_marketplace_ref(scenario, kind, value)
                elif kind in _LEAK_REF_KINDS:
                    scenario.find_leak_entry(value)
                elif kind in _TRUTH_REF_KINDS:
                    self._validate_truth_ref(scenario, kind, value)
                else:
                    raise FakeWorldValidationError(
                        f"{entry.behavior_id} references unknown kind "
                        f"{kind!r} in {ref!r}"
                    )

    @staticmethod
    def _validate_route_ref(scenario: FakeWorldScenario, path: str) -> None:
        path = path.partition("?")[0]
        static_routes = {
            route for source in scenario.sources for route in source.routes
        }
        if path in static_routes:
            return
        if path.startswith("/old-thread/"):
            scenario.find_thread(thread_id_str=path[len("/old-thread/") :])
            return
        if path.startswith("/legacy/"):
            scenario.find_board(board_id_str=path[len("/legacy/") :])
            return
        for prefix in _DYNAMIC_ROUTE_PREFIXES:
            if path.startswith(prefix):
                value = path[len(prefix) :]
                if "/" in value:
                    raise FakeWorldValidationError(
                        f"route ref {path!r} must embed exactly one id"
                    )
                if prefix == "/thread/":
                    scenario.find_thread(thread_id_str=value)
                elif prefix == "/board/":
                    scenario.find_board(board_id_str=value)
                elif prefix == "/post/":
                    scenario.find_post(post_id_str=value)
                elif prefix == "/attachment/":
                    scenario.find_attachment(attachment_id_str=value)
                elif prefix == "/listing/":
                    scenario.find_listing(value)
                elif prefix == "/seller/":
                    scenario.find_seller(value)
                elif prefix == "/leak/":
                    scenario.find_leak_entry(value)
                return
        raise FakeWorldValidationError(
            f"route ref {path!r} is not a canonical Fake World route"
        )

    def _validate_forum_ref(
        self, scenario: FakeWorldScenario, kind: str, value: str
    ) -> None:
        if kind == "board":
            scenario.find_board(board_id_str=value)
        elif kind == "thread":
            scenario.find_thread(thread_id_str=value)
        elif kind == "post":
            scenario.find_post(post_id_str=value)
        elif kind == "attachment":
            scenario.find_attachment(attachment_id_str=value)
        elif kind == "alias":
            scenario.find_alias(alias_id_str=value)

    @staticmethod
    def _validate_marketplace_ref(
        scenario: FakeWorldScenario, kind: str, value: str
    ) -> None:
        if kind == "listing":
            scenario.find_listing(value)
        elif kind == "seller":
            scenario.find_seller(value)

    def _validate_truth_ref(
        self, scenario: FakeWorldScenario, kind: str, value: str
    ) -> None:
        truth = scenario.truth
        if kind == "actor":
            truth.actor(ActorId(value))
        elif kind == "truthalias":
            truth.alias(AliasId(value))
        elif kind == "organization":
            truth.organization(OrganizationId(value))
        elif kind == "location":
            truth.location(LocationId(value))
        elif kind == "relationship":
            truth.relationship(RelationshipId(value))
        elif kind == "event":
            truth.event(EventId(value))
        else:  # pragma: no cover - membership checked by caller
            raise FakeWorldValidationError(f"unknown truth ref kind {kind!r}")


__all__ = [
    "TEST_ID_RE",
    "BehaviorTraceability",
    "TraceabilityManifest",
]
