# Darkula Fake World

## Purpose
The Fake World is an executable synthetic underground ecosystem used for deterministic tests and live-model evaluations. It should reproduce documented behavioral archetypes without cloning live criminal services or depending on them.

## Initial archetypes
- cybercrime forum: boards, threads/posts, aliases, reputation, registration/gating, pagination, edits, quotes, attachments;
- access/credential marketplace: searchable listings, geography, organization/account/access types, changing inventory;
- ransomware/data-leak site: victim cards, organizations, geography/industry, publication/status changes, downloadable artifacts;
- additional forum/mirror: reposting, cross-source identity/evidence, migration and availability behavior.

Illustrative fictional sources may use names such as BlackGate, AccessBay, NightLeak, and ShadowTalk. They are composites, not replicas.

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

## Structure
The eventual implementation should treat Fake World as a subsystem rather than scattered HTML fixtures, with scenario definitions, source renderers/fixtures, and independent truth. Exact directories/format are TBD.
