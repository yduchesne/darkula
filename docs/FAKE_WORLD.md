# Darkula Fake World

## Purpose
The Fake World is an executable synthetic underground ecosystem used for deterministic tests and live-model evaluations. It should reproduce documented behavioral archetypes without cloning live criminal services or depending on them.

## Initial archetypes
- cybercrime forum: boards, threads/posts, aliases, reputation, registration/gating, pagination, edits, quotes, attachments;
- access/credential marketplace: searchable listings, geography, organization/account/access types, changing inventory;
- ransomware/data-leak site: victim cards, organizations, geography/industry, publication/status changes, downloadable artifacts;
- additional forum/mirror: reposting, cross-source identity/evidence, migration and availability behavior.

`BlackGate`, `AccessBay`, `NightLeak`, and `ShadowTalk` are the canonical fictional source names: BlackGate is a cybercrime forum, AccessBay an access/credential marketplace, NightLeak a ransomware/data-leak site, and ShadowTalk a secondary forum/cross-source discussion source. They are composites, not replicas.

## World truth vs observations
Scenario truth is independent of what Darkula sees. Truth may define fictional actors, aliases, organizations, locations, relationships, source ownership, events, and timestamps. Source renderers expose only partial/noisy manifestations. Expected model outputs must not simply be embedded as hidden prompts.

## Realism
Scenarios should cover authentication/session expiry, nested pagination, redirects/mirrors, endpoint rotation, intermittent failures, rate limiting, malformed/dynamic content, edits/deletions, duplicates/reposts/quotes, multilingual material, images/attachments, ambiguous geography, inconsistent dates, misspellings, and unsupported claims.

Geographic scenarios must include explicit and implicit geography, e.g. "credentials of a hospital in Washington state", plus ambiguity such as Washington State vs Washington, D.C.

## Cross-source scenarios
Prefer interconnected stories: one alias advertises access on a forum; a related listing appears on a market; a fictional organization later appears on a leak site; another forum quotes/reposts the material. Preserve uncertainty and conflicting evidence.

## Traceability
For significant behavior maintain:
```text
documented real-world behavior
 -> FakeWorldScenario
 -> Darkula requirement
 -> integration test
 -> model/agent eval where applicable
```
Public authoritative reporting can justify archetypes. Do not require live illicit content for routine development.

## HTTP and browser boundary
Fake World models source behavior independently from its transport. Source renderers may be invoked directly by deterministic tests, but crawler integration must expose Fake World sources through ordinary HTTP servers. From `CrawlerRuntime`'s perspective, BlackGate and later sources are external websites; production crawler code must not know that the source is synthetic.

The canonical crawler integration path is:

```text
real CrawlerController
  -> real PodmanSandbox
  -> real sandboxed CrawlerRuntime
  -> real Playwright/Chromium
  -> HTTP
  -> Fake World server / BlackGate
```

This path exercises the browser, cookies/session state, redirects, pagination, rendered HTML, malformed content, failures/rate limits, link discovery, browser timeouts, and sandbox network policy. A direct `httpx`-only path may be useful for narrower tests but does not replace the canonical real-browser vertical slice.

The Fake World HTTP layer is a thin adapter over the same scenario/truth/renderer model; it must not create a second source-behavior implementation. The renderer remains directly testable without starting HTTP infrastructure.

Tests of `CrawlerController` may use `FakeSandbox` to isolate controller policy, cancellation, failure mapping, and result validation. The canonical crawler integration path does not use a `FakeCrawlerController`. A future `FakeCrawler` may implement the application-facing Crawler SPI when a higher-level component needs crawling outside its unit under test.

## Structure
The implementation treats Fake World as a subsystem rather than scattered HTML fixtures, with scenario definitions, source renderers/fixtures, independent truth, and a reusable HTTP-hosting adapter for crawler integration. Transport adapters expose observations only; hidden `FakeWorldTruth` never crosses into production Darkula code. Exact directories/format remain owned by the implementing PR.
