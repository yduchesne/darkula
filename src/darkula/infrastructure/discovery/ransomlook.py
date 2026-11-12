# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded RansomLook registry adapter (PR 18).

Maps the two documented, anonymous, read-only public RansomLook paths to the
existing PR 17 :class:`~darkula.domain.discovery.DiscoveryInput` contract:

- ``GET /api/groups`` -> a bounded ``list[str]`` of group names;
- ``GET /api/group/<encoded name>`` -> exactly ``[group_object, posts]``.

The adapter never downloads a discovered locator, follows a redirect, uses
credentials/cookies, or reads scraper/screenshot fields. Provider data are
untrusted claims, never authorization.

Attribution: RansomLook registry data are made available by their
contributors under CC BY 4.0; Darkula observations are third-party claims,
not independently verified facts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from darkula.app.registry_polling import (
    DEFAULT_DEFER_SECONDS,
    RETRY_DELAY_SECONDS,
    RegistryAccessDenied,
    RegistryEntryNotFound,
    RegistryRateLimited,
    RegistryRecordConflict,
    RegistryRecordSkipReason,
    RegistrySchemaError,
    RegistryTransportError,
)
from darkula.app.source_discovery import DiscoveryProvider
from darkula.domain.discovery import (
    DiscoveryDetectionMethod,
    DiscoveryInput,
    DiscoveryReferenceKind,
    normalize_discovery_locator,
)
from darkula.domain.registry_polling import validate_registry_cursor
from darkula.domain.source import EndpointType
from darkula.infrastructure.discovery.http import (
    RegistryHttpTransport,
    validate_relative_path,
)

#: Stable provider identity persisted on every observation.
RANSOMLOOK_PROVIDER_ID = "ransomlook"
#: Public, attribution-safe origin used in evidence strings (never a fetch).
RANSOMLOOK_PUBLIC_ORIGIN = "https://www.ransomlook.io"
RANSOMLOOK_INDEX_PATH = "/api/groups"
RANSOMLOOK_DETAIL_PREFIX = "/api/group/"

#: Bounded limits (fixed hard caps unless injected from validated settings).
MAX_INDEX_ENTRIES = 2000
MAX_LOCATIONS_PER_GROUP = 64
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 16
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})

_CONTROL_CHARS = frozenset(chr(code) for code in (*range(0x00, 0x20), 0x7F))
_DETAIL_FORBIDDEN = frozenset("/\\?#")


def _canonical_digest(document: Any) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace, ASCII)."""
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _length_prefixed_digest(components: tuple[str, ...]) -> str:
    """SHA-256 over UTF-8, 4-byte big-endian length-prefixed components."""
    digest = hashlib.sha256()
    for component in components:
        encoded = component.encode("utf-8")
        if len(encoded) >= 2**32:
            raise ValueError("registry identity component is too large")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _group_digest(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON object key")
        document[key] = value
    return document


def _reject_constant(value: str) -> Any:
    raise ValueError(f"unsupported JSON constant {value}")


def _check_depth(document: Any, *, depth: int = 1) -> None:
    if depth > MAX_JSON_DEPTH:
        raise RegistrySchemaError("registry JSON nesting exceeds the bound")
    if isinstance(document, dict):
        for value in document.values():
            _check_depth(value, depth=depth + 1)
    elif isinstance(document, list):
        for value in document:
            _check_depth(value, depth=depth + 1)


def _reject_unsupported_encoding(encoding: str | None) -> None:
    if encoding is None:
        return
    if encoding.strip().lower() in ("", "identity"):
        return
    raise RegistrySchemaError("registry response used an unsupported encoding")


def _parse_json(body: bytes) -> Any:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistrySchemaError("registry response is not valid UTF-8") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise RegistrySchemaError("registry response is not valid JSON") from exc
    _check_depth(document)
    return document


def _parse_retry_after(value: str | None) -> int:
    """Return a bounded numeric Retry-After, or the fixed 900s fallback."""
    if value is None:
        return DEFAULT_DEFER_SECONDS
    candidate = value.strip()
    if not candidate.isdigit():
        return DEFAULT_DEFER_SECONDS
    seconds = int(candidate)
    if seconds < 0 or seconds > 3600:
        return DEFAULT_DEFER_SECONDS
    return seconds


def _parse_provider_time(value: Any) -> tuple[datetime, str] | None:
    """Return ``(utc_datetime, normalized_iso)`` for a valid aware ISO time."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    utc_value = parsed.astimezone(UTC)
    return utc_value, utc_value.isoformat()


