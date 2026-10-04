# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula CrawlerRuntime (PR 7).

The minimal sandboxed runtime that performs hostile browsing with Playwright
+ Chromium inside one disposable Darkula-owned container. It contains no
agents, no LLM clients, no repositories, no DataStream/ObjectStore clients,
no application credentials, no production composition root, and no arbitrary
shell-command surface.

Responsibilities (deterministic and bounded):

- launch Chromium and maintain one fresh browser context per execution;
- navigate only the controller-provided allowed origin (parsed origin
  semantics, never string prefix matching);
- follow only deterministic authorized navigation (BFS with sorted links,
  bounded page/request/depth/time budgets);
- maintain cookie/session state within the execution (login form submission
  when credentials are granted);
- handle redirects and represent 429/503 without turning source HTTP
  failures into sandbox infrastructure crashes;
- skip self-destructive endpoints (``/logout``) so authenticated traversal
  remains deterministic;
- return bounded observations/links and close the browser.

No autonomous "interestingness" inference exists.
"""

from crawler_runtime.engine import (
    AuthDecision,
    NavigateResult,
    PageAdapter,
    traverse,
)
from crawler_runtime.playwright_adapter import PlaywrightPageAdapter

__all__ = [
    "AuthDecision",
    "NavigateResult",
    "PageAdapter",
    "PlaywrightPageAdapter",
    "traverse",
]
