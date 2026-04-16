# Framework Architecture (MVP)

## Design goals

- Feasibility first, extraction second.
- Deterministic orchestration and bounded retries.
- Structured agent communication through shared typed state.
- Persistent learning from success and failure records.

## Agent communication model

Agents do not directly chat with each other. Each agent:

1. reads the current `RunState`
2. writes a typed output back into `RunState`
3. returns control to the orchestrator

This keeps coordination deterministic and debuggable.

## Core state contracts

`RunState` carries these fields:

- `target`: input metadata and desired config
- `observation`: discovery output
- `feasibility`: complexity and recommended strategy
- `plan`: endpoint + payload template + confidence
- `extraction`: accepted config + price + matrix + shipping
- `validation`: mismatch list and inferred failure reason
- `failures`: audit trail of failed attempts
- `attempt_count` and `max_attempts`: retry budget

## Orchestration loop

1. Discovery
2. Feasibility
3. Initial planning
4. Execute
5. Validate
6. If invalid, append failure and repair
7. Retry until valid or max attempts reached

The loop is implemented in `ExtractionOrchestrator.run`.

## Knowledge persistence

`KnowledgeStore` writes execution outcomes to sqlite:

- `site_name`
- `endpoint`
- `strategy`
- `success`
- `failure_reason`

Additional validated-only learning:

- `replay_templates`: persists applied request-template metadata/mappings only on deterministic validated success.
- This is intended to support safe “proposal → validate → promote” flows (including future optional agentic onboarding).

Planner can reuse recent successful endpoint strategies for the same site.

## Framework choices

Current MVP uses a custom orchestrator for strict state control.

Implemented integrations:

- URL-first CLI mode that can bootstrap from a single product URL
- Browser bootstrap module that captures request URLs, token/session hints, option catalogs, and request-template metadata
- Cache-first recon reuse with one-time refresh on detected drift
- Optional Playwright dependency for browser-based discovery signals

Planned next integrations:

- Optional LangChain/LangGraph only for toolized LLM reasoning steps

## URL-first run path

1. CLI accepts only a product URL
2. CLI reuses a fresh recon snapshot when one exists; otherwise the bootstrap module performs browser capture
3. Captured hints, option catalog data, request templates, and network traces are saved into `target.bootstrap_signals` and `target.network_traces`
4. Discovery and feasibility classify extraction path from traces and signals
5. Planner selects endpoint candidates using network-trace scoring
6. Execution prefers deterministic HTTP replay and can synthesize site-specific replay payloads via pluggable site adapters (built from captured templates)
7. Validation and repair run through the same orchestrator loop, with one automatic recon refresh if cached traces appear to have drifted

If Playwright is unavailable, the system still runs using URL heuristics and falls back to browser-automation strategy classification.

## Why this communication style

- Easy to replay runs from persisted state snapshots
- Reliable testing for each agent in isolation
- No uncontrolled cross-agent loops
- Clear place to implement policy controls and retry budgets
