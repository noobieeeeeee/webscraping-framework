# Agentic Price Extraction MVP

This repository contains an MVP framework for dynamic price extraction feasibility and execution.

## Quick Setup

### Using conda (recommended):
```bash
conda env create -f environment.yml
conda activate price-extractor
playwright install chromium
```

### Using pip:
```bash
pip install -r requirements.txt
playwright install chromium
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed setup options.

## What is implemented

- Feasibility-first pipeline
- Structured run state and typed outputs
- Agent modules:
  - Discovery
  - Planner
  - Executor
  - Validator
  - Repair
- Orchestrator with bounded self-correction loop
- SQLite-backed knowledge store for endpoint and failure pattern memory
- CLI entry point

## Run

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --product-type flyer --options-file configs/sample_options.json
```

URL-first mode:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --product-type flyer --options-json "{\"quantity\":250,\"format\":\"A5\"}"
```

Repeatable option flags (shell-friendly for keys like seitig):

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --option quantity=250 --option seitig=16 --option "format=DIN A4"
```

Option precedence is deterministic when multiple sources are provided:

`--option` > `--options-json` > `--options-file` > manifest/spec defaults.

Require that all requested options are actually matched on page controls:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --option seitig=16 --require-matched-options --fail-on-invalid
```

Strict request-only pricing (HTTP replay only; no captured-trace/DOM/heuristic fallbacks):

Note: `--request-only` cannot be combined with `--recon-only`, `--require-matched-options`, or `--allow-heuristic-fallback`.

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --request-only --verbose --fail-on-invalid
```

Recon-only mode (discover visible options/endpoints and cache locally, skip pricing):

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --recon-only --output-file .data/runs/recon.json
```

Refresh stale or changed site recon explicitly:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --recon-only --force-recon-refresh --recon-ttl-days 7
```

Pricing runs now reuse a fresh cached recon snapshot automatically when one exists. Use `--force-recon-refresh` to bypass the cache for a pricing run as well.

Persist single-run output JSON and fail CI on invalid extraction:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --output-file .data/runs/latest.json --fail-on-invalid
```

Verbose terminal logging (bootstrap signals, discovered options, plan, extraction/validation decisions):

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --verbose
```

Verbose mode now includes a ranked endpoint table (`[endpoint-candidates]`) showing score, role, confidence, method, status, and URL.

Cookie consent handling and headed debug inspection:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --headed --headed-debug-hold-seconds 20 --verbose
```

Disable automatic consent clicking heuristics if needed:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --disable-auto-accept-cookies
```

Tune active dependency-probe interaction budget:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --max-dependency-probe-steps 10
```

Expected-price assertion with custom tolerance:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --expected-price 24.90 --expected-price-tolerance 0.05 --fail-on-invalid
```

Enable synthetic fallback pricing only when you explicitly want it:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --allow-heuristic-fallback
```

Safer option for shell compatibility:

```bash
python -m price_extractor.cli --url "https://example.com/product/xyz" --product-type flyer --options-file configs/sample_options.json
```

or after install:

```bash
price-agent --url "https://example.com/product/xyz" --product-type flyer --options-file configs/sample_options.json
```

Onboarding helper (proposal generator):

After you have a recon/pricing run directory under `.data/runs/<site>/<unit_id>/`, generate a structured proposal for what code to write next (endpoints to target, blockers, and a `SiteAdapter` stub):

```bash
price-onboard --run-dir .data/runs/<site>/<unit_id>
```

Or without installing scripts:

```bash
python -m price_extractor.agentic.onboarding --run-dir .data/runs/<site>/<unit_id>
```

LangGraph variant (resume-friendly agentic framework scaffold):

Install the optional agentic extras:

```bash
pip install -e .[agentic]
```

Optional: create a local `.env` from `.env.example` and put API keys there.

Then run:

```bash
price-onboard-lg --run-dir .data/runs/<site>/<unit_id>
```

Enable real LLM suggestions (OpenAI-compatible, optional):

1) Set API key env var (PowerShell):

```powershell
$env:OPENAI_API_KEY = "<your_api_key>"
```

2) Optionally set custom provider base URL (if not OpenAI):

```powershell
$env:OPENAI_BASE_URL = "https://<provider-host>/v1"
```

Groq (OpenAI-compatible) example:

```powershell
$env:GROQ_API_KEY = "<your_groq_key>"
```

```bash
price-onboard-lg --run-dir .data/runs/<site>/<unit_id> --llm-enable --llm-model llama-3.3-70b-versatile --llm-base-url https://api.groq.com/openai/v1 --llm-api-key-env GROQ_API_KEY
```

Gemini note: no full rewrite is required. Gemini can also be used through an OpenAI-compatible endpoint. A dedicated helper command is now available with Gemini defaults:

```powershell
$env:GEMINI_API_KEY = "<your_gemini_key>"
```

```bash
price-onboard-gemini --run-dir .data/runs/<site>/<unit_id> --llm-model gemini-2.0-flash --out .data/proposals/<site>-gemini-proposal.json --patch-plan-out .data/proposals/<site>-gemini-patch-plan.json
```

3) Run LangGraph onboarding with LLM enabled:

```bash
price-onboard-lg --run-dir .data/runs/<site>/<unit_id> --llm-enable --llm-model gpt-4.1-mini --out .data/proposals/<site>-proposal.json
```

4) Emit a reviewed patch-plan artifact (JSON only, no auto-apply):

```bash
price-onboard-lg --run-dir .data/runs/<site>/<unit_id> --llm-enable --patch-plan-out .data/proposals/<site>-patch-plan.json
```

Use a custom API key variable name if preferred:

```bash
price-onboard-lg --run-dir .data/runs/<site>/<unit_id> --llm-enable --llm-api-key-env MY_PROVIDER_API_KEY --llm-base-url https://<provider-host>/v1
```

If your key is in `.env`, pass the dotenv path explicitly:

```bash
price-onboard-lg --run-dir .data/runs/<site>/<unit_id> --llm-enable --dotenv-path .env
```

Notes:
- LLM output is JSON suggestions only (`llmSuggestions`); no code is auto-applied.
- Deterministic extraction/persistence rules remain unchanged.

Manifest mode (multi-target run with checkpointing):

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --checkpoint-file .data/checkpoints/print_job.json
```

