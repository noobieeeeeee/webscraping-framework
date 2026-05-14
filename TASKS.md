# Task Tracker

Status: `[x] done` / `[ ] pending`

Last updated: 2026-05-12

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
- [x] Add request-family semantics to DiscoveryAgent (pricing/schema/quantity/validation/infrastructure)
- [x] Add semantic clustering heuristics for related endpoint tokens (productdetails/repo/price-matrix/pricescale)
- [x] Extend endpoint diagnostics with request_family, semantic_signals, penalties, payload_signals
- [x] Add relationship inference diagnostics between endpoints (tokens, payload, response, URL prefix)
- [x] Add request-family cluster summaries for ranked endpoints
- [x] Add replay trace normalization and reconciliation diagnostics for endpoint matching
- [x] Add fuzzy replay trace matching for path prefixes, containment, and semantic overlap

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

## Stabilization (semantic discovery)

- [x] Substitute matched trace URL for HTTP request when match strategy is `path_prefix` or `path_containment` (closes stale/template candidate URL gap)
- [x] Fix cross-class `self.` references that prevented `_find_trace` from running (qualified to `DiscoveryAgent.`)
- [x] Rescue pricing-family endpoints (`pricing_pipeline` / `quantity_pipeline` / `schema_pipeline`) from the `score > 0` gate in `_build_http_replay_candidates`. Infrastructure / unknown remain excluded. Sources are tagged `family_rescued_<family>_<idx>` for diagnostics.
- [x] Widen readiness + dependency-probe selectors to cover modern React/ARIA configurator controls (`[role="radio"]`, `[role="checkbox"]`, `[role="tab"]`, `[aria-pressed]`, `[aria-selected]`, `[aria-checked]`, `[data-testid*="option"]`, `[data-cy*="option"]`). Closes the "captured mostly static assets" warning when configurator uses ARIA primitives instead of native form controls.
- [x] Extend cookie consent to Usercentrics / Didomi vendors and add a shadow-DOM walker fallback that traverses shadowRoot chains and avoids deny-pattern buttons (`reject`, `nur erforderliche`, etc.).
- [x] Add semantic-label probe (`trigger_configurator_via_semantic_labels`) that localizes configurator option groups by visible label text and clicks one option per matched group. Default vocabulary covers German printing terms (Menge/Auflage, Papier, Format, Farbigkeit, Seiten, Bindung, Veredelung, Ausrichtung); callers can supply other vocabularies. Runs after readiness when no controls/API hits were detected; emits diagnostics with `requestCountDelta` and `pricingApiHitsDelta`.
- [x] Raise readiness budget cap from `min(12000, timeout_ms // 2)` (10s) to `min(timeout_ms, 30000)` (20s with default 20s timeout, up to 30s). Add deferred consent retry inside the readiness poll loop: every ~2.5s, re-run `attempt_cookie_consent` if not yet clicked. Addresses late-rendering Usercentrics / Didomi banners that mount after the initial consent attempt and block configurator hydration.
- [x] Expose readiness, deferred-consent, and label-probe diagnostics on the CLI (`[bootstrap-readiness]`, `[bootstrap-consent-retry]`, `[bootstrap-label-probe]`, `[bootstrap-label-probe-groups]`, `[bootstrap-label-probe-clicks]`).
- [x] End-to-end regression smoke test (`test_print24_end_to_end_replay_from_captured_fixture`) drives `_try_http_replay` with slim fixtures derived from a real successful print24 run (network_traces, cookies, generated JSON adapter). Locks in the captured-artifacts -> replay -> adapter chain.
- [x] Surface skipped JSON-adapter injections (`apply_injections` now collects `value_not_in_map` / `cast_failed` / `path_unreachable` entries; `build_adapter_http_candidate` exposes them on `request_template_applied.skipped`; CLI prints a dedicated `[adapter-injection-skipped]` line). Closes the silent-substitution bug where `require_mapped=True` + an unmapped user value returned a price for the wrong configuration.
- [x] Introduce shared option-key alias table (`OPTION_KEY_ALIASES` in `agentic/onboarding.py`) covering quantity, format, material, pages, color, binding, finishing, orientation with German + English aliases. `_infer_option_key_from_path`, the `properties.name` walker, and `agentic/adapter_generation._group_label_matches_option` / `_build_value_map_from_catalog` all route through it.
- [x] Expand `_build_inject_rules` (`agentic/adapter_generation.py`) past `["quantity", "format"]` to the full canonical set, so generated JSON adapters can parameterize color/binding/finishing/orientation/pages/material when the upstream proposer emits paths for them.
- [x] Surface coverage gaps in adapter inject pipeline. `json_adapters.build_adapter_http_candidate`, `Print24SiteAdapter`, and `OnlineprintersSiteAdapter` (both copies — site_adapters.py + agents.py) now emit `request_template_applied.unsupportedInputs` for user-supplied `--option` keys with no rule, and Onlineprinters additionally emits `skipped` for matched catalog rows whose metadata is too thin to mutate the form. CLI prints `[adapter-injection-unsupported]` with an explanatory note. Closes the silent-no-op on paper/material when the user passes an option the adapter cannot apply.
- [ ] **Open blocker (partial)** — bootstrap option-catalog extraction for paper/material/etc. on Onlineprinters (singleton-per-paper groups missing parent "Material" grouping; matched rows lack form-field `name` / `variantUrl` / `backendHints`) and Print24 (catalog entirely empty: 4 groups all with `None` label and `0` options). Without this, the `[adapter-injection-unsupported]` and `[adapter-injection-skipped]` diagnostics will fire for every paper/color/binding option on those sites. Fix path: improve DOM grouping detection + capture form-field `name` attribute + `variantUrl`/`href` on each option; for Print24, identify the configurator option-group widget at all.
- [x] **Generalized value-map sideload (Saxoprint live, Print24 migrated).** New `src/price_extractor/value_map_loader.py` auto-discovers `value_maps/<site>.json` at repo root and resolves `(canonical_key, requested_value) → (propertyId, backendId)` via the same NFKD+lowercase+collapse-whitespace+exact-or-substring matcher Print24 uses. Schema v1: per-property entries with `canonicalKey` (routed through `OPTION_KEY_ALIASES`), `sourceLabel`, optional `productScope` (advisory, logged not gating), and `options[].{backendId, label, confidence}` where confidence is `confirmed|partial|ui_only`. Default accepts `confirmed+partial`; `--allow-sideload-ui-only` opts into the rest. `_prevalidate_requested_options` augments matched rows with `sideloadResolution` and promotes previously-unmatched keys to `matchType=sideload_only` so sites with no catalog (Saxoprint DOM walker captures nothing) still produce structured prevalidation. `Print24SiteAdapter` (and the `agents.py` duplicate) consumes `sideloadResolution` with precedence `catalog_applied > sideload > hardcoded`; sideload explicitly overrides `catalog_skipped` (the case where catalog had the group but couldn't resolve the requested value — sideload may know the prop_id for that value). `[sideload-loaded]` and `[adapter-injection-applied-sideload]` CLI diagnostics surface what loaded and what applied. `value_maps/saxoprint.de.json` (15 properties, 124 options, translated from `saxoprint_mappings.json`) and `value_maps/print24.com.json` (the migrated A5/A6+250/10 hardcoded dicts) are checked in; the hardcoded class constants in `Print24SiteAdapter` remain as a final fallback this turn (deletion is a follow-on after a cache-hit smoke verifies parity). 30 new tests across `tests/test_value_map_loader.py` (schema parsing, confidence filtering, ambiguity/collision/no-match outcomes, NFKD parity with Print24 matcher, end-to-end against the checked-in JSON, Print24 adapter precedence integration).
- [x] **Print24 SiteAdapter consumes the harvested catalog (v1.5).** `Print24SiteAdapter` (and the agents.py duplicate) now pulls prop_ids from `option_prevalidation.matched` rows as PRIMARY, with the existing A5/A6/250/10 hardcoded mappings retained as fallback. New strict `_value_matches_captured_label` (NFKD + lowercase + collapse-whitespace + exact-or-substring) prevents the silent-wrong-prop_id bug where prevalidation's loose alias matching pairs "115 g/m² Bilderdruckpapier" with the captured 130 g/m² row. New `skipped` entries surface `reason=prop_id_unknown_for_value` with captured value + capturedPropId + a pointer to print24_feasibility_notes.md §3.1/§9. Catalog skipped does NOT fall through to hardcoded — those mappings are product-specific. Hardcoded class constants required `ClassVar` typing to avoid `@dataclass` mutable-default rejection. 16 new tests; verified end-to-end across captured/paper-change/quantity-change/unknown-value scenarios. Open follow-on: `repo` probe in bootstrap (Playwright-side) to harvest non-captured alternatives.
- [x] **Print24 catalog harvesting from `productDetails` (v1).** New `print24_catalog.harvest_print24_catalog_from_traces` parses captured `productDetails` AJAX responses (per `print24_feasibility_notes.md` §3.3) and produces standard-shape catalog rows: one option per `prop_details` entry, with canonical `box_name` as the option name and `prop_id` under `backendHints.dataPropertyId`. Wired into `_catalog_groups_from_bootstrap` so Print24 bootstraps (whose DOM walker captures nothing useful — option widgets live in Next.js client components) now produce a usable catalog. Extended `_prevalidate_requested_options` with a new `catalog_only` quantity match type (Print24 has `hasManualInput=False` + empty `presetValues`, so the existing tile/manual paths don't validate quantity; the catalog row's structured prop_id is enough). Quantity backstop now also accepts `name == "quantity"` (Print24 canonical box_name), not just `input_var_*`. 8 new tests; verified against `unit_4f793d8.../bootstrap.json` for the captured config (quantity prop_id=339, format=222, material=9413). Open v2: bootstrap-side `repo` probe to harvest non-captured alternatives, or `Print24SiteAdapter` consuming the harvested catalog with `option_code_unknown_for_value`-style diagnostic for non-captured values.
- [x] **Interpolation-mode quantity for Onlineprinters (per `onlineprinters_request_modification.md` §5B).** New `form_field_inference.quantity_tile_signals` extracts the tile preset list + `Höhere Auflage` Interpolation tile from the DOM-captured catalog (driven by the variant-scrape from approach b + the cap lift). `_prevalidate_requested_options` stamps `quantityTileSignals` on the matched quantity row, with a backstop pass so off-preset typed quantities (e.g. 12345 when presets jump 11000/12000/13000) still get a quantity field handle. `OnlineprintersSiteAdapter._apply_interpolation_quantity` writes `"Interpolation"` to the qty field AND updates the `input_qty_1` IMMEDIATELY AFTER the qty field in-place (per the MD spec, appending breaks the request). The setlink option_code becomes the Interpolation tile's data-varindex. Tile mode also gains per-tile data-varindex precision (was using whatever data-varindex the prevalidation-picked option happened to carry). Verified end-to-end against real bootstrap for quantities at 250 / 5000 / 19000 (tile) and 12345 / 25000 (interpolation). 9 new tests. Open: combined paper-change + interpolation-quantity (template substitution of qty-suffix in the new-paper varindex) — flagged but not yet closed.
- [x] **Lift `response_body_preview` cap for same-site POSTs (1 MB).** `browser_bootstrap.py` previously capped previews at 16 KB which truncated Onlineprinters' WEBSALE configurator AJAX, leaving the variant-scrape blind to most paper options. Same-site POSTs now get a 1 MB cap (covers Onlineprinters product-page POSTs, Print24 `repo`/`price-matrix`, Saxoprint `get-product-prices`); cross-origin traces stay at 16 KB so bootstrap.json size doesn't blow up. Added `response_body_len` and `response_body_preview_truncated` to each trace so a future over-1MB response surfaces explicitly. After re-running bootstrap, all paper variants for a product should now land in the catalog with full `dataVarindex` — closes the residual `option_code_unknown_for_value` skips that were visible in the user's `state_snapshot.json` snapshot from before approach (b) shipped.
- [x] **Onlineprinters paper-switching via variant scraping (approach b).** Extended `form_field_inference.py`: `parse_html_option_inputs` scans configurator AJAX response HTML for `<input ... name="input_var_..." data-varindex="...">` blocks (handles JSON-escaped HTML), `collect_variants_from_traces` aggregates across all captured `response_body_preview` strings, `build_form_field_map_from_traces` combines baseline-POST current-selection metadata with response-HTML variant codes. `enrich_catalog_with_form_fields` now supplies the FULL option code for non-current variants from the variants index. Fixed a quantity_manual-vs-paper-field collision in `_prevalidate_requested_options` (require numeric visibleValue, not just any option-with-a-name). Verified end-to-end against the user's real `bootstrap.json`: `--option material="115 g/m² Bilderdruckpapier"` produces a synthesized POST with `input_var_PBRA444_1_1="115 g/m² Bilderdruckpapier"`, `input_var_PBRA444_3_1="250"`, and setlink `depvar_index_setparent=<PBRA444><PBRA444.115.081000>`. Open caveats: response_body_preview cap (16000 chars) limits how many variants are scraped; interpolation-mode quantity (`onlineprinters_request_modification.md` §5B) not yet handled.
- [x] **Form-field inference from captured POSTs (approach 2 for Onlineprinters paper).** New `src/price_extractor/form_field_inference.py` parses captured baseline POST: extracts `input_var_<PROD>_<G>_<P>` fields (current value), decodes triple-encoded SetLink, recovers `(depvarGroupCode, depvarOptionCode)` per position. `enrich_catalog_with_form_fields` attaches form `name` to every option in a matched group; the currently-selected option gets full backendHints (dataVarindex from setlink), others get `optionCodeUnknown=True`. `OnlineprintersSiteAdapter` now refuses to write non-current options without an option code, emitting `[adapter-injection-skipped]` with `reason: option_code_unknown_for_value` per `onlineprinters_request_modification.md` §3.3 (writing label alone causes silent backend normalization). Verified end-to-end against the user's real `bootstrap.json`: current paper enriches fully → mutation succeeds; alternate paper surfaces the precise gap. Open follow-on: probe-through OR HTML-scrape variant URLs to learn alternate paper codes.
- [x] **Onlineprinters parent-group detection (partial fix for above).** New `src/price_extractor/option_catalog_postprocess.regroup_singleton_clusters` collapses singleton paper/color/binding/finishing groups into synthetic canonical parent groups (`material`, `material (umschlag)` / `material (innen)`, `color`, `binding`, `finishing`). Wired into the `_catalog_groups_from_bootstrap` summary-fallback path so pre-validation now matches `--option material="115 g/m² Bilderdruckpapier"` against a `material (innen)` parent group with the right paper variant inside (verified end-to-end against the user's real onlineprinters bootstrap snapshot). Still does NOT close the form-mutation gap — the synthesized options carry no `name`/`backendHints` because the source summary never had them; `[adapter-injection-skipped]` now fires with `reason: catalog_row_missing_form_metadata` for the right reason. Next: enrich the in-browser JS extractor or learn form-field names from captured network traces (SetLink decoding per `onlineprinters_request_modification.md`).
- [ ] Use request-family clusters to de-noise endpoint ranking (no replay changes yet)
- [ ] Add per-family rollups for endpoint diagnostics (pricing/schema/quantity/validation/infrastructure)
- [ ] Evaluate infrastructure-family false positives across additional sites
- [ ] Introduce family-aware planner hints without changing replay behavior

## Known bottlenecks

- Endpoint ranking is still noisy for multi-call pricing flows
- Replay remains endpoint-centric even when pricing requires a pipeline
- Infrastructure endpoints can outscore weak pricing signals without family gating
- Endpoint relationships are diagnostic only; replay does not consume them yet
- Replay still lacks family-aware sequencing and cross-endpoint template assembly

## Replay reconciliation

- Replay trace matching now normalizes URLs before matching and records diagnostics per attempt
- Matching remains conservative and incremental; there is no replay orchestration layer
- Fuzzy-matched candidates (`path_prefix` / `path_containment`) now substitute the matched trace URL for the actual HTTP request, so stale or templated candidate URLs no longer hit the server with missing path segments. Exact matches are left unchanged.
- Trace-matching utilities (`_normalize_replay_url`, `_score_trace_match_candidate`, `_build_trace_match_diagnostics`) live on `DiscoveryAgent`; `ExecutionAgent` now references them via `DiscoveryAgent.` rather than `self.` (they are already `@staticmethod` / `@classmethod`). This is the minimum fix to make trace matching actually run during replay.
- Remaining limitation: endpoint candidates are still matched one at a time, not as a generalized template graph

## Recent verification

- Recon-only (fresh traces with fidelity fix): `.data/runs/recon-onlineprinters-tracefix.json`
- Full run (options + replay still succeeds): `.data/runs/onlineprinters-tracefix-price.json`
- Print24 request-only seed (forced refresh) validated: `.data/knowledge-smoke.db` (response dump under `.data/http-responses/print24.com/`)
- Print24 request-only cache-hit validated: `.data/knowledge-smoke.db` (response dump under `.data/http-responses/print24.com/`)
- Saxoprint request-only seed (forced refresh) validated: `.data/knowledge-smoke.db` (response dump: `.data/http-responses/saxoprint.de/20260411T104156Z_bebaf1b254f9_status200.json`)
- Saxoprint request-only cache-hit validated: `.data/knowledge-smoke.db` (response dump: `.data/http-responses/saxoprint.de/20260411T104505Z_bebaf1b254f9_status200.json`)
