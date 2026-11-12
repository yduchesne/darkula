# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the deterministic Fake World source registry (PR 18)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from darkula.app.registry_polling import RegistryRateLimited
from darkula.infrastructure.discovery.ransomlook import RansomLookDiscoveryAdapter
from darkula.testing.fake_world.source_registry import (
    FAKE_REGISTRY_INDEX,
    REQUEST_CLASS_DETAIL,
    REQUEST_CLASS_INDEX,
    FakeSourceRegistry,
    ScriptedRegistryResponse,
    synthetic_sensitive_fields_detail,
)

MOMENT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


async def _noop_sleep(seconds: float) -> None:
    del seconds


def _adapter(registry: FakeSourceRegistry) -> RansomLookDiscoveryAdapter:
    return RansomLookDiscoveryAdapter(
        transport=registry, request_interval_seconds=0, sleep=_noop_sleep
    )


class TestFakeRegistryFixtures:
    """U37: exactly four synthetic entries and deterministic responses."""

    def test_canonical_index_is_four_synthetic_entries(self) -> None:
        assert FAKE_REGISTRY_INDEX == (
            "accessbay",
            "blackgate",
            "nightleak",
            "shadowtalk",
        )
        assert list(FAKE_REGISTRY_INDEX) == sorted(
            FAKE_REGISTRY_INDEX, key=lambda name: name.encode("utf-8")
        )

    @pytest.mark.asyncio
    async def test_index_and_detail_have_production_shapes(self) -> None:
        registry = FakeSourceRegistry()
        adapter = _adapter(registry)
        assert await adapter.list_entry_ids() == FAKE_REGISTRY_INDEX

        provider = adapter.for_entry("blackgate", observed_at=MOMENT)
        claims = [claim async for claim in provider.observations()]
        assert claims
        assert [call.request_class for call in registry.calls] == [
            REQUEST_CLASS_INDEX,
            REQUEST_CLASS_DETAIL,
        ]

    @pytest.mark.asyncio
    async def test_responses_are_deterministic(self) -> None:
        first = FakeSourceRegistry()
        second = FakeSourceRegistry()
        first_response = await first.get(
            "/api/groups", timeout_seconds=1, max_response_bytes=1_000_000
        )
        second_response = await second.get(
            "/api/groups", timeout_seconds=1, max_response_bytes=1_000_000
        )
        assert first_response.body == second_response.body
        assert json.loads(first_response.body) == list(FAKE_REGISTRY_INDEX)

    def test_registry_is_get_only(self) -> None:
        registry = FakeSourceRegistry()
        assert hasattr(registry, "get")
        for forbidden in ("post", "put", "delete", "patch", "request"):
            assert not hasattr(registry, forbidden)

    @pytest.mark.asyncio
    async def test_scripted_failures_are_consumed_in_order(self) -> None:
        registry = FakeSourceRegistry(
            scripted=[
                ScriptedRegistryResponse(429, retry_after="30"),
                ScriptedRegistryResponse(503),
                ScriptedRegistryResponse(200, body=b'\n["accessbay"]\n'),
            ]
        )
        adapter = _adapter(registry)
        with pytest.raises(RegistryRateLimited):
            await adapter.list_entry_ids()
        # 503 then 200: exactly one retry.
        assert await adapter.list_entry_ids() == ("accessbay",)

    def test_sensitive_fields_fixture_is_synthetic(self) -> None:
        document = synthetic_sensitive_fields_detail()
        rendered = repr(document)
        assert "synthetic" in rendered
        assert "c2NyZWVuc2hvdC1ieXRlcw==" in rendered
        assert len(document) == 2
