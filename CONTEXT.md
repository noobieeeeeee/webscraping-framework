# Multi-chat context (handoff)

Last updated: 2026-04-13

This file is meant to be the **single source of truth** for ongoing work in this repo across multiple Copilot chats.
If you make meaningful changes, update the sections **Recent work log** and **Status / next tasks**.

---

## What this project is

**Agentic Price Extraction MVP**: a Python framework to determine feasibility and extract prices from dynamic product configurators (printing sites like Onlineprinters / Print24 / Saxoprint).

High-level approach:

- Use a **browser bootstrap** (Playwright) to capture session/cookies + network activity + basic DOM option signals.
- Run a deterministic agent pipeline:
  - Discovery → Feasibility → Planner → Execution → Validation → Repair (bounded retries)
- Prefer **HTTP replay** of captured API endpoints for deterministic price extraction; fall back only when explicitly allowed.
- Persist learning (successful endpoints, option-normalization mappings, recon snapshots) in SQLite.

See docs/architecture.md for the baseline design.

---

## Repo layout / key files

- src/price_extractor/cli.py
  - CLI entry point
  - Builds TargetInput from URL/spec + bootstrap artifacts
  - Writes per-run artifacts under `.data/runs/...`

- src/price_extractor/browser_bootstrap.py
  - Playwright-based bootstrap
  - Captures:
    - cookies + “requires_session” signals
    - network traces (subset of request/response headers + POST body preview)
    - option groups / selected configuration summaries
    - consent handling signals

- src/price_extractor/agents.py
  - Agent implementations (discovery/planning/execution/validation/repair)
  - HTTP replay via `urllib.request`
  - Trace selection scoring (`_find_trace`) and extraction from JSON/text

- src/price_extractor/site_adapters.py
  - Pluggable per-site HTTP replay request synthesis
  - Centralizes site-specific replay candidate building

- src/price_extractor/orchestrator.py
  - Orchestration loop (retry budget, failure audit trail, summary output)

- src/price_extractor/knowledge_store.py
  - SQLite persistence in `.data/knowledge.db`
  - Tables:
    - `knowledge_runs` (success endpoints + failures)
    - `option_normalizations` (requested → accepted value)
    - `recon_snapshots` (bootstrap artifacts snapshot with TTL)
    - `replay_templates` (validated replay templates/mappings)

- README.md
  - Primary usage docs + copy/paste commands

- TASKS.md
  - Task tracker (Done / Next)

---

## How to run (Windows)

This repo typically uses a local conda env in `.conda-env`.

- Activate the env:
  - `conda activate d:\sem_7\webscraping-framework\.conda-env`

- Run a single URL:
  - `python -m price_extractor.cli --url "https://example.com/product" --verbose`

If Playwright browser bootstrap is needed:

- `pip install -e .[browser]`
- `playwright install chromium`

---

## Artifacts and where to look

Each run writes artifacts under:

- `.data/runs/<site>/<unit_id>/`
  - `summary.json` (high-level outcome)
  - `spec.json` (inputs)
  - `bootstrap.json` (bootstrap signals + cookie names/values)
  - `network_traces.json` (captured request/response metadata)
  - `option_catalog.json` (structured option groups/options + backend hints)
  - `drift_report.json` (cache-vs-refresh diff summary when recon reuse is involved)
  - `state_snapshot.json` (agent state)

`network_traces.json` is the critical input for HTTP replay; it stores a subset of request headers, method, URL, and POST body (with truncation metadata).

---

## Implemented features (high value)

From TASKS.md and recent work:

- Redirect + variant-aware HTTP replay improvements (Onlineprinters)
- Repeatable CLI option overrides:
  - `--option key=value` (repeatable, deterministic precedence, scalar coercion)
- Strict option enforcement:
  - `--require-matched-options` fails runs when requested options aren’t applied/matched
- Recon snapshot caching with TTL:
  - `--recon-only`, `--recon-ttl-days`, `--force-recon-refresh`
  - Snapshots stored in `.data/knowledge.db`
- Cache-first pricing runs:
  - normal pricing runs now reuse fresh recon snapshots automatically
  - one automatic refresh is attempted when cached traces appear to have drifted
- Option catalog + strict pre-validation:
  - bootstrap exports `option_catalog`
  - `--require-matched-options` now fails early with catalog-aware suggestions when available
- Onlineprinters request-template synthesis:
  - bootstrap exports request-template hints (`SetLink`, hidden inputs, depvar-related hints)
  - request replay can synthesize Onlineprinters form payloads from requested options
- Trace capture fidelity:
  - Larger same-site POST body capture + truncation metadata (`post_data_len`, `post_data_truncated`)
  - Preserve request/response header values as-is (keys normalized to lowercase)
- Strict request-only pricing mode:
  - `--request-only` enforces **HTTP replay only**
  - No captured-trace preview, DOM extraction, or heuristic fallbacks
  - Bootstrap interactions are gated (no UI option application, no dependency probing)
