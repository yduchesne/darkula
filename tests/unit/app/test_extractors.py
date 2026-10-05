# SPDX-License-Identifier: AGPL-3.0-only
"""Semantic unit matrices for the PR 11 deterministic extractors.

Covers the IP/URL/EM/DN/HS matrices from the PR 11 plan. Matching is
conservative: values are recognized only at exact canonical-text spans and
normalization follows the documented v1 rules.
"""

from __future__ import annotations

from darkula.app.extractors import (
    DeterministicEntityExtractor,
    DomainExtractor,
    EmailExtractor,
    EntityMatch,
    HashExtractor,
    IpAddressExtractor,
    UrlExtractor,
)
from darkula.domain.extraction import EntityType, HashSubtype


def _only(extractor: DeterministicEntityExtractor, text: str) -> EntityMatch:
    matches = extractor.extract(text)
    assert len(matches) == 1, matches
    match = matches[0]
    # Every match must be exactly the source slice it claims.
    assert text[match.source_span.start : match.source_span.end] == match.raw_value
    return match


class TestIpExtractor:
    def test_ip1_ipv4_canonical_exact(self) -> None:
        match = _only(IpAddressExtractor(), "server 192.168.1.1 here")
        assert match.entity_type is EntityType.IP_ADDRESS
        assert match.normalized_value == "192.168.1.1"
        assert match.source_span.start == 7

    def test_ip2_punctuation_excluded(self) -> None:
        match = _only(IpAddressExtractor(), "(10.0.0.1),")
        assert match.raw_value == "10.0.0.1"

    def test_ip3_invalid_octet_no_match(self) -> None:
        assert IpAddressExtractor().extract("999.1.1.1") == ()

    def test_ip4_larger_token_contamination_no_match(self) -> None:
        assert IpAddressExtractor().extract("1.2.3.4.5") == ()

    def test_ip5_full_ipv6_compressed_lowercase(self) -> None:
        match = _only(IpAddressExtractor(), "2001:0db8:0000:0000:0000:0000:0000:0001")
        assert match.normalized_value == "2001:db8::1"

    def test_ip6_compressed_ipv6(self) -> None:
        match = _only(IpAddressExtractor(), "fe80::1")
        assert match.normalized_value == "fe80::1"

    def test_ip7_malformed_ipv6_no_match(self) -> None:
        assert IpAddressExtractor().extract("2001:db8:::1") == ()

    def test_ip8_private_and_reserved_extracted_syntactically(self) -> None:
        extractor = IpAddressExtractor()
        assert {m.normalized_value for m in extractor.extract("10.0.0.1")} == {
            "10.0.0.1"
        }
        assert {m.normalized_value for m in extractor.extract("::1")} == {"::1"}


class TestUrlExtractor:
    def test_url1_http_match(self) -> None:
        match = _only(UrlExtractor(), "http://example.com")
        assert match.entity_type is EntityType.URL
        assert match.normalized_value == "http://example.com"

    def test_url2_https_match(self) -> None:
        match = _only(UrlExtractor(), "https://example.com")
        assert match.normalized_value == "https://example.com"

    def test_url3_uppercase_scheme_and_host_canonical(self) -> None:
        match = _only(UrlExtractor(), "HTTP://EXAMPLE.COM/Path")
        assert match.normalized_value == "http://example.com/Path"

    def test_url4_path_and_query_preserved(self) -> None:
        match = _only(UrlExtractor(), "https://example.com/a/b?x=1&y=2")
        assert match.normalized_value == "https://example.com/a/b?x=1&y=2"

    def test_url5_prose_punctuation_excluded(self) -> None:
        match = _only(UrlExtractor(), "See http://example.com/a.")
        assert match.raw_value == "http://example.com/a"

    def test_url6_unsupported_scheme_no_match(self) -> None:
        assert UrlExtractor().extract("ftp://example.com") == ()

    def test_url7_relative_path_no_match(self) -> None:
        assert UrlExtractor().extract("/foo/bar") == ()

    def test_url8_userinfo_rejected(self) -> None:
        assert UrlExtractor().extract("http://user:pass@example.com/") == ()

    def test_url9_non_default_port_preserved(self) -> None:
        match = _only(UrlExtractor(), "http://example.com:8080/x")
        assert match.normalized_value == "http://example.com:8080/x"

    def test_url10_malformed_host_no_match(self) -> None:
        assert UrlExtractor().extract("http://exa mple.com") == ()


