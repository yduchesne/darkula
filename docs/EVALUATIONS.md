# Darkula Evaluations

## Purpose
Evaluations measure intelligent behavior against realistic, known Fake World truth. They are distinct from deterministic integration tests and may invoke real models through LlmClient.

## Evaluation layers
**Recon evals** assess source characterization, useful path discovery, access/navigation understanding, proposed collection strategy, evidence grounding, and dispositions.

**Extraction evals** assess semantic entity, topic/classification, relationship, and geographic extraction/resolution. Deterministic extractors remain primarily ordinary tests.

**SourceAnalyst evals** assess source-level conclusions such as relevance, activity, novelty, geography/industry focus, overlap, and evidence support.

**End-to-end evals** assess combined behavior while retaining stage-level results so upstream extraction failures are not misdiagnosed as SourceAnalyst failures.

## Ground truth
Fake World truth exists independently of rendered source pages and expected model answers. A scenario may know that two aliases represent one fictional actor while Darkula sees only evidence exposed by sources. Evals compare system outputs with truth and required evidence constraints without handing hidden truth to the model.

## Execution
Deterministic CI uses FakeLlmClient. Live-model eval runs use production-style LlmClient adapters and record model/provider/version, scenario/version, prompts according to telemetry policy, structured outputs, latency/token metadata, and evaluation results.

## Observability
Every eval should correlate scenario ID, experiment/run ID, agent run, LLM invocation, and OTEL trace where available. AgentObservability adapters (for example LangSmith or Langfuse) must not define the evaluation domain model.

## Gates
Exact metrics, thresholds, datasets, and CI gating policy are intentionally TBD until representative Fake World scenarios exist. Binary pass/fail may be used where requirements are categorical; graded measures require explicit rubrics and reproducibility.