- Offline regression suite:
  - `python -m unittest discover -s tests -v`
  - covers recon reuse/refresh, header injection, candidate selection, and Onlineprinters synthesis

- Site adapter layer (pluggable replay synthesis):
  - `ExecutionAgent` consults `site_adapters` to build per-site replay candidates (Print24 + Onlineprinters)

- Replay template persistence (validated-only):
  - On deterministic validated success, orchestrator can persist applied request-template metadata to `replay_templates`

---

## Recent work log (2026-04-10)

### 1) Strict request-only mode end-to-end

Implemented `--request-only` across:

- CLI flag + argument incompatibility checks
- Bootstrap gating (disable UI option application and dependency probing)
- Execution enforcement (HTTP replay only)
- Validation guardrails (fail if non-HTTP fallback is used)
- Summary output includes `request_only: true/false`

Key files:

- src/price_extractor/cli.py
- src/price_extractor/agents.py
- src/price_extractor/orchestrator.py
- README.md
- TASKS.md

### 2) Print24 request-only replay fix (missing `portal` header)

Observed failure mode:

- Replaying Print24 API endpoints with `urllib`/curl returned `text/html` (empty body) instead of JSON.

Root cause:

- Print24’s API expects a custom request header: `portal: print24`.
  - This header is present in the site’s own XHR requests.
  - Without it, the server returns HTML instead of JSON.

Fix:

- Capture `portal` in bootstrap traces.
- During HTTP replay, if `portal` is missing but a `portalName` cookie exists, inject `portal=<portalName>`.

Key files:

- src/price_extractor/browser_bootstrap.py
  - request header capture list includes `portal`
- src/price_extractor/agents.py
  - inject `portal` header from cookies when missing

Verification:

- Print24 request-only now succeeds and returns JSON; extracted price observed: 70.47 EUR.

### 3) Request-only smoke tests (Onlineprinters + Saxoprint)

As of 2026-04-10, the README `--request-only` commands succeed for:

- Onlineprinters: validated, HTTP replay success; extracted price observed: 168.39 EUR.
  - Bootstrap still flags `anti_bot=True` on this site, but replay succeeded.
- Saxoprint: validated, HTTP replay success; extracted price observed: 423.05 EUR.
  - Bootstrap may warn that the configurator “did not become ready”; replay can still succeed if the key API endpoints were captured.

Important limitation:

- Onlineprinters request-only replay now synthesizes the replay body from captured `SetLink` / form templates plus option-catalog matches.
- Print24 and Saxoprint still primarily rely on captured replay bodies unless additional site-specific synthesis is added.

### 4) Cache-first pricing runs + refresh-on-drift

Implemented automatic recon snapshot reuse for normal pricing runs:

- Fresh recon snapshots are now reused outside `--recon-only`
- `--force-recon-refresh` now bypasses the cache for pricing runs too
- Cached runs auto-refresh recon once when signals suggest drift:
  - missing replay trace
  - request error
  - HTML/non-price replay result
  - `price_not_extracted`
  - `request_only_http_replay_failed`
  - missing enriched recon metadata (`option_catalog` / `request_templates`)

Summary/artifact additions:

- Normal run summaries now include:
  - `recon_cache`
  - `recon_refresh_reason`
  - `drift_report`
  - `drift_verdict`
- Per-run artifacts now also include:
  - `option_catalog.json`
  - `drift_report.json`

### 5) Onlineprinters option catalog + strict pre-validation

Bootstrap now exports a structured `option_catalog` with:

- normalized group labels
- per-option visible labels / values
- selected flags
- backend hints (`data-varindex`, `data-prnumber`, `data-pimvarnodekey`, control name, href / variant URL where present)

CLI behavior:

- `--require-matched-options` now performs catalog-aware pre-validation before execution when catalog data is available
- On failure, the run exits early with suggested values instead of waiting for replay/validation to fail later
- When catalog data is unavailable, the older post-bootstrap enforcement path still acts as the fallback

### 6) Offline regression suite

Added a default offline regression suite under `tests/` covering:

- recon cache reuse
- force-refresh behavior
- one-time refresh-on-drift
- Print24 `portal` header injection
- Saxoprint / Onlineprinters replay candidate selection
- Onlineprinters catalog pre-validation + request-template synthesis

Default test command:

- `python -m unittest discover -s tests -v`

---

## Recent work log (2026-04-13)

### 1) Site adapter abstraction for HTTP replay synthesis

- Introduced `src/price_extractor/site_adapters.py` to centralize per-site HTTP replay request synthesis.
- Updated `ExecutionAgent` to consult adapters first (Print24 + Onlineprinters), before generic replay candidates.

### 2) Persist validated replay templates in SQLite

- Added `replay_templates` table to SQLite knowledge store.
- Orchestrator persists applied template metadata only on deterministic validated success (guards against poisoned learning).
- Added regression coverage; fixed a Windows SQLite tempdir cleanup failure by ensuring DB connections are closed in tests.

### 3) Bulk-run safety controls for manifest scale

