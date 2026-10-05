# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula deterministic observable extractors (PR 11).

One Darkula-owned extractor contract plus the fixed
``deterministic-observables/v1`` profile. Every extractor is **pure**: no
I/O, database, ObjectStore, LLM, wall clock, randomness, network, DNS,
WHOIS/RDAP, geocoding, or provider dependency. The same canonical text
always yields the same matches in a deterministic order.

Matching is deliberately conservative and bounded: a value is recognized
only at an exact span of canonical text, normalization is explicit, and
trailing prose punctuation is excluded rather than absorbed. No relationship,
reputation, geographic, or semantic/LLM reasoning occurs here (PR 12-14).

IDNA/Unicode behavior (v1): only ASCII labels (including ASCII ``xn--``
A-labels) are recognized; Unicode labels are neither converted nor extracted
consequently (a correct conversion would require an unapproved policy/
dependency decision). This is a documented, bounded v1 behavior.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass

from darkula.domain.extraction import (
    DETERMINISTIC_OBSERVABLES_PROFILE_NAME,
    DETERMINISTIC_OBSERVABLES_PROFILE_VERSION,
    EntityType,
    ExtractorIdentity,
    HashSubtype,
    SourceSpan,
)

# -- shared label/domain grammar (ASCII A-label form) ---------------------
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_TLD = r"(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})"
_DOMAIN_RE = re.compile(rf"(?<![\w-])(?:{_LABEL}\.)+{_TLD}(?![\w@-])")

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])"
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@"
    rf"(?:{_LABEL}\.)+{_TLD}"
    r"(?![\w@-])"
)

_URL_RE = re.compile(r"(?<![\w])https?://[^\s<>\"']+", re.IGNORECASE)

_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
# Candidate run of hex/colon/dot that contains at least one colon; validated
# structurally by ``ipaddress`` below (malformed candidates are rejected).
_IPV6_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Fa-f:.])(?=[0-9A-Fa-f:.]*:)[0-9A-Fa-f:.]{2,}(?![0-9A-Fa-f:.])"
)

_HASH_RE = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})(?![0-9A-Fa-f])"
)

_URL_TRAILING = ".,;:!?"
_URL_BRACKETS: tuple[tuple[str, str], ...] = ((")", "("), ("]", "["), ("}", "{"))

_MAX_TOTAL_EMAIL_LENGTH = 254
_MAX_LOCAL_PART_LENGTH = 64
_MAX_DOMAIN_LENGTH = 253


@dataclass(frozen=True, slots=True)
class EntityMatch:
    """One deterministic extractor match (no persistence identity yet).

    ``raw_value`` equals the exact canonical-text slice ``text[start:end]``;
    ``normalized_value`` is the v1 normalized form. ``subtype`` is bounded
    extractor-owned metadata (currently only the ``HASH`` algorithm).
    """

    entity_type: EntityType
    raw_value: str
    normalized_value: str
    source_span: SourceSpan
    extractor: ExtractorIdentity
    subtype: str | None = None


class DeterministicEntityExtractor(ABC):
    """One deterministic, pure entity-extraction capability."""

    #: Bounded developer-controlled extractor name (for example ``ip``).
    NAME: str = ""
    #: Bounded extractor semantic version (for example ``v1``).
    VERSION: str = "v1"

    @property
    def name(self) -> str:
        """Return the bounded logical extractor name."""
        return self.NAME

    @property
    def version(self) -> str:
        """Return the extractor semantic version."""
        return self.VERSION

    @property
    def identity(self) -> ExtractorIdentity:
        """Return this extractor's bounded identity."""
        return ExtractorIdentity(name=self.NAME, version=self.VERSION)

    @abstractmethod
    def extract(self, text: str) -> tuple[EntityMatch, ...]:
        """Return deterministic matches over ``text`` (pure, no I/O)."""
        raise NotImplementedError