def _safe_reference(value: Any) -> str | None:
    """Return one safe nonblank reference string, or ``None``."""
    if not isinstance(value, str):
        return None
    if not value or not value.strip():
        return None
    if value != value.strip():
        return None
    if any(character in _CONTROL_CHARS for character in value):
        return None
    return value


def _detail_name_is_safe(name: str) -> bool:
    if not name or len(name) > 200:
        return False
    if name != name.strip():
        return False
    if "://" in name:
        return False
    if any(character in _DETAIL_FORBIDDEN for character in name):
        return False
    return not any(character in _CONTROL_CHARS for character in name)


class RansomLookGroupProvider(DiscoveryProvider):
    """One selected group's bounded PR 17 claims (lazy, one detail GET)."""

    def __init__(
        self,
        adapter: RansomLookDiscoveryAdapter,
        entry_id: str,
        observed_at: datetime,
    ) -> None:
        self._adapter = adapter
        self._entry_id = entry_id
        self._observed_at = observed_at
        self._skipped = 0

    @property
    def skipped_records(self) -> int:
        return self._skipped

    def _skip(self, reason: RegistryRecordSkipReason) -> None:
        del reason  # bounded reason code is counted, never logged/widened
        self._skipped += 1

    async def observations(self) -> AsyncIterator[DiscoveryInput]:
        self._skipped = 0
        if not _detail_name_is_safe(self._entry_id):
            self._skip(RegistryRecordSkipReason.INVALID_REFERENCE)
            return
        path = RANSOMLOOK_DETAIL_PREFIX + quote(self._entry_id, safe="")
        document = await self._adapter._fetch_json(path, request_class="detail")
        group, _posts = self._adapter._parse_detail(document)
        if group.get("private") is True:
            self._skip(RegistryRecordSkipReason.PRIVATE)
            return
        name = group.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise RegistrySchemaError("registry group name is not a string")
            if name.casefold() != self._entry_id.casefold():
                raise RegistrySchemaError(
                    "registry group name does not match the selected index entry"
                )
        locations = group.get("locations")
        if not isinstance(locations, list):
            raise RegistrySchemaError("registry group locations are not a list")
        if len(locations) > self._adapter.max_locations_per_group:
            raise RegistrySchemaError("registry group has too many locations")

        group_digest = _group_digest(self._entry_id)
        origin_reference = (
            RANSOMLOOK_PUBLIC_ORIGIN
            + RANSOMLOOK_DETAIL_PREFIX
            + quote(self._entry_id, safe="")
        )
        group_evidence = "registry://ransomlook/group/" + group_digest
        group_claim = self._group_claim(
            group_digest=group_digest,
            origin_reference=origin_reference,
            evidence_reference=group_evidence,
        )
        if group_claim is not None:
            yield group_claim

        seen: dict[str, str] = {}
        for location in locations:
            claim = self._location_claim(
                location,
                group_digest=group_digest,
                origin_reference=origin_reference,
                seen=seen,
            )
            if claim is not None:
                yield claim

    def _group_claim(
        self,
        *,
        group_digest: str,
        origin_reference: str,
        evidence_reference: str,
    ) -> DiscoveryInput | None:
        version = _canonical_digest(
            {
                "kind": "group",
                "name": self._entry_id,
                "asserted_reference": self._entry_id,
                "evidence_reference": evidence_reference,
                "origin_reference": origin_reference,
            }
        )
        try:
            return DiscoveryInput(
                provider_id=self._adapter.provider_id,
                provider_record_id="group:" + group_digest,
                provider_record_version=version,
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=DiscoveryReferenceKind.NAME_ONLY,
                asserted_reference=self._entry_id,
                observed_at=self._observed_at,
                evidence_reference=evidence_reference,
                source_kind_hint=None,
                requires_auth_claim=None,
                origin_reference=origin_reference,
            )
        except ValueError:
            self._skip(RegistryRecordSkipReason.PR17_VALIDATION)
            return None

    def _location_claim(
        self,
        location: Any,
        *,
        group_digest: str,
        origin_reference: str,
        seen: dict[str, str],
    ) -> DiscoveryInput | None:
        if not isinstance(location, dict):
            self._skip(RegistryRecordSkipReason.INVALID_SCHEMA)
            return None
        if location.get("private") is True:
            self._skip(RegistryRecordSkipReason.PRIVATE)
            return None
        slug_value = location.get("slug")
        fqdn_value = location.get("fqdn")
        slug = _safe_reference(slug_value)
        fqdn = _safe_reference(fqdn_value)
        if slug is not None:
            chosen = slug
        elif fqdn is not None:
            chosen = fqdn
        else:
            missing = all(
                value is None or (isinstance(value, str) and not value.strip())
                for value in (slug_value, fqdn_value)
            )
            self._skip(
                RegistryRecordSkipReason.MISSING_REFERENCE
                if missing
                else RegistryRecordSkipReason.INVALID_REFERENCE
            )
            return None

        if "://" in chosen:
            try:
                normalize_discovery_locator(chosen)
            except ValueError:
                self._skip(RegistryRecordSkipReason.INVALID_REFERENCE)
                return None
            reference_kind = DiscoveryReferenceKind.LOCATOR
            host = urlsplit(chosen).hostname or ""
            source_kind_hint = (
                EndpointType.ONION if host.endswith(".onion") else EndpointType.CLEARNET
            )
        else:
            reference_kind = DiscoveryReferenceKind.NAME_ONLY
            source_kind_hint = None

        provider_time = _parse_provider_time(location.get("updated"))
        observed_at = (
            provider_time[0] if provider_time is not None else self._observed_at
        )

        record_digest = _length_prefixed_digest((self._entry_id, chosen))
        evidence_reference = "registry://ransomlook/location/" + record_digest
        projection: dict[str, Any] = {
            "kind": "location",
            "group_record_digest": group_digest,
            "reference_kind": reference_kind.value,
            "asserted_reference": chosen,
            "source_kind_hint": (
                None if source_kind_hint is None else source_kind_hint.value
            ),
            "requires_auth_claim": None,
            "origin_reference": origin_reference,
            "evidence_reference": evidence_reference,
        }
        if provider_time is not None:
            projection["provider_updated"] = provider_time[1]
        version = _canonical_digest(projection)

        record_id = "location:" + record_digest
        previous = seen.get(record_id)
        if previous is not None:
            if previous != version:
                raise RegistryRecordConflict(
                    "one registry group carries contradictory duplicate records"
                )
            return None
        seen[record_id] = version

        try:
            return DiscoveryInput(
                provider_id=self._adapter.provider_id,
                provider_record_id=record_id,
                provider_record_version=version,
                detection_method=DiscoveryDetectionMethod.REGISTRY,
                reference_kind=reference_kind,
                asserted_reference=chosen,
                observed_at=observed_at,
                evidence_reference=evidence_reference,
                source_kind_hint=source_kind_hint,
                requires_auth_claim=None,
                origin_reference=origin_reference,
            )
        except ValueError:
            self._skip(RegistryRecordSkipReason.PR17_VALIDATION)
            return None