- Added HTTP replay runtime controls (CLI -> bootstrap_signals -> ExecutionAgent):
  - per-host pacing (`--http-min-delay-ms`, `--http-jitter-ms`)
  - retry/backoff (`--http-max-retries`, `--http-backoff-base-ms`, `--http-backoff-max-ms`)
  - timeout (`--http-request-timeout-seconds`)
  - proxy pool + rotation (`--proxy-url`, `--proxy-file`, `--proxy-rotation`)
- Added streaming per-unit JSONL output for long jobs: `--results-jsonl`.
- Added CSV manifest mode (`--manifest-csv`) so row-driven target specs can be executed directly from CSV.
- Added regression tests for retry-on-429 and per-host pacing behavior.
- Added support for one-URL-per-file matrix CSVs that omit `url` by using `--manifest-csv-url`.
- Added compact JSONL output mode (`--results-jsonl-mode compact`) for very large row counts.
- Added proxy health quarantine controls (`--proxy-failure-threshold`, `--proxy-cooldown-seconds`).

### 4) LangGraph onboarding with OpenAI-compatible LLM setup

- Extended `price-onboard-lg` with optional provider-backed suggestions:
  - `--llm-enable`
  - `--llm-model`
  - `--llm-base-url` (defaults to `OPENAI_BASE_URL` or OpenAI public API)
  - `--llm-api-key-env` (defaults to `OPENAI_API_KEY`)
- LLM output is JSON-only suggestions (`llmSuggestions`) and remains non-destructive (no auto-apply).

### 5) Completion of previously pending engineering tasks

- Extended template synthesis beyond Onlineprinters by adding Viaprinto query-template synthesis in site adapters.
- Expanded option-catalog key alias normalization/suggestion coverage for multilingual printing-domain terms.
- Grew offline fixtures beyond Onlineprinters (Viaprinto + Print24 fixtures) and added regression coverage.
- Added LLM-assisted reviewed patch-plan workflow (`patchPlan`) in `price-onboard-lg`, with optional `--patch-plan-out` artifact output and `.env` loading support (`--dotenv-path`).

---

## Status / next tasks

Recently completed:

- Cache-first pricing runs (reuse fresh recon snapshot automatically; refresh on TTL/drift)
- Onlineprinters option catalog export + strict pre-validation UX
- Onlineprinters payload synthesis from captured request templates (persist mappings/templates)
- Drift detection + regression scenarios
- Site adapter layer for per-site replay synthesis (Print24 + Onlineprinters)
- Replay template persistence in SQLite (`replay_templates`) guarded by deterministic validation

Additional practical next steps (operational):

- Run live smoke tests for cache-first + synthesized replay on:
  - Onlineprinters
  - Print24
  - Saxoprint
- Extend request-template synthesis beyond Onlineprinters
- Add/validate a new site adapter (new site onboarding)
- Expand option-catalog normalization / suggestion coverage for more sites
- Continue growing offline fixtures from fresh `.data/runs/...` artifacts
- Add CSV-driven high-volume row ingestion flow (row -> request replay -> per-row result export)

---

## Copy/paste quick tests

These are duplicated from README.md so future chats can run them immediately.

Onlineprinters:

- `python -m price_extractor.cli --url "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4" --request-only --option quantity=250 --option seitig=16 --verbose --fail-on-invalid`

Print24:

- `python -m price_extractor.cli --url "https://print24.com/de/druckprodukte/broschueren/broschueren-klammerheftung-greenline" --request-only --option quantity=250 --option "format=A5" --option material=130gsm --verbose --fail-on-invalid`

Saxoprint:

- `python -m price_extractor.cli --url "https://www.saxoprint.de/broschueren/broschueren-drucken" --request-only --option quantity=250 --option "format=A5" --option material=130gsm --verbose --fail-on-invalid`

---

## Debug playbook (when a site fails)

1) Look at `.data/runs/<site>/<unit_id>/summary.json`:
   - `replay_mode` / `fallback_mode`
   - `http_replay.attempts[]` for status + content-type

2) If HTTP replay returns HTML instead of JSON:
   - Inspect `.data/runs/<site>/<unit_id>/network_traces.json` for the same URL
   - Confirm what headers the browser used (`request_headers`)
   - Add required headers to:
     - capture list in src/price_extractor/browser_bootstrap.py
     - replay injection logic in src/price_extractor/agents.py (if derivable from cookies/signals)

3) If the trace exists but replay still fails:
   - Check cookies in `bootstrap.json` and ensure replay includes them
   - Confirm method + POST body are present (or see truncation flags)

4) If you need to re-capture traces:
   - Run with `--verbose` and/or `--headed` to inspect behavior

---

## Notes / constraints

- `--request-only` is intentionally strict:
  - no UI option clicking/probing
  - no captured-response-preview extraction
  - no DOM extraction
  - no heuristic fallback

- `--request-only` cannot be combined with:
  - `--recon-only`
  - `--require-matched-options`
  - `--allow-heuristic-fallback`

- Sites change frequently; always validate against the latest traces in `.data/runs/`.