def _match(
    *,
    entity_type: EntityType,
    raw_value: str,
    normalized_value: str,
    start: int,
    end: int,
    identity: ExtractorIdentity,
    subtype: str | None = None,
) -> EntityMatch:
    """Build one :class:`EntityMatch` with an exact span."""
    return EntityMatch(
        entity_type=entity_type,
        raw_value=raw_value,
        normalized_value=normalized_value,
        source_span=SourceSpan(start=start, end=end),
        extractor=identity,
        subtype=subtype,
    )


class IpAddressExtractor(DeterministicEntityExtractor):
    """Syntactically valid IPv4/IPv6 addresses via stdlib ``ipaddress``.

    Private/reserved addresses are extracted syntactically; no
    reputation/routability classification is performed. Malformed/embedded
    forms and overlong candidates are rejected.
    """

    NAME = "ip"

    def extract(self, text: str) -> tuple[EntityMatch, ...]:
        matches: list[EntityMatch] = []
        identity = self.identity
        for regex in (_IPV4_RE, _IPV6_CANDIDATE_RE):
            for candidate in regex.finditer(text):
                raw = candidate.group(0)
                if "%" in raw:
                    continue
                try:
                    parsed = ipaddress.ip_address(raw)
                except ValueError:
                    continue
                matches.append(
                    _match(
                        entity_type=EntityType.IP_ADDRESS,
                        raw_value=raw,
                        normalized_value=str(parsed),
                        start=candidate.start(),
                        end=candidate.end(),
                        identity=identity,
                    )
                )
        return tuple(matches)


def _clean_url(raw: str) -> str:
    """Return ``raw`` without trailing prose punctuation/brackets."""
    cleaned = raw
    while cleaned and cleaned[-1] in _URL_TRAILING:
        cleaned = cleaned[:-1]
    changed = True
    while changed:
        changed = False
        for close, open_ in _URL_BRACKETS:
            if cleaned.endswith(close) and cleaned.count(close) > cleaned.count(open_):
                cleaned = cleaned[:-1]
                changed = True
    return cleaned


def _plausible_host(host: str) -> bool:
    """Return whether a URL host is a plausible domain or IP literal."""
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True
    return host == "localhost" or "." in host


