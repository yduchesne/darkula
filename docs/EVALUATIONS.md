# Darkula Evaluations

## Purpose
Evaluations measure intelligent behavior against realistic, known Fake World truth. They are distinct from deterministic integration tests and may invoke real models through LlmClient.

## Evaluation layers
**Recon evals** assess source characterization, useful path discovery, access/navigation understanding, proposed collection strategy, evidence grounding, and dispositions.

**Extraction evals** assess semantic entity, topic/classification, relationship, and geographic extraction/resolution. Deterministic extractors remain primarily ordinary tests.

**SourceAnalyst evals** assess source-level conclusions such as relevance, activity, novelty, geography/industry focus, overlap, and evidence support.

**End-to-end evals** assess combined behavior while retaining stage-level results so upstream extraction failures are not misdiagnosed as SourceAnalyst failures.

## Ground truth
Fake World truth exists independently of rendered source pages and expected
model answers. A scenario may know that two aliases represent one fictional
actor while Darkula sees only evidence exposed by sources. Evals compare
system outputs with truth and required evidence constraints without handing
hidden truth to the model.

## PR 10 status — no live-model recon evals yet

PR 10 delivers the deterministic bound of recon behavior (FakeLlmClient
scenarios over the real Coordinator/ReconAgent, available structured
outputs, evidence-reference validation, real-crawler/Fake World slices) but
**not** a live-model ReconAgent quality evaluation framework: exact
metrics/thresholds/datasets and LangSmith/Langfuse adapters remain PR 15.
Manual live-provider smoke runs are possible through the delivered
`OpenAiLlmClient` adapter + `DARKULA_LLM__DRIVER=openai` configuration and
are documented as optional; they are never part of ``./build.sh --qa`` or
``./build.sh --intg`` and CI never requires a paid/live model API.

## Reusable Fake World contracts (PR 6)
PR 6 delivered the reusable scenario/truth inputs that future evals will
share with deterministic tests (no evaluation framework was added):

- `darkula.testing.fake_world.get_scenario("blackgate-core", version=1)`
  returns the canonical immutable scenario;
- `FakeWorldTruth` is independent from every rendered page and supports
  truth-only facts (for example a hidden alias or a private sale that no
  rendered observation states);
- renderers expose only partial/noisy observations, preserving the
  truth-vs-evidence gap evals must measure;
- the Washington State hospital seed plus a separate Washington, D.C.
  reference is available as a future geographic-disambiguation eval input;
- behavior IDs (`FW-BG-*`) link scenario/version, requirements, and
  deterministic tests for future eval correlation.

Exact metrics, thresholds, datasets, and CI gating for live-model evals
remain intentionally TBD (representative scenarios now exist; thresholds
are deliberately not invented in PR 6).

## Execution
Deterministic CI uses FakeLlmClient. Live-model eval runs use production-style LlmClient adapters and record model/provider/version, scenario/version, prompts according to telemetry policy, structured outputs, latency/token metadata, and evaluation results.

## Observability
Every eval should correlate scenario ID, experiment/run ID, agent run, LLM invocation, and OTEL trace where available. AgentObservability adapters (for example LangSmith or Langfuse) must not define the evaluation domain model.

## Gates
Exact metrics, thresholds, datasets, and CI gating policy are intentionally TBD until representative Fake World scenarios exist. Binary pass/fail may be used where requirements are categorical; graded measures require explicit rubrics and reproducibility.