class RansomLookDiscoveryAdapter:
    """Provider-neutral registry adapter over the bounded transport."""

    provider_id = RANSOMLOOK_PROVIDER_ID

    def __init__(
        self,
        *,
        transport: RegistryHttpTransport,
        max_index_entries: int = MAX_INDEX_ENTRIES,
        max_locations_per_group: int = MAX_LOCATIONS_PER_GROUP,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        request_timeout_seconds: float = 8.0,
        request_interval_seconds: float = 2.0,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        if max_index_entries < 1 or max_index_entries > MAX_INDEX_ENTRIES:
            raise ValueError("max_index_entries is out of bounds")
        if max_locations_per_group < 1 or max_locations_per_group > (
            MAX_LOCATIONS_PER_GROUP
        ):
            raise ValueError("max_locations_per_group is out of bounds")
        if max_response_bytes < 1 or max_response_bytes > MAX_RESPONSE_BYTES:
            raise ValueError("max_response_bytes is out of bounds")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must be >= 0")
        self._transport = transport
        self.max_locations_per_group = max_locations_per_group
        self._max_index_entries = max_index_entries
        self._max_response_bytes = max_response_bytes
        self._request_timeout_seconds = request_timeout_seconds
        self._request_interval_seconds = request_interval_seconds
        self._logical_requests = 0
        self._sleep = sleep

    async def _wait(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
            return
        import asyncio

        await asyncio.sleep(seconds)

    async def _sleep_before_retry(self) -> None:
        await self._wait(RETRY_DELAY_SECONDS)

    async def _response(self, path: str, *, request_class: str) -> Any:
        validate_relative_path(path)
        attempt = 0
        while True:
            try:
                response = await self._transport.get(
                    path,
                    timeout_seconds=self._request_timeout_seconds,
                    max_response_bytes=self._max_response_bytes,
                )
            except RegistryTransportError:
                if attempt == 0:
                    attempt = 1
                    await self._sleep_before_retry()
                    continue
                raise
            if response.status_code in RETRYABLE_STATUSES:
                if attempt == 0:
                    attempt = 1
                    await self._sleep_before_retry()
                    continue
                raise RegistryTransportError(
                    "registry provider is temporarily unavailable"
                )
            _reject_unsupported_encoding(response.content_encoding)
            if len(response.body) > self._max_response_bytes:
                raise RegistrySchemaError("registry response exceeds the bounded size")
            if response.status_code == 200:
                return response
            if response.status_code == 429:
                raise RegistryRateLimited(_parse_retry_after(response.retry_after))
            if response.status_code in (401, 403):
                raise RegistryAccessDenied(
                    "registry provider denied anonymous read access"
                )
            if response.status_code in (301, 302, 303, 307, 308):
                raise RegistrySchemaError("registry redirect is denied")
            if response.status_code == 404 and request_class == "detail":
                raise RegistryEntryNotFound(
                    "registry entry is listed but temporarily unavailable"
                )
            raise RegistrySchemaError(
                "registry provider returned an unexpected response status"
            )

    async def _fetch_json(self, path: str, *, request_class: str) -> Any:
        if self._logical_requests > 0 and self._request_interval_seconds > 0:
            await self._wait(self._request_interval_seconds)
        self._logical_requests += 1
        response = await self._response(path, request_class=request_class)
        return _parse_json(response.body)

    async def list_entry_ids(self) -> tuple[str, ...]:
        document = await self._fetch_json(RANSOMLOOK_INDEX_PATH, request_class="index")
        if not isinstance(document, list):
            raise RegistrySchemaError("registry index is not a list")
        if not 1 <= len(document) <= self._max_index_entries:
            raise RegistrySchemaError("registry index has an invalid entry count")
        names: list[str] = []
        seen: set[str] = set()
        for item in document:
            if not isinstance(item, str):
                raise RegistrySchemaError("registry index contains a non-string entry")
            try:
                validate_registry_cursor(item)
            except ValueError as exc:
                raise RegistrySchemaError(
                    "registry index contains an ineligible entry name"
                ) from exc
            if item in seen:
                raise RegistrySchemaError("registry index contains a duplicate entry")
            seen.add(item)
            names.append(item)
        return tuple(names)

    def _parse_detail(self, document: Any) -> tuple[dict[str, Any], list[Any]]:
        if not isinstance(document, list) or len(document) != 2:
            raise RegistrySchemaError("registry detail is not a two-element list")
        group, posts = document
        if not isinstance(group, dict):
            raise RegistrySchemaError("registry group object is not an object")
        if not isinstance(posts, list):
            raise RegistrySchemaError("registry posts field is not a list")
        return group, posts

    def for_entry(self, entry_id: str, *, observed_at: datetime) -> DiscoveryProvider:
        return RansomLookGroupProvider(self, entry_id, observed_at)


__all__ = [
    "MAX_INDEX_ENTRIES",
    "MAX_JSON_DEPTH",
    "MAX_LOCATIONS_PER_GROUP",
    "MAX_RESPONSE_BYTES",
    "RANSOMLOOK_DETAIL_PREFIX",
    "RANSOMLOOK_INDEX_PATH",
    "RANSOMLOOK_PROVIDER_ID",
    "RANSOMLOOK_PUBLIC_ORIGIN",
    "RansomLookDiscoveryAdapter",
    "RansomLookGroupProvider",
]
