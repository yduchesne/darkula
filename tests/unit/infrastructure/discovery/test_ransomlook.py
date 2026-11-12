# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the bounded RansomLook registry adapter (PR 18)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from darkula.app.registry_polling import (
    RegistryAccessDenied,
    RegistryEntryNotFound,
    RegistryRateLimited,
    RegistryRecordConflict,
    RegistrySchemaError,
    RegistryTransportError,
)
from darkula.app.source_discovery import DiscoveryProvider
from darkula.domain.discovery import (
    DiscoveryDetectionMethod,
    DiscoveryInput,
    DiscoveryReferenceKind,
)
from darkula.domain.source import EndpointType
from darkula.infrastructure.discovery.http import RegistryHttpResponse
from darkula.infrastructure.discovery.ransomlook import (
    RansomLookDiscoveryAdapter,
    _parse_retry_after,
)
from darkula.testing.fake_world.source_registry import (
    FakeSourceRegistry,
    default_detail,
    synthetic_sensitive_fields_detail,
)

OBSERVED_AT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


async def _noop_sleep(seconds: float) -> None:
    del seconds


def _adapter(registry: Any, **kwargs: Any) -> RansomLookDiscoveryAdapter:
    kwargs.setdefault("request_interval_seconds", 0.0)
    kwargs.setdefault("sleep", _noop_sleep)
    return RansomLookDiscoveryAdapter(transport=registry, **kwargs)


async def _claims(
    adapter: RansomLookDiscoveryAdapter, name: str
) -> tuple[list[DiscoveryInput], int]:
    provider: DiscoveryProvider = adapter.for_entry(name, observed_at=OBSERVED_AT)
    claims = [claim async for claim in provider.observations()]
    return claims, getattr(provider, "skipped_records", 0)


def _body(document: Any) -> bytes:
    return json.dumps(document).encode("utf-8")


def _index_body(*names: str) -> bytes:
    return _body(list(names))


def _detail_body(
    *, name: str = "blackgate", locations: Sequence[dict[str, Any]] | None = None
) -> bytes:
    return _body(
        [
            {"name": name, "locations": list(locations or [])},
            [],
        ]
    )


