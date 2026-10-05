# Darkula Evaluations

## Purpose
Evaluations measure intelligent behavior against realistic, known Fake World
truth. They are distinct from deterministic integration tests and may invoke
real models through `LlmClient`. Evaluation is explicit and opt-in: ordinary
`--qa`/`--sec`/`--intg` builds and CI never require a live model or SaaS
credentials.

## Core invariant
> Evaluation consumes production Darkula behavior and independent Fake World
> truth from outside the production pipeline. The model never receives hidden
> truth or expected answers; evaluators compare bounded production outputs
> with versioned truth after execution, and live-model evaluation remains
> explicit, reproducible, provider-aware, and non-CI by default.

`FakeWorldTruth`, expected answers, rubric answers, and hidden aliases/events
are never included in prompts, production contexts/results, composition, or
production imports.

## Evaluation layers
- **Recon** — source type, useful paths/entrypoints, accessibility/auth/
  navigation, scoreable collection-strategy items, disposition, evidence
  grounding, unsupported facts, and observable mirror recognition.
- **Semantic extraction** — typed occurrence/normalized-value matches,
  per-type and aggregate P/R/F1, exact grounding validity, unsupported extras.
  Extraction confidence never determines correctness.
- **Geography** — expected `RESOLVED`/`AMBIGUOUS`/`UNRESOLVED` and canonical
  fields when declared. Resolution confidence stays separate from extraction
  confidence. No live geocoder was added for PR 15.
- **Relationship extraction** — predicate/endpoint matches, exact support
  grounding, P/R/F1 over observable assertions. Hidden cross-source
  relationships are never required.
- **SourceAnalyst** — explicit finite rubric coverage, evidence validity, and
  deterministically detectable unsupported conclusions. Source-local analysis
  is never penalised for lacking hidden cross-source truth.

## Ground truth
Fake World truth exists independently of rendered source pages and expected
model answers. A scenario may know that two aliases represent one fictional
actor while Darkula sees only evidence exposed by sources. Expected
observations include only facts the evaluated agent could observe; hidden
truth is used solely for leakage detection (a strict safety metric), never for
recall requirements.

## Evaluation identity and artifacts
Evaluation identity is distinct from production identity:

```text
production IDs  = production persistence identity
EvalCaseId      = stable evaluation-case identity
EvalRunId       = one experiment/run identity
EvalInvocationId= one case execution within a run
scenario/version= synthetic dataset identity
evaluator/version = scoring-semantics identity
model identity  = model-under-test identity
```

The evaluator-side package `src/darkula/evaluation/` provides bounded,
immutable models (`domain`), deterministic metric primitives (`metrics`),
narrow per-layer evaluators (`evaluators`), an explicit case registry
(`cases`), an atomic artifact store (`artifacts`), a runner (`runner`), and an
explicit opt-in CLI (`cli`). Production domain/application/infrastructure/
composition code must never import it (static guard).

Runs write bounded artifacts under `.eval-results/<run-id>/`:

```text
.eval-results/<run-id>/manifest.json   atomically replaced run manifest
.eval-results/<run-id>/results.jsonl   one bounded result per case
```

Artifacts contain run/case/scenario/evaluator/model identities, metrics,
bounded failure codes, duration, and gate results only. Prompts, outputs,
unrestricted bodies, credentials, secrets, and hidden reasoning are never
persisted. A partial/cancelled run is written with `PARTIAL`/`CANCELLED` and
is never mistaken for a completed one.

## Metrics
Scoring is deterministic and versioned; the same inputs always produce the
same score. Primitive scorers include `SetMatchScore` (TP/FP/FN/P/R/F1),
`CategoricalScore`, `GroundingScore`, and `RubricCoverage`. Empty-set
semantics are documented and tested:

- expected empty + actual empty → P = R = F1 = 1;
- expected empty + actual non-empty → P = 0, R = 1, F1 = 0;
- expected non-empty + actual empty → P = 0, R = 0, F1 = 0.

There is no LLM-as-judge. There is no arbitrary universal quality threshold:
raw metrics, categorical case expectations, and optional explicit gating are
distinct. Categorical safety/grounding invariants (for example hidden-truth
leakage) may be strict.

## Case suite
Minimal high-value cases span multiple sources and layers:
`EV-RECON-001..003`, `EV-SEM-001..003`, `EV-GEO-001`, `EV-REL-001..002`, and
`EV-SA-001..004`. Cases are registered explicitly by `(case_id, version)` — no
filesystem discovery; semantic changes require a case-version increment.
Repeated experiments retain distinct run IDs but stable semantic case/
scenario/evaluator identities.

## Execution
The runner invokes an injected production-capability port once per case,
measures duration, scores with the deterministic evaluator, writes results
incrementally, and finalizes the manifest atomically. Deterministic CI uses
`FakeLlmClient` or prebuilt bounded observations; live-model runs use
production `LlmClient` adapters and record configured provider/model/profile,
operation, duration, status, and evaluator metrics. `LlmClient` is not
expanded for token accounting.

Live execution is explicit: `./build.sh --eval --live` (or
`python -m darkula.evaluation`) fails fast when the composed model is not
live; the deterministic fake can never masquerade as a live run. The runner
requires observation fixtures for deterministic replay; live executions wire
the production-capability port from the composed runtime.

## Observability
Every result records run/invocation/case/version, scenario/version, layer,
evaluator/version, model provider/name/profile, and operation name.
Agent-specific observability (LangSmith) reuses the existing provider-neutral
`AgentObservability` SPI. See `docs/OBSERVABILITY.md`.

## Gates
Exact raw metrics are always recorded. A quality gate is enabled only
explicitly and is always supplied by the caller (never a hard-coded universal
threshold). A failed gate changes the CLI exit code, not the recorded metrics.

## PR 6 reusable Fake World contracts
- `get_scenario("blackgate-core", version=1)` returns the canonical immutable
  scenario; `get_scenario("darkula-cross-source", version=1)` returns the
  PR 15 cross-source scenario;
- `FakeWorldTruth` is independent from every rendered page and supports
  truth-only facts (hidden alias `ghost_admin`, private Edgewater sale, hidden
  seizure counter-fact);
- renderers expose only partial/noisy observations, preserving the
  truth-vs-evidence gap evaluations must measure;
- the Washington State hospital seed plus a separate Washington, D.C.
  reference remains available as a geographic-disambiguation input;
- behavior IDs (`FW-BG-*`, `FW-AB-*`, `FW-NL-*`, `FW-ST-*`, `FW-XS-*`) link
  scenario/version, requirements, and deterministic tests for eval
  correlation.