CSV manifest mode (one row per target; supports columns like `option.quantity`, `option.format`, or `options_json`):

```bash
python -m price_extractor.cli --manifest-csv configs/sample_manifest.csv --request-only --checkpoint-file .data/checkpoints/csv_job.json
```

For one-URL-per-file matrix CSVs (rows only contain configuration columns, no `url` column), pass a default URL:

```bash
python -m price_extractor.cli --manifest-csv configs/sample_matrix.csv --manifest-csv-url "https://example.com/product/xyz" --site-name example-site --request-only --results-jsonl .data/runs/matrix-results.jsonl
```

Use compact row output for very large runs (default):

```bash
python -m price_extractor.cli --manifest-csv configs/sample_matrix.csv --manifest-csv-url "https://example.com/product/xyz" --results-jsonl .data/runs/matrix-results.jsonl --results-jsonl-mode compact
```

Streaming per-unit output for long manifest runs (JSONL):

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --results-jsonl .data/runs/bulk-results.jsonl
```

Retry only previously failed units on resume:

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --checkpoint-file .data/checkpoints/print_job.json --retry-failed
```

HTTP replay pacing + retry/backoff controls (recommended for large runs):

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --request-only --http-min-delay-ms 500 --http-jitter-ms 250 --http-max-retries 2 --http-backoff-base-ms 500 --http-backoff-max-ms 5000 --results-jsonl .data/runs/bulk-results.jsonl
```

Proxy pool controls for replay requests (use only where you are authorized to scrape):

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --request-only --proxy-file configs/proxies.txt --proxy-rotation round_robin --http-min-delay-ms 700 --http-jitter-ms 400
```

Optional proxy quarantine controls:

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --request-only --proxy-file configs/proxies.txt --proxy-rotation round_robin --proxy-failure-threshold 3 --proxy-cooldown-seconds 300
```

Staged safe-scale rollout (single-config -> probe -> pilot -> staging -> production):

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-job-db .data/checkpoints/staged_job_state.db --results-jsonl .data/runs/staged-results.jsonl
```

Use a custom stage policy (budgets/gates/runtime envelope):

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-policy-file configs/sample_staged_policy.json --staged-job-db .data/checkpoints/staged_job_state.db
```

Combine primary manifest units with an additional generated-unit manifest:

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-generated-manifest-file configs/sample_manifest.json --staged-rollout
```

Non-interactive staged runs (auto-approve pilot promotion):

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-auto-approve
```

Generate per-product agentic onboarding artifacts during staged runs (proposal JSON + adapter stub code):

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-auto-approve --staged-generate-agentic-proposals --staged-proposals-dir .data/proposals/staged
```

Tune onboarding endpoint context size for each generated proposal:

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-generate-agentic-proposals --staged-proposal-top-n 15
```

Use LangGraph payload generation for each staged product proposal (with optional LLM):

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-generate-agentic-proposals --staged-agentic-use-langgraph --staged-agentic-llm-enable --staged-agentic-llm-model gpt-4.1-mini --staged-agentic-dotenv-path .env
```

