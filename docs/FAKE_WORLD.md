# Darkula Fake World

## Purpose
The Fake World is an executable synthetic underground ecosystem used as the
deterministic external-world test boundary. PR 6 delivered the subsystem
foundation: a versioned scenario/truth model, the realistic **BlackGate**
forum archetype, a deterministic source renderer, and behavior traceability.
Later PRs (7 crawler, 10 recon, 12 geography, 15 expansion/evals) consume
these deliverables without contacting live criminal services.

## Package and ownership boundary
```text
src/darkula/testing/fake_world/
    identifiers.py     bounded semantic identity value types + validation
    model.py           immutable scenario + source-observable forum model
    truth.py           independent world truth (tests/evals only)
    rendering.py       transport-light request/response + FakeWorldRenderer
    traceability.py    stable behavior IDs + manifest validation
    registry.py        canonical scenario lookup
    scenarios/blackgate_v1.py   canonical "blackgate-core" v1
```

Allowed dependencies: tests/evals and future crawler integration tests may
import `darkula.testing.fake_world`. **Forbidden:** production
domain/application/infrastructure packages (and the composition root) must
never import it — enforced by a static AST guard and a fresh-interpreter
`sys.modules` check in `tests/unit/testing/fake_world/test_architecture.py`.

## Scenario identity and versioning
Logical scenarios have an explicit identity and integer version:

`BlackGate`, `AccessBay`, `NightLeak`, and `ShadowTalk` are the canonical fictional source names: BlackGate is a cybercrime forum, AccessBay an access/credential marketplace, NightLeak a ransomware/data-leak site, and ShadowTalk a secondary forum/cross-source discussion source. They are composites, not replicas.

## World truth vs observations
> Fake World truth describes the complete fictional world, while source
> renderers expose only the observations a real collector could see;
> production Darkula code never receives hidden truth or expected answers.

`FakeWorldTruth` stores actors, aliases, organizations, locations,
relationships, and fixed-UTC events. Rendered pages expose only a partial,
noisy manifestation. Example built into BlackGate v1: the truth model knows
actor-001 also owns the hidden alias `zfox`, which no rendered page states;
and event-002 records a private second sale that never appears on the forum.
Tests/evals may compare outputs with truth; application code must not consume
truth.

Truth holds objective fictional-world facts, not expected Darkula answers.
Forum posts can be true, false, stale, or conflicting — they are observations,
not assertions of ground truth.

## BlackGate core v1
A fully synthetic cybercrime-forum archetype:

- 4 boards (announcements public; access, chatter, archives gated);
- aliases with reputation; welcome thread public, everything else
  registered-gated;
- a 12-post thread spanning 2 pages (stable pagination, prev/next);
- registration/login gating with synthetic test-only credentials
  (`zerofox77` / `blackgate-test-password`);
- deterministic request-count session expiry (12 protected requests, then
  re-authentication);
- quotes, edits with markers, moderator-removed placeholders, reposted/
  duplicate content, multilingual (Cyrillic) content;
- one controlled malformed legacy page;
- hostile prompt-injection-like prose rendered strictly as untrusted content;
- safe synthetic attachments (text, 1x1 GIF binary, metadata-only) plus one
  unsafe-looking-but-inert filename;
- deterministic redirect (`/old-thread/...`), rate limiting (429 with fixed
  Retry-After on the archives board), and an intermittent 503-then-200
  thread sequence;
- the geographic seed: a fictional Washington State hospital listing and a
  separate Washington, D.C. reference (ambiguity seed for PR 12), consumed by
  PR 12 without exposing hidden truth: the rendered "Mason Creek General
  Hospital" / "Mason Creek, Washington" clues ground semantic ORGANIZATION +
  LOCATION mentions, the D.C. reference grounds a distinct LOCATION, and a
  bare "Washington" can remain AMBIGUOUS under a scripted resolver. Production
  code contains no BlackGate/`"Washington"` special case.
- an additive public "Collector sample indicators" thread
  (`thr-collector-samples`) carrying fully synthetic, reserved/non-routable
  observables (IPv4 `203.0.113.77`, IPv6 `2001:db8::c0de`, a repeated
  `collector-samples.example.test` host, an HTTP(S) URL, an email, a SHA-256,
  and inert prompt-injection prose) used by the PR 11 canonical extraction
  slice. This is a compatible additive fixture: existing canonical pages and
  behavior IDs are unchanged; only the landing/board-index counts and golden
  hashes were deliberately updated.

## Renderer boundary
The renderer is transport-light and fully deterministic:

```python
FakeWorldRenderer().render(scenario, request, session=None) -> RenderResult
```

- identical `(scenario, request, session)` produce identical responses;
- no network, no filesystem mutation, no wall clock, no hidden global state;
- source-level failures (401/403/404/405/429/503/redirect) are returned as
  `RenderedSourceResponse` objects, never raised;
- scenario/programming errors raise a typed `FakeWorldValidationError`;
- sessions are returned explicitly via `RenderResult.session_update`; runtime
  counters (login serial, expiry, rate limit, scripted failures) restart with
  `renderer.reset()` and are never shared between tests.

## Behavior traceability
Every significant canonical behavior carries a stable behavior ID following
`FW-<SOURCE>-<AREA>-NNN`, for example `FW-BG-AUTH-001`. Each manifest entry
links:

```text
behavior ID -> scenario/version -> archetype/rationale ->
requirement/future capability -> deterministic test IDs -> future eval note
```

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

**PR 7 delivered HTTP adapter.** `FakeWorldHttpService.handle()` maps the renderer/transport contract onto HTTP semantics: source failures become real status codes (200/302/401/429/503); the login form POST becomes an authenticated session (`Set-Cookie: session=...`); every protected render decrements the deterministic 12-request session budget and redirects to `/login` once exhausted; 503 schedules serve a real failure then the retry succeeds; the malformed legacy page exists as-is; the unsafe-named attachment is served with `Content-Disposition: attachment` and a safe content type for an inert body. Cookie lookup is case-insensitive (`Cookie` header), matching `http.server`'s behavior in the real stack. Truth-only identifiers are build-time excluded from every renderer HTML, and the integration slice verifies through the real browser that `actor-001`/`alias-002`/`relationship-001`/`zfox` and the auth password never appear in any observed page title, text excerpt, or link.

The delivered canonical browser path (verified by `tests/integration/crawler/`) starts cold at `/` and observes: the protected `/index` bouncing to the login form, the login POST landing on the authenticated board index with the session retained, boards and threads rendering, the 503-then-200 thread, the malformed legacy page, and bounded attachment samples; a focused crawl starting at `/thread/thr-hospital-creds?page=1` re-observes the auth-bounced target post-login and follows pagination (`?page=2`, `Page 2 of 2`) with the same session. The engine additionally skips the logout path, treats quote/repost fragments as the same page, and never enqueues out-of-origin links.

Tests of `CrawlerController` may use `FakeSandbox` to isolate controller policy, cancellation, failure mapping, and result validation. The canonical crawler integration path does not use a `FakeCrawlerController`. A future `FakeCrawler` may implement the application-facing Crawler SPI when a higher-level component needs crawling outside its unit under test.

## Structure
The implementation treats Fake World as a subsystem rather than scattered HTML fixtures, with scenario definitions, source renderers/fixtures, independent truth, and a reusable HTTP-hosting adapter for crawler integration. Transport adapters expose observations only; hidden `FakeWorldTruth` never crosses into production Darkula code. Exact directories/format remain owned by the implementing PR.