class TestEmailExtractor:
    def test_em1_normal_mailbox(self) -> None:
        match = _only(EmailExtractor(), "mail bob@example.com now")
        assert match.entity_type is EntityType.EMAIL
        assert match.normalized_value == "bob@example.com"

    def test_em2_uppercase_domain_lowercased(self) -> None:
        match = _only(EmailExtractor(), "bob@EXAMPLE.COM")
        assert match.normalized_value == "bob@example.com"

    def test_em3_local_part_case_preserved(self) -> None:
        match = _only(EmailExtractor(), "Bob.Smith@example.com")
        assert match.normalized_value == "Bob.Smith@example.com"

    def test_em4_punctuation_excluded(self) -> None:
        match = _only(EmailExtractor(), "(bob@example.com)")
        assert match.raw_value == "bob@example.com"

    def test_em5_malformed_and_space_no_match(self) -> None:
        assert EmailExtractor().extract("bob @example.com") == ()
        assert EmailExtractor().extract("bob@exa mple.com") == ()

    def test_em6_larger_token_matched_in_full(self) -> None:
        match = _only(EmailExtractor(), "notbob@example.com")
        assert match.raw_value == "notbob@example.com"

    def test_em7_overlong_local_part_rejected(self) -> None:
        assert EmailExtractor().extract("a" * 65 + "@example.com") == ()


class TestDomainExtractor:
    def test_dn1_domain_lowercase(self) -> None:
        match = _only(DomainExtractor(), "example.com")
        assert match.entity_type is EntityType.DOMAIN
        assert match.normalized_value == "example.com"

    def test_dn2_subdomain_exact_occurrence(self) -> None:
        match = _only(DomainExtractor(), "a.b.example.com")
        assert match.raw_value == "a.b.example.com"

    def test_dn3_invalid_hyphen_no_match(self) -> None:
        assert DomainExtractor().extract("-foo.com") == ()
        assert DomainExtractor().extract("foo-.com") == ()

    def test_dn4_length_violation_no_match(self) -> None:
        assert DomainExtractor().extract("a" * 64 + ".com") == ()

    def test_dn5_ipv4_is_not_a_domain(self) -> None:
        assert DomainExtractor().extract("192.168.1.1") == ()

    def test_dn6_email_domain_is_an_independent_occurrence(self) -> None:
        matches = DomainExtractor().extract("bob@example.com")
        assert [m.raw_value for m in matches] == ["example.com"]

    def test_dn7_url_host_is_an_independent_occurrence(self) -> None:
        matches = DomainExtractor().extract("http://example.com/path")
        assert [m.raw_value for m in matches] == ["example.com"]

    def test_dn8_idna_behavior_is_documented_ascii_only(self) -> None:
        # v1 recognizes ASCII/A-label domains only; a Unicode label is not
        # converted and produces no partial false match.
        assert DomainExtractor().extract("münchen.de") == ()

    def test_dn9_dotted_numeric_garbage_no_match(self) -> None:
        assert DomainExtractor().extract("999.999.999.999") == ()

    def test_dn10_punctuation_exact_span(self) -> None:
        match = _only(DomainExtractor(), "(example.com)")
        assert match.raw_value == "example.com"
        assert match.source_span.start == 1


class TestHashExtractor:
    def test_hs1_md5(self) -> None:
        match = _only(HashExtractor(), "d41d8cd98f00b204e9800998ecf8427e")
        assert match.subtype == HashSubtype.MD5.value
        assert match.entity_type is EntityType.HASH

    def test_hs2_sha1(self) -> None:
        match = _only(HashExtractor(), "da39a3ee5e6b4b0d3255bfef95601890afd80709")
        assert match.subtype == HashSubtype.SHA1.value

    def test_hs3_sha256(self) -> None:
        digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        match = _only(HashExtractor(), digest)
        assert match.subtype == HashSubtype.SHA256.value

    def test_hs4_uppercase_lowercase_normalized(self) -> None:
        match = _only(HashExtractor(), "D41D8CD98F00B204E9800998ECF8427E")
        assert match.normalized_value == "d41d8cd98f00b204e9800998ecf8427e"

    def test_hs5_non_hex_no_match(self) -> None:
        assert HashExtractor().extract("z" * 32) == ()

    def test_hs6_longer_hex_embedding_no_match(self) -> None:
        assert HashExtractor().extract("a" * 65) == ()

    def test_hs7_punctuation_exact_span(self) -> None:
        match = _only(HashExtractor(), "<d41d8cd98f00b204e9800998ecf8427e>")
        assert match.raw_value == "d41d8cd98f00b204e9800998ecf8427e"
        assert match.source_span.start == 1