class _StaticTransport:
    """Sequential response transport (no real network)."""

    def __init__(self, responses: Sequence[RegistryHttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def get(
        self, path: str, *, timeout_seconds: float, max_response_bytes: int
    ) -> RegistryHttpResponse:
        del timeout_seconds
        self.calls.append(path)
        if not self.responses:
            raise RegistryTransportError("no scripted response")
        response = self.responses.pop(0)
        if len(response.body) > max_response_bytes:
            return response
        return response


class TestIndex:
    """U01/U02: strict, bounded index parsing."""

    @pytest.mark.asyncio
    async def test_accepts_documented_list(self) -> None:
        adapter = _adapter(FakeSourceRegistry())
        assert await adapter.list_entry_ids() == (
            "accessbay",
            "blackgate",
            "nightleak",
            "shadowtalk",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            b'["a", "a"]',
            b'["a", 1]',
            b"[]",
            b'{"next": "page-2"}',
            b"not-json",
            b"\xff",
            b'{"a": 1, "a": 2}',
            b'{"a": NaN}',
            b"[" * 20 + b"]" * 20,
        ],
    )
    async def test_rejects_malformed_index(self, body: bytes) -> None:
        transport = _StaticTransport([RegistryHttpResponse(200, body)])
        adapter = _adapter(transport)
        with pytest.raises(RegistrySchemaError):
            await adapter.list_entry_ids()

    @pytest.mark.asyncio
    async def test_rejects_over_cap_index(self) -> None:
        names = tuple(f"group-{index}" for index in range(2001))
        adapter = _adapter(FakeSourceRegistry(index=names))
        with pytest.raises(RegistrySchemaError):
            await adapter.list_entry_ids()

    @pytest.mark.asyncio
    async def test_rejects_ineligible_cursor_names(self) -> None:
        for name in ("", " padded ", "tab\tname", "x" * 201):
            transport = _StaticTransport([RegistryHttpResponse(200, _index_body(name))])
            adapter = _adapter(transport)
            with pytest.raises(RegistrySchemaError):
                await adapter.list_entry_ids()


class TestDetailProjection:
    """U03-U11/U18: strict safe-field projection only."""

    @pytest.mark.asyncio
    async def test_maps_group_and_locations_not_posts(self) -> None:
        adapter = _adapter(FakeSourceRegistry())
        claims, skipped = await _claims(adapter, "blackgate")
        assert skipped == 0
        assert [claim.reference_kind for claim in claims] == [
            DiscoveryReferenceKind.NAME_ONLY,
            DiscoveryReferenceKind.LOCATOR,
            DiscoveryReferenceKind.NAME_ONLY,
        ]
        assert all(
            claim.detection_method is DiscoveryDetectionMethod.REGISTRY
            for claim in claims
        )
        assert all(claim.provider_id == "ransomlook" for claim in claims)
        assert all(claim.requires_auth_claim is None for claim in claims)
        assert all(claim.evidence_excerpt is None for claim in claims)

    @pytest.mark.asyncio
    async def test_ignores_forbidden_sensitive_fields(self) -> None:
        registry = FakeSourceRegistry(
            details={"blackgate": synthetic_sensitive_fields_detail()}
        )
        claims, skipped = await _claims(_adapter(registry), "blackgate")
        assert skipped == 0
        assert len(claims) == 2
        rendered = repr(claims)
        for forbidden in (
            "synthetic-scraper-header",
            "synthetic-init-script",
            "synthetic-browser",
            "c2NyZWVuc2hvdC1ieXRlcw==",
            "PGh0bWw+bm90LXBlcnNpc3RlZDwvaHRtbD4=",
            "synthetic title",
            "synthetic victim post body",
            "synthetic metadata must never persist",
        ):
            assert forbidden not in rendered

    @pytest.mark.asyncio
    async def test_private_location_is_excluded(self) -> None:
        adapter = _adapter(FakeSourceRegistry())
        claims, skipped = await _claims(adapter, "shadowtalk")
        assert skipped == 1
        assert [claim.asserted_reference for claim in claims] == [
            "shadowtalk",
            "shadowtalk-echo.onion",
        ]

    @pytest.mark.asyncio
    async def test_private_group_yields_no_claims(self) -> None:
        registry = FakeSourceRegistry(
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "private": True,
                        "locations": [{"slug": "https://secret.example.org/"}],
                    },
                    [],
                ]
            }
        )
        claims, skipped = await _claims(_adapter(registry), "blackgate")
        assert claims == []
        assert skipped == 1

    @pytest.mark.asyncio
    async def test_availability_never_affects_admissibility(self) -> None:
        adapter = _adapter(FakeSourceRegistry())
        claims, skipped = await _claims(adapter, "accessbay")
        assert skipped == 0
        location = claims[1]
        assert location.requires_auth_claim is None
        assert location.source_kind_hint is EndpointType.CLEARNET
        assert location.reference_kind is DiscoveryReferenceKind.LOCATOR

    @pytest.mark.asyncio
    async def test_onion_and_clearnet_locator_kinds(self) -> None:
        adapter = _adapter(FakeSourceRegistry())
        blackgate, _ = await _claims(adapter, "blackgate")
        nightleak, _ = await _claims(adapter, "nightleak")
        assert blackgate[1].source_kind_hint is EndpointType.ONION
        assert blackgate[2].reference_kind is DiscoveryReferenceKind.NAME_ONLY
        assert blackgate[2].source_kind_hint is None
        assert all(
            claim.source_kind_hint is EndpointType.CLEARNET for claim in nightleak[1:]
        )

    @pytest.mark.asyncio
    async def test_bare_onion_is_never_schemed(self) -> None:
        adapter = _adapter(FakeSourceRegistry())
        claims, _ = await _claims(adapter, "blackgate")
        assert claims[2].asserted_reference == "blackgate-mirror.onion"
        assert claims[2].reference_kind is DiscoveryReferenceKind.NAME_ONLY

    @pytest.mark.asyncio
    async def test_unsafe_references_are_skipped_without_error_values(self) -> None:
        registry = FakeSourceRegistry(
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {"slug": "ftp://example.org/x"},
                            {"slug": "https://user:pass@example.org/"},
                            {"slug": "https://example.org/a%2Fb"},
                            {"slug": "https://example.org/a%0Ab"},
                            {"slug": "https://example.org/\x01"},
                        ],
                    },
                    [],
                ]
            }
        )
        claims, skipped = await _claims(_adapter(registry), "blackgate")
        assert len(claims) == 1
        assert skipped == 5
        rendered = repr(claims)
        assert "user:pass" not in rendered
        assert "ftp://" not in rendered

    @pytest.mark.asyncio
    async def test_missing_references_are_skipped(self) -> None:
        registry = FakeSourceRegistry(
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {"title": "no reference"},
                            {"slug": "   ", "fqdn": None},
                        ],
                    },
                    [],
                ]
            }
        )
        claims, skipped = await _claims(_adapter(registry), "blackgate")
        assert len(claims) == 1
        assert skipped == 2

    @pytest.mark.asyncio
    async def test_slug_wins_over_fqdn(self) -> None:
        registry = FakeSourceRegistry(
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {
                                "slug": "https://primary.example.org/",
                                "fqdn": "fallback.example.org",
                            }
                        ],
                    },
                    [],
                ]
            }
        )
        claims, skipped = await _claims(_adapter(registry), "blackgate")
        assert skipped == 0
        assert [claim.asserted_reference for claim in claims] == [
            "blackgate",
            "https://primary.example.org/",
        ]
        assert "fallback.example.org" not in repr(claims)

    @pytest.mark.asyncio
    async def test_same_locator_distinct_provider_records(self) -> None:
        registry = FakeSourceRegistry(
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {"slug": "https://Example.org:443/a"},
                            {"slug": "https://example.org/a"},
                        ],
                    },
                    [],
                ]
            }
        )
        claims, _ = await _claims(_adapter(registry), "blackgate")
        locations = claims[1:]
        assert len(locations) == 2
        assert locations[0].provider_record_id != locations[1].provider_record_id


