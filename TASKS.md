# Task Tracker

Status: `[x] done` / `[ ] pending`

Last updated: 2026-04-11

## Done

- [x] Fix HTTP replay endpoint selection after redirects (Onlineprinters)
- [x] Add repeatable CLI overrides: `--option key=value` (with deterministic precedence + scalar coercion)
- [x] Add strict option enforcement: `--require-matched-options` (surface matched/unmatched in summary)
- [x] Persist recon snapshots to SQLite with TTL (`--recon-only`, `--recon-ttl-days`, `--force-recon-refresh`)
- [x] Trace capture fidelity:
  - [x] Preserve request/response header **value** casing (avoid breaking auth/CSRF tokens)
  - [x] Improve POST body capture (store `post_data_len` + `post_data_truncated`, raise same-site body cap)
- [x] Add strict **request-only** pricing mode (`--request-only`): disable UI option application/probing and require HTTP replay (no captured-trace/DOM/heuristic fallbacks)
- [x] Print24 request-only replay synthesis (rewrite `productDetails` payload `properties[].id` for requested `format`/`quantity`)
- [x] Prevent poisoned normalization rules from overriding core keys (`format`, `quantity`) during HTTP replay
- [x] Gate normalization learning/persistence to only validated runs (avoid storing bad mappings)
- [x] Verbose logging: print outgoing request payload (headers redacted)
- [x] Persist HTTP replay response body to disk (see `.data/http-responses/`)
- [x] Semantic validation for server-humanized config values (e.g., `250` vs `250 Stück`, `A5` vs DIN A5 description)
- [x] Add site adapter layer for per-site HTTP replay synthesis (Print24 + Onlineprinters)
- [x] Persist validated replay templates/mappings to SQLite (`replay_templates`)
- [x] Add HTTP replay runtime controls for scale: per-host pacing, retry/backoff, proxy pool + rotation
- [x] Add streaming per-unit JSONL output for large manifest runs (`--results-jsonl`)
- [x] Add CSV manifest ingestion (`--manifest-csv`) for row-driven target execution
- [x] Support one-URL-per-file semicolon CSV matrices via `--manifest-csv-url` fallback
- [x] Add compact per-row JSONL schema for large matrix runs (`--results-jsonl-mode compact`)
- [x] Add proxy failure quarantine controls (`--proxy-failure-threshold`, `--proxy-cooldown-seconds`)
- [x] Add OpenAI-compatible LangGraph onboarding setup (`--llm-enable`, model/base URL/API-key env controls)

## Next

- [x] Cache-first pricing runs (reuse fresh recon snapshot automatically; refresh on TTL/drift)
- [x] Onlineprinters option catalog export + strict pre-validation UX
- [x] Onlineprinters payload synthesis from captured request templates (persist mappings/templates)
- [x] Drift detection + regression scenarios

## New next

- [x] Live smoke validation of cache-first + synthesized replay on Onlineprinters (seed + cache-hit)
- [x] Live smoke validation of cache-first + synthesized replay on Print24 (seed + cache-hit)
- [x] Live smoke validation of cache-first + synthesized replay on Saxoprint (seed + cache-hit)
- [x] Extend template synthesis beyond Onlineprinters (Viaprinto query-template synthesis)
- [x] Expand option-catalog normalization and suggestion coverage for more sites
- [x] Grow offline fixture set for more sites (Viaprinto + Print24 fixtures)
- [x] Add dedicated per-row result schema/export for million-row price matrix extraction workflows
- [x] Add LLM-assisted adapter code proposal workflow (`llmSuggestions` -> reviewed patch plan) without auto-apply

## Next frontier

- [ ] Add model-eval harness for side-by-side provider/model comparison on onboarding quality (JSON validity + deterministic acceptance rate)

## Recent verification

- Recon-only (fresh traces with fidelity fix): `.data/runs/recon-onlineprinters-tracefix.json`
- Full run (options + replay still succeeds): `.data/runs/onlineprinters-tracefix-price.json`
- Print24 request-only seed (forced refresh) validated: `.data/knowledge-smoke.db` (response dump under `.data/http-responses/print24.com/`)
- Print24 request-only cache-hit validated: `.data/knowledge-smoke.db` (response dump under `.data/http-responses/print24.com/`)
- Saxoprint request-only seed (forced refresh) validated: `.data/knowledge-smoke.db` (response dump: `.data/http-responses/saxoprint.de/20260411T104156Z_bebaf1b254f9_status200.json`)
- Saxoprint request-only cache-hit validated: `.data/knowledge-smoke.db` (response dump: `.data/http-responses/saxoprint.de/20260411T104505Z_bebaf1b254f9_status200.json`)
