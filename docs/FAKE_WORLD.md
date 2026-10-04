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

```text
scenario_id      = "blackgate-core"
scenario_version = 1
```

`darkula.testing.fake_world.get_scenario("blackgate-core", version=1)` loads
the canonical, fully validated, immutable definition. Unknown IDs/versions
raise `UnknownScenarioError` (a `KeyError` subclass); invalid versions raise
a typed validation error. Scenario definitions are shared safely (immutable);
mutable runtime state lives only on fresh renderer instances.

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
  separate Washington, D.C. reference (ambiguity seed for PR 12).

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

Deterministic test-matrix IDs: `FW1..FW10` (model), `R1..R24` (rendering),
`G1..G5` (geography/truth), `T1..T6` (traceability), `VSLICE` (component
vertical slice), `GOLDEN` (frozen canonical hashes). The manifest is validated
at scenario construction: duplicate IDs, dangling route/object refs, and
missing test mapping fail fast.

## Security/synthetic-data discipline
- fully fictional content — no live criminal-source dependency, no copied
  stolen/victim data, no malware fixtures;
- synthetic login credentials clearly labeled test-only (`# nosec` justified);
- unsafe-looking attachment filenames remain inert data (RFC 5987-encoded in
  headers, never used as filesystem paths);
- device-independent golden hashes, no fixture writes, no live URL fetch;
- truth identifiers never leak into rendered HTML/headers (regression-tested).

## What remains for PR 15
AccessBay/NightLeak/ShadowTalk archetypes, cross-source identity/evidence
scenarios, and model/agent evaluations over the same scenario definitions.
PR 7 consumes the renderer for crawler tests; PR 10 for recon; PR 12 uses the
Washington State/D.C. ambiguity seed.