class UrlExtractor(DeterministicEntityExtractor):
    """Explicit absolute ``http``/``https`` URLs, normalized conservatively.

    Scheme/hostname are lowercased; path/query/fragment and an explicit port
    are preserved. URLs containing userinfo/credentials are rejected (never
    persisted verbatim). No network fetch occurs.
    """

    NAME = "url"

    def extract(self, text: str) -> tuple[EntityMatch, ...]:
        matches: list[EntityMatch] = []
        identity = self.identity
        for candidate in _URL_RE.finditer(text):
            cleaned = _clean_url(candidate.group(0))
            if not cleaned:
                continue
            try:
                parsed = urllib.parse.urlsplit(cleaned)
            except ValueError:
                continue
            scheme = parsed.scheme.lower()
            if scheme not in ("http", "https"):
                continue
            if "@" in parsed.netloc or parsed.username is not None:
                continue
            host = parsed.hostname
            if host is None or not _plausible_host(host):
                continue
            try:
                port = parsed.port
            except ValueError:
                continue
            host_out = f"[{host}]" if ":" in host else host
            netloc = host_out if port is None else f"{host_out}:{port}"
            normalized = urllib.parse.urlunsplit(
                (scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )
            matches.append(
                _match(
                    entity_type=EntityType.URL,
                    raw_value=cleaned,
                    normalized_value=normalized,
                    start=candidate.start(),
                    end=candidate.start() + len(cleaned),
                    identity=identity,
                )
            )
        return tuple(matches)


class EmailExtractor(DeterministicEntityExtractor):
    """Bounded practical mailbox syntax (not full RFC 5322 parsing).

    The local part case is preserved and the domain is lowercased. No
    network validation occurs. Overlong/whitespace/control-bearing tokens are
    rejected.
    """

    NAME = "email"

    def extract(self, text: str) -> tuple[EntityMatch, ...]:
        matches: list[EntityMatch] = []
        identity = self.identity
        for candidate in _EMAIL_RE.finditer(text):
            raw = candidate.group(0)
            local, sep, domain = raw.rpartition("@")
            if not sep:
                continue
            if len(raw) > _MAX_TOTAL_EMAIL_LENGTH:
                continue
            if len(local) > _MAX_LOCAL_PART_LENGTH:
                continue
            if len(domain) > _MAX_DOMAIN_LENGTH:
                continue
            matches.append(
                _match(
                    entity_type=EntityType.EMAIL,
                    raw_value=raw,
                    normalized_value=f"{local}@{domain.lower()}",
                    start=candidate.start(),
                    end=candidate.end(),
                    identity=identity,
                )
            )
        return tuple(matches)


class DomainExtractor(DeterministicEntityExtractor):
    """Syntactically plausible ASCII DNS domains, normalized lowercase.

    IPv4/dotted-numeric garbage, malformed labels, and leading/trailing
    hyphens are rejected by the grammar. No parent domains are synthesized;
    only exact source-text occurrences are emitted.
    """

    NAME = "domain"

    def extract(self, text: str) -> tuple[EntityMatch, ...]:
        matches: list[EntityMatch] = []
        identity = self.identity
        for candidate in _DOMAIN_RE.finditer(text):
            raw = candidate.group(0)
            if len(raw) > _MAX_DOMAIN_LENGTH:
                continue
            matches.append(
                _match(
                    entity_type=EntityType.DOMAIN,
                    raw_value=raw,
                    normalized_value=raw.lower(),
                    start=candidate.start(),
                    end=candidate.end(),
                    identity=identity,
                )
            )
        return tuple(matches)


def _hash_subtype(length: int) -> HashSubtype:
    """Return the bounded hash algorithm for a hex digest length."""
    if length == 32:
        return HashSubtype.MD5
    if length == 40:
        return HashSubtype.SHA1
    return HashSubtype.SHA256


class HashExtractor(DeterministicEntityExtractor):
    """Exact hexadecimal MD5/SHA-1/SHA-256 digests, normalized lowercase.

    A digest embedded in a longer hex string is rejected. The algorithm is
    carried as a bounded ``subtype``.
    """

    NAME = "hash"

    def extract(self, text: str) -> tuple[EntityMatch, ...]:
        matches: list[EntityMatch] = []
        identity = self.identity
        for candidate in _HASH_RE.finditer(text):
            raw = candidate.group(0)
            matches.append(
                _match(
                    entity_type=EntityType.HASH,
                    raw_value=raw,
                    normalized_value=raw.lower(),
                    start=candidate.start(),
                    end=candidate.end(),
                    identity=identity,
                    subtype=_hash_subtype(len(raw)).value,
                )
            )
        return tuple(matches)


def deterministic_observables_extractors() -> tuple[DeterministicEntityExtractor, ...]:
    """Return the fixed deterministic-observables/v1 extractor set.

    Ordered by extractor name so the persisted manifest is deterministic.
    """
    extractors: tuple[DeterministicEntityExtractor, ...] = (
        DomainExtractor(),
        EmailExtractor(),
        HashExtractor(),
        IpAddressExtractor(),
        UrlExtractor(),
    )
    return tuple(sorted(extractors, key=lambda item: item.name))


def deterministic_observables_manifest() -> tuple[ExtractorIdentity, ...]:
    """Return the exact deterministic-observables/v1 extractor manifest."""
    return tuple(item.identity for item in deterministic_observables_extractors())


__all__ = [
    "DETERMINISTIC_OBSERVABLES_PROFILE_NAME",
    "DETERMINISTIC_OBSERVABLES_PROFILE_VERSION",
    "DeterministicEntityExtractor",
    "DomainExtractor",
    "EmailExtractor",
    "EntityMatch",
    "HashExtractor",
    "IpAddressExtractor",
    "UrlExtractor",
    "deterministic_observables_extractors",
    "deterministic_observables_manifest",
]
