# SPDX-License-Identifier: AGPL-3.0-only
"""PR 10 real-crawler reconnaissance slice fixtures.

Imports the PR 7 crawler infrastructure fixtures (provisioned by
``./build.sh --intg``) so the canonical real Coordinator -> Real ReconAgent
-> FakeLlmClient -> real CrawlerController -> PodmanSandbox -> Chromium ->
BlackGate -> PostgreSQL slice runs against the same Darkula-owned
infrastructure as the PR 7 suite, failing closed when it is missing.

Only the ``slice/`` sub-suite imports these; the ``persistence/`` sub-suite
stays crawler-free.
"""

from __future__ import annotations

from tests.integration.crawler.conftest import (  # noqa: F401
    controller,
    crawler_infrastructure_ready,
    crawler_settings,
    fake_world_url,
    sandbox,
)
