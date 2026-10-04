# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula crawler application contract (PR 7).

``Crawler`` / ``CrawlRequest`` / ``CrawlResult`` are the single application
contract for bounded source inspection; ``CrawlerController`` is its trusted
orchestrator. The versioned controller-runtime protocol lives in
:mod:`darkula.crawler.runtime_protocol`.
"""

from darkula.crawler.contracts import (
    AllowedOrigin,
    CrawlCredentials,
    Crawler,
    CrawlPageObservation,
    CrawlRequest,
    CrawlResult,
    CrawlStatus,
    InvalidCrawlRequest,
)
from darkula.crawler.controller import CrawlerController
from darkula.crawler.runtime_protocol import (
    PROTOCOL_VERSION,
    RuntimeCredentials,
    RuntimeInput,
    RuntimeOutput,
    RuntimeOutputPage,
    RuntimeProtocolError,
    decode_input,
    decode_output,
    encode_input,
    encode_output,
)

__all__ = [
    "PROTOCOL_VERSION",
    "AllowedOrigin",
    "CrawlCredentials",
    "CrawlPageObservation",
    "CrawlRequest",
    "CrawlResult",
    "CrawlStatus",
    "Crawler",
    "CrawlerController",
    "InvalidCrawlRequest",
    "RuntimeCredentials",
    "RuntimeInput",
    "RuntimeOutput",
    "RuntimeOutputPage",
    "RuntimeProtocolError",
    "decode_input",
    "decode_output",
    "encode_input",
    "encode_output",
]