Staged generation with Groq:

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-generate-agentic-proposals --staged-agentic-use-langgraph --staged-agentic-llm-enable --staged-agentic-llm-model llama-3.3-70b-versatile --staged-agentic-llm-base-url https://api.groq.com/openai/v1 --staged-agentic-llm-api-key-env GROQ_API_KEY
```

Staged generation with Gemini:

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-generate-agentic-proposals --staged-agentic-use-langgraph --staged-agentic-llm-enable --staged-agentic-llm-model gemini-2.0-flash --staged-agentic-llm-base-url https://generativelanguage.googleapis.com/v1beta/openai --staged-agentic-llm-api-key-env GEMINI_API_KEY
```

Optional staged LangGraph provider controls:

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-generate-agentic-proposals --staged-agentic-use-langgraph --staged-agentic-llm-enable --staged-agentic-llm-base-url https://<provider-host>/v1 --staged-agentic-llm-api-key-env OPENAI_API_KEY --staged-agentic-llm-timeout-seconds 90
```

Tune staged circuit-breaker thresholds:

```bash
python -m price_extractor.cli --manifest-file configs/sample_manifest.json --staged-rollout --staged-breaker-window 60 --staged-breaker-min-events 15 --staged-breaker-block-rate 0.15 --staged-breaker-server-error-rate 0.25
```

Export long-run JSONL results to CSV for quick analysis:

```bash
price-results-export --jsonl .data/runs/results.jsonl --csv .data/runs/results.csv
```

## Notes

This MVP accepts URL input and uses browser bootstrap to capture request/token/session hints and network traces.
Execution now attempts trace-driven HTTP replay first for direct/hybrid strategies.
If replay is unavailable, execution attempts deterministic extraction from captured API response previews and browser DOM price candidates.
Heuristic fallback pricing is disabled by default and only used when `--allow-heuristic-fallback` is enabled.
Discovery now classifies endpoint roles (schema/pricing/quantity/delivery/validation/noise) and uses payload/response evidence to drive feasibility and strategy selection.
Validation now compares intended options against server-accepted configuration inferred from replay responses and flags normalization/fallback signals when values drift.
Validation now fails explicitly with `price_not_extracted` if no deterministic price was extracted.
Bootstrap now inspects DOM controls to estimate option-group count and quantity mode hints (manual/preset/mixed/unknown), and these signals are included in run summaries.
Bootstrap now attempts generic cookie-consent acceptance heuristics and records consent outcome in artifacts and verbose logs.
Bootstrap now attempts requested option application (including quantity input where detectable) and records matched/unmatched options plus triggered request URLs.
Bootstrap now exports a structured `option_catalog` plus request-template hints that can be reused from cached recon snapshots.
CLI now supports repeatable per-option overrides via `--option key=value` with scalar coercion (`true/false`, numbers, null/none).
Use `--require-matched-options` when you want the run to fail if requested options are not present in the exported option catalog; when catalog data is available, the CLI now fails fast with suggested values before execution.
Recon snapshots are persisted in the local knowledge DB with a TTL (default: 7 days) and can be reused automatically in pricing runs or recon-only flows to avoid repeating browser recon.
Cached pricing runs now refresh recon once when traces appear to have drifted (for example: missing replay traces, request errors, HTML instead of structured pricing data, or request-only replay failure).
Request replay can synthesize dynamic payloads from captured templates and catalog-backed option matches instead of always replaying stale default bodies.
Bootstrap infers option dependency edges from DOM metadata hints and surfaces quantity threshold behavior when both presets and manual input exist.
Bootstrap now performs limited active probing (small interaction budget) to detect cross-option dependency effects and reports active dependency edge counts.
Per-run artifacts are written by default under `.data/runs/<site>/<unit_id>/` with `summary.json`, `spec.json`, `bootstrap.json`, `network_traces.json`, `option_catalog.json`, `drift_report.json`, and `state_snapshot.json`.
Some artifacts can contain session-related data (for example cookies and request metadata). Treat `.data/` as sensitive local runtime output and avoid sharing or committing it.
CLI now supports checkpoint/resume for long jobs:
- Completed units are skipped on subsequent manifest runs.
- Failed units are skipped by default on manifest runs until `--retry-failed` is provided.
- Single URL runs always execute immediately (no checkpoint-based skipping).
- Checkpoint JSON is persisted after every processed unit.
Template synthesis and replay candidate generation are implemented through pluggable site adapters. Sites without adapter coverage still rely on captured replay bodies unless additional site-specific logic is added. See [CONTRIBUTING.md](CONTRIBUTING.md) for instructions on adding new sites.

Offline regression tests are available via:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

PowerShell equivalent:

```powershell
$env:PYTHONPATH='src'; python -m unittest discover -s tests -v
```

To enable browser bootstrap, install optional browser dependencies:

```bash
pip install -e .[browser]
```

If Playwright browser binaries are not installed, run:

```bash
playwright install chromium
```