class TestTimestampsAndVersions:
    """U12-U15/U17: deterministic revisions and timestamp handling."""

    @pytest.mark.asyncio
    async def test_aware_timestamp_is_normalized_to_utc(self) -> None:
        registry = FakeSourceRegistry(
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {
                                "slug": "https://example.org/a",
                                "updated": "2026-01-02T03:04:05+02:00",
                            }
                        ],
                    },
                    [],
                ]
            }
        )
        claims, _ = await _claims(_adapter(registry), "blackgate")
        location = claims[1]
        assert location.observed_at == datetime(2026, 1, 2, 1, 4, 5, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_changed_timestamp_creates_new_version(self) -> None:
        def build(updated: str | None) -> Any:
            location: dict[str, Any] = {"slug": "https://example.org/a"}
            if updated is not None:
                location["updated"] = updated
            return [
                {"name": "blackgate", "locations": [location]},
                [],
            ]

        first, _ = await _claims(
            _adapter(FakeSourceRegistry(details={"blackgate": build(None)})),
            "blackgate",
        )
        second, _ = await _claims(
            _adapter(
                FakeSourceRegistry(details={"blackgate": build("2026-02-03T04:05:06Z")})
            ),
            "blackgate",
        )
        assert first[1].provider_record_id == second[1].provider_record_id
        assert first[1].provider_record_version != second[1].provider_record_version

    @pytest.mark.asyncio
    async def test_invalid_timestamp_falls_back_and_is_stable(self) -> None:
        def build(updated: str | None) -> Any:
            location: dict[str, Any] = {"slug": "https://example.org/a"}
            if updated is not None:
                location["updated"] = updated
            return [
                {"name": "blackgate", "locations": [location]},
                [],
            ]

        invalid, _ = await _claims(
            _adapter(FakeSourceRegistry(details={"blackgate": build("nope")})),
            "blackgate",
        )
        naive, _ = await _claims(
            _adapter(
                FakeSourceRegistry(details={"blackgate": build("2026-02-03T04:05:06")})
            ),
            "blackgate",
        )
        absent, _ = await _claims(
            _adapter(FakeSourceRegistry(details={"blackgate": build(None)})),
            "blackgate",
        )
        assert invalid[1].observed_at == OBSERVED_AT
        assert naive[1].observed_at == OBSERVED_AT
        assert invalid[1].provider_record_version == absent[1].provider_record_version
        assert naive[1].provider_record_version == absent[1].provider_record_version

    @pytest.mark.asyncio
    async def test_reordered_keys_produce_identical_identity(self) -> None:
        first_location = {
            "slug": "https://example.org/a",
            "updated": "2026-01-01T00:00:00Z",
        }
        second_location = {
            "updated": "2026-01-01T00:00:00Z",
            "slug": "https://example.org/a",
        }
        first, _ = await _claims(
            _adapter(
                FakeSourceRegistry(
                    details={
                        "blackgate": [
                            {"name": "blackgate", "locations": [first_location]},
                            [],
                        ]
                    }
                )
            ),
            "blackgate",
        )
        second, _ = await _claims(
            _adapter(
                FakeSourceRegistry(
                    details={
                        "blackgate": [
                            {"locations": [second_location], "name": "blackgate"},
                            [],
                        ]
                    }
                )
            ),
            "blackgate",
        )
        assert first[1].provider_record_id == second[1].provider_record_id
        assert first[1].provider_record_version == second[1].provider_record_version

    @pytest.mark.asyncio
    async def test_contradictory_duplicate_identity_fails_group(self) -> None:
        registry = FakeSourceRegistry(
            details={
                "blackgate": [
                    {
                        "name": "blackgate",
                        "locations": [
                            {"slug": "https://example.org/a"},
                            {
                                "slug": "https://example.org/a",
                                "updated": "2026-01-01T00:00:00Z",
                            },
                        ],
                    },
                    [],
                ]
            }
        )
        with pytest.raises(RegistryRecordConflict):
            await _claims(_adapter(registry), "blackgate")


class TestUnsafeDetailEntry:
    """D11/U33: unsafe entry names never become a URL."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name",
        ["a/b", "a\\b", "a?b", "a#b", "a://b", "bad\nname", "x" * 201],
    )
    async def test_unsafe_entry_is_skipped_without_http(self, name: str) -> None:
        registry = FakeSourceRegistry()
        claims, skipped = await _claims(_adapter(registry), name)
        assert claims == []
        assert skipped == 1
        assert registry.calls == []


class TestStatusPolicy:
    """U28-U32: bounded status handling, no redirects/credentials."""

    @staticmethod
    def _transport(*bodies: bytes) -> _StaticTransport:
        return _StaticTransport([RegistryHttpResponse(200, body) for body in bodies])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_access_denied_is_permanent(self, status: int) -> None:
        adapter = _adapter(_StaticTransport([RegistryHttpResponse(status, b"")]))
        with pytest.raises(RegistryAccessDenied):
            await adapter.list_entry_ids()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    async def test_redirects_are_denied(self, status: int) -> None:
        adapter = _adapter(_StaticTransport([RegistryHttpResponse(status, b"")]))
        with pytest.raises(RegistrySchemaError):
            await adapter.list_entry_ids()

    @pytest.mark.asyncio
    async def test_index_404_is_not_treated_as_empty(self) -> None:
        adapter = _adapter(_StaticTransport([RegistryHttpResponse(404, b"")]))
        with pytest.raises(RegistrySchemaError):
            await adapter.list_entry_ids()

    @pytest.mark.asyncio
    async def test_detail_404_is_temporary(self) -> None:
        transport = _StaticTransport(
            [
                RegistryHttpResponse(200, _index_body("blackgate")),
                RegistryHttpResponse(404, b""),
            ]
        )
        adapter = _adapter(transport)
        await adapter.list_entry_ids()
        with pytest.raises(RegistryEntryNotFound):
            await _claims(adapter, "blackgate")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("30", 30),
            ("0", 0),
            ("3600", 3600),
            ("3601", 900),
            ("-1", 900),
            ("999999", 900),
            ("Wed, 21 Oct 2015 07:28:00 GMT", 900),
            (None, 900),
        ],
    )
    async def test_retry_after_is_bounded(
        self, header: str | None, expected: int
    ) -> None:
        assert _parse_retry_after(header) == expected

    @pytest.mark.asyncio
    async def test_rate_limit_carries_bounded_cooldown(self) -> None:
        adapter = _adapter(
            _StaticTransport([RegistryHttpResponse(429, b"", retry_after="30")])
        )
        with pytest.raises(RegistryRateLimited) as error:
            await adapter.list_entry_ids()
        assert error.value.retry_after_seconds == 30

    @pytest.mark.asyncio
    async def test_retryable_status_is_retried_once(self) -> None:
        transport = _StaticTransport(
            [
                RegistryHttpResponse(503, b""),
                RegistryHttpResponse(200, _index_body("blackgate")),
            ]
        )
        adapter = _adapter(transport)
        assert await adapter.list_entry_ids() == ("blackgate",)
        assert len(transport.calls) == 2

    @pytest.mark.asyncio
    async def test_second_retryable_status_gives_up(self) -> None:
        transport = _StaticTransport(
            [
                RegistryHttpResponse(503, b""),
                RegistryHttpResponse(503, b""),
            ]
        )
        adapter = _adapter(transport)
        with pytest.raises(RegistryTransportError):
            await adapter.list_entry_ids()
        assert len(transport.calls) == 2

    @pytest.mark.asyncio
    async def test_unsupported_content_encoding_is_rejected(self) -> None:
        adapter = _adapter(
            _StaticTransport(
                [RegistryHttpResponse(200, b"[]", content_encoding="gzip")]
            )
        )
        with pytest.raises(RegistrySchemaError):
            await adapter.list_entry_ids()

    @pytest.mark.asyncio
    async def test_adapter_enforces_response_size(self) -> None:
        huge = b'["' + b"a" * 100 + b'"]'
        adapter = _adapter(
            _StaticTransport([RegistryHttpResponse(200, huge)]),
            max_response_bytes=32,
        )
        with pytest.raises(RegistrySchemaError):
            await adapter.list_entry_ids()

    @pytest.mark.asyncio
    async def test_error_messages_do_not_echo_provider_values(self) -> None:
        hostile = "hostile-secret-value"
        transport = _StaticTransport(
            [RegistryHttpResponse(200, _index_body(hostile, hostile))]
        )
        adapter = _adapter(transport)
        with pytest.raises(RegistrySchemaError) as error:
            await adapter.list_entry_ids()
        assert hostile not in str(error.value)

        body = b"synthetic-hostile-body"
        adapter = _adapter(_StaticTransport([RegistryHttpResponse(400, body)]))
        with pytest.raises(RegistrySchemaError) as error:
            await adapter.list_entry_ids()
        assert "synthetic-hostile-body" not in str(error.value)


class TestProviderShape:
    """U03/U12: exactly the documented two-element detail shape."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "document",
        [
            {"group": {}, "posts": []},
            [{}, []],
            [{}, {}, []],
            [{"name": "blackgate"}, {}],
            [{"name": "blackgate"}, []][0],
        ],
    )
    async def test_rejects_other_detail_shapes(self, document: Any) -> None:
        registry = FakeSourceRegistry(details={"blackgate": document})
        with pytest.raises(RegistrySchemaError):
            await _claims(_adapter(registry), "blackgate")

    @pytest.mark.asyncio
    async def test_mismatched_group_name_is_a_schema_error(self) -> None:
        registry = FakeSourceRegistry(
            details={"blackgate": [{"name": "other", "locations": []}, []]}
        )
        with pytest.raises(RegistrySchemaError):
            await _claims(_adapter(registry), "blackgate")

    @pytest.mark.asyncio
    async def test_too_many_locations_is_a_schema_error(self) -> None:
        locations = [{"slug": f"https://example.org/{index}"} for index in range(65)]
        registry = FakeSourceRegistry(
            details={"blackgate": [{"name": "blackgate", "locations": locations}, []]}
        )
        with pytest.raises(RegistrySchemaError):
            await _claims(_adapter(registry), "blackgate")

    @pytest.mark.asyncio
    async def test_empty_locations_is_valid(self) -> None:
        registry = FakeSourceRegistry(
            details={"blackgate": [{"name": "blackgate", "locations": []}, []]}
        )
        claims, skipped = await _claims(_adapter(registry), "blackgate")
        assert len(claims) == 1
        assert claims[0].reference_kind is DiscoveryReferenceKind.NAME_ONLY
        assert skipped == 0

    @pytest.mark.asyncio
    async def test_group_name_pr17_invalid_still_processes_locations(self) -> None:
        hostile_name = "token: secret-like group"
        # The index validator accepts this cursor-eligible name; PR 17
        # name-only validation rejects the secret-like markers.
        registry = FakeSourceRegistry(
            index=(hostile_name,),
            details={
                hostile_name: [
                    {
                        "name": hostile_name,
                        "locations": [{"slug": "https://example.org/a"}],
                    },
                    [],
                ]
            },
        )
        claims, skipped = await _claims(_adapter(registry), hostile_name)
        assert claims == []
        assert skipped >= 1
        assert "secret-like" not in repr(claims)

    def test_default_detail_is_synthetic(self) -> None:
        for name in ("accessbay", "blackgate", "nightleak", "shadowtalk"):
            assert isinstance(default_detail(name), list)
