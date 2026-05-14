# Multi-chat context (handoff)

Last updated: 2026-05-13

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

---

## Current architecture state (2026-05-11)

### Request-family discovery (semantic foundation)

- DiscoveryAgent assigns a request family per endpoint: pricing_pipeline, schema_pipeline, quantity_pipeline, validation_pipeline, infrastructure, or unknown.
- Family assignment uses lightweight token heuristics across URL, payload keys, response keys, and response preview.
- Endpoint rankings now include request_family, semantic_signals, payload_signals, and penalties for diagnostics and future clustering.
- Replay behavior remains unchanged (endpoint-centric); request families are diagnostic only for now.

### Endpoint labels vs request relationships

- Endpoint labels (roles + request_family) describe single requests, not how they relate.
- Discovery now infers lightweight relationships between endpoints using shared tokens, payload keys, response keys, URL prefix, and semantic hints.
- Each ranked endpoint exposes relatedEndpoints, relationshipReasons, and relationshipConfidence as diagnostics only.

### Trace normalization model

- Replay trace matching now normalizes URLs before reconciliation.
- Normalization includes lowercase handling, query/fragment stripping, duplicate slash collapsing, trailing slash normalization, and path token normalization.
- Fuzzy matching supports prefix overlap, path containment, dynamic suffixes, parameterized endpoints, and semantic path overlap.
- Replay attempts now expose normalizedEndpoint, candidateTraceCount, matchedTraceUrl, matchStrategy, matchConfidence, and mismatchReason.

### Replay reconciliation strategy

- Replay still chooses one trace per planned endpoint, but matching is now normalized and fuzzier than raw string equality.
- Candidate prioritization prefers traces with pricing semantics, POST bodies, quantity semantics, and replayable request payloads.
- For `path_prefix` and `path_containment` matches, the actual HTTP request now targets the matched trace URL (current session, validated by the browser) instead of the candidate URL (which may be a stale or templated form lacking path segments or query params). Exact matches (`normalized_exact`, `normalized_path_exact`, `trace_override`) continue to use the candidate URL unchanged. Substitutions are recorded in attempt diagnostics (`endpointSubstituted`, `originalCandidateEndpoint`, `substitutionReason`) to preserve replay explainability.
- The diagnostics are the foundation for future request-template reconciliation, not a new replay orchestrator.

### Cluster semantics

- Request-family clusters summarize grouped endpoints (memberEndpoints, dominantSignals).
- Each cluster exposes pricingRelevance, quantityRelevance, and infrastructureLikelihood to guide future replay-template synthesis.

### Why endpoint-centric replay is insufficient

- Pricing often requires a pipeline (schema -> quantity -> price), but replay picks a single endpoint.
- Infrastructure endpoints can outrank weak pricing signals when endpoint ranking is noisy.
- Planner and replay can drift because ranking does not encode pipeline semantics.

### Current replay limitations

- No family-aware clustering or sequencing for replay candidates.
- Replay selection is still endpoint-centric and does not follow request templates across families.
- Infrastructure endpoints are only penalized heuristically, not filtered by family gating.
- Endpoint relationships and cluster summaries are not used by replay yet.
- Replay reconciliation is still one planned endpoint to one best trace, not a general template graph.

### Future replay-template direction

- Use request-family clusters to assemble replay pipelines (schema -> quantity -> pricing).
- Promote shared request templates across related endpoints to reduce noisy ranking drift.
- Use normalized trace reconciliation as the basis for future replay-template synthesis.

### Current system flow

- Browser bootstrap (Playwright) captures traces, cookies, DOM signals
- Discovery -> Feasibility -> Planner -> Execution -> Validation -> Repair (bounded retries)
- HTTP replay is preferred when feasible; fallbacks are optional and gated by flags

### Configurator readiness + consent handling

- Readiness probe and dependency-probe selectors include modern React/ARIA primitives (`[role="radio"]`, `[role="checkbox"]`, `[role="tab"]`, `[aria-pressed]`, `[aria-selected]`, `[aria-checked]`, `[data-testid*="option"]`, `[data-cy*="option"]`) so SPAs whose option pickers don't use native `input[type=radio]` are still detected.
- Cookie consent has Usercentrics + Didomi vendor selectors plus a generic shadow-DOM walker that traverses nested shadowRoots, classifies buttons by text (accept vs deny), and avoids deny/manage variants. Records `matchedSelector=shadow_dom_walker` when it succeeds.
- Semantic-label probe (`trigger_configurator_via_semantic_labels`) is a fallback that fires only when readiness reports `< 2` interactive controls and `< 2` pricing API hits. It scans visible labels/headings/legends for printing-domain vocabulary (default: German — Menge/Auflage/Papier/Format/Farbigkeit/Seiten/Bindung/Veredelung/Ausrichtung) and clicks one option per matched group, locating controls by `aria-labelledby` reverse lookup, `for` attribute, then ancestor/sibling search. Mechanism is generic; vocabulary is parameterizable. Diagnostics are written to `consent_state.labelProbe` and include detected groups, clicks performed, and request / pricing-API deltas.

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

## Recent work log (2026-05-13)

### 1) Generalized value-map sideload (Saxoprint live, Print24 migrated)

Closed the highest-leverage gap from the start of this session: site-agnostic external value-map JSON, with Saxoprint as first consumer and Print24's hardcoded ClassVar dicts migrated to data. Mechanism is genuinely site-independent — `value_map_loader.py` carries zero per-site branches; sites differ only in their `value_maps/<site>.json` file.

**Schema v1** (one file per site at repo root `value_maps/<site>.json`):

- Top-level: `schemaVersion=1`, `site`, `generatedAt`, `captureMethod`, optional `notes`.
- `properties[]` is a LIST (not dict) so multiple entries can share one `canonicalKey` (booklet inner-vs-outer material is the obvious motivating case).
- Per property: `propertyId` (site-opaque: numeric for Saxoprint/Print24, form-field name like `input_var_PBRA444_1_1` for Onlineprinters if ever serialized), `canonicalKey` (one of `OPTION_KEY_ALIASES` keys OR a site-specific extension like `product_variant`, `perforation`, `co2_compensation`), `sourceLabel` (German diagnostic), `productScope` (optional, default `["*"]`, currently ADVISORY — surfaces in diagnostics but does not gate), optional `note`.
- Per option: `backendId`, `label`, `confidence in {confirmed, partial, ui_only}`. Default loader accepts `confirmed+partial`; `--allow-sideload-ui-only` opts into the rest.

**Lookup semantics** (`value_map_loader.resolve_value`):

1. Filter properties by `canonicalKey` (collected via `_canonical_option_key` from `agentic/onboarding.py` — same alias table as JSON-adapter inject pipeline).
2. For each, run `value_matches_label(requested, option.label)` over `options[]`. Same NFKD+lowercase+collapse-whitespace+exact-or-substring matcher Print24 uses (parity pinned by tests).
3. Outcome is one of: `resolved` (1 property × 1 option), `ambiguous` (1 property × multiple options), `key_collision` (multiple properties × at least one match each — e.g. booklet inner+outer material both match, user must disambiguate by passing a more-specific canonical key), `no_match` (property exists but no option matched), `no_property` (canonical key absent), `no_map` (file absent or load-error).

**Wiring**:

- `_prevalidate_requested_options(options, bootstrap_artifacts, allow_sideload_ui_only=False)` runs a `_augment_with_sideload` pass after the existing catalog loop (and also when catalog is unavailable). For matched rows: attaches `sideloadResolution` as a supplementary diagnostic the adapter can consume. For previously-unmatched rows: promotes to `matchType="sideload_only"` with a synthesized `catalogOption` carrying the backend hints, removes the unmatched entry, clears matching error messages, and re-flips `valid=True` if no errors remain. This is how Saxoprint (catalog often empty because the DOM walker doesn't see the Next.js configurator) starts producing usable prevalidation.
- `Print24SiteAdapter.build_http_replay_candidates` (and the `agents.py` duplicate) adds a sideload pass between catalog and hardcoded. Precedence is `catalog_applied > sideload > hardcoded`. Critical distinction: `catalog_skipped` does NOT block sideload — when the harvested catalog had the group but couldn't resolve the requested value (e.g. captured `130 g/m²`, user asked for `115 g/m²`), sideload IS allowed to fill, because that's the entire reason this layer exists. Hardcoded only fires when neither catalog nor sideload covered the key.
- `request_template_applied` gains `sideloadApplied: [{key, boxName, propId, matchedLabel, confidence, productScope, via}]`.
- CLI emits `[sideload-loaded] {site, path, schemaVersion, captureMethod, generatedAt, propertyCount, canonicalKeys, allowUiOnly, loadError}` (verbose only, with `ensure_ascii=False` so German labels render cleanly) and `[adapter-injection-applied-sideload]` whenever sideload contributed to a replay candidate.

**`value_maps/saxoprint.de.json`** — 15 properties, ~124 options, all `confirmed` except `Ausführung` (one `partial` Flyer entry; the UI-only `Falzflyer` variant dropped because its `options_ui_only` block carried UI-positional IDs not real backend IDs — captured in the property `note` instead). `quantity`/`format`/`material`/`color`/`pages` route through the canonical alias table; site-specific canonical keys introduced for `perforation`, `data_check`, `proof`, `sample_copy`, `fsc_label`, `co2_compensation`, `sponsoring`, `sender_address`, `delivery_split`, `product_variant`.

**`value_maps/print24.com.json`** — A5/A6 format pair + 250/10 quantity pair, marked `productScope=["brochures"]`. The in-code `_HARDCODED_FORMAT_MAP` / `_HARDCODED_QUANTITY_MAP` ClassVars are still kept as a final fallback this turn, so behavior is strictly additive — sideload takes over their job for any product where the harvested catalog can't, but if the JSON ever has a load-error the legacy path catches.

**Saxoprint coverage caveat**: this turn does NOT add a Saxoprint synthesis adapter. The `(propertyId, backendId)` pairs produced by prevalidation are correctly structured; whether the Saxoprint `get-product-prices` POST actually accepts these as `properties[].id` overrides the same way Print24 does is a separate downstream question that needs a captured-trace inspection. The diagnostics (`[sideload-loaded]`, `sideload_only` matched rows) tell us; wiring `SaxoprintSiteAdapter` is a follow-on turn.

**Schema lock-in protection**: `productScope` is in the schema but advisory-only this turn — it logs to `sideloadApplied[].productScope` so a user pointing a flyer mapping at a brochure URL sees it, but it doesn't gate resolution. When we eventually have a `product_family` signal (derived from URL path or bootstrap inference), it can become gating without a schema change.

**Open follow-ons surfaced by this work**:

- Delete `Print24SiteAdapter._HARDCODED_FORMAT_MAP` / `_HARDCODED_QUANTITY_MAP` after a cache-hit smoke verifies sideload parity for the captured brochure unit.
- Wire a `SaxoprintSiteAdapter.build_http_replay_candidates` that consumes `sideloadResolution` and rewrites Saxoprint's `properties[].id` (analogous to Print24). Needs trace inspection to confirm the payload shape.
- Eventually serialize the Onlineprinters variant-scrape into `value_maps/onlineprinters.de.json` for cross-session reuse (each option's `data-varindex` is essentially a backend ID).
- Promote `productScope` from advisory to gating once we have a `product_family` signal.

Tests: 30 new in `tests/test_value_map_loader.py` covering schema parsing, confidence filtering (default vs `--allow-sideload-ui-only`), NFKD parity with `Print24SiteAdapter._value_matches_captured_label`, all five resolve-outcome statuses, `productScope` passthrough, `note` propagation, end-to-end load of both checked-in JSONs, and Print24 adapter precedence integration (catalog wins over sideload, sideload wins over hardcoded, sideload overrides `catalog_skipped`). Suite: 190 tests, 189 pass, 1 pre-existing import-path failure in `test_browser_readiness.py` (missing `sys.path` insert — unrelated, file authored earlier).

---

## Recent work log (2026-05-12)

### -7) Print24 SiteAdapter consumes the harvested catalog (v1.5)

Closing the loop on the previous turn's catalog harvest: `Print24SiteAdapter` (and the duplicate `ExecutionAgent._build_print24_synthesized_candidate` in `agents.py`) now uses the harvested catalog as its primary source of prop_ids, with the existing hardcoded A5/A6 + 250/10 mappings retained as a fallback for runs without harvested catalog.

The critical new piece is `_value_matches_captured_label(requested, captured_label)`: prevalidation matches groups LOOSELY by alias scoring, so a user requesting `material="115 g/m² Bilderdruckpapier"` will produce a matched_row whose `catalogOption.visibleLabel` is "130 g/m² Recycling-Bilderdruckpapier" — same group, different option. Naively trusting the captured `prop_id` writes the wrong option to the server and returns the wrong price silently (the exact failure mode you flagged at the start of this conversation).

The strict matcher:

- NFKD-normalize both strings (so `g/m²` matches user-typed `g/m2` and `Stück` matches `stück`).
- Lowercase + collapse whitespace.
- Accept ONLY exact match or `norm_req in norm_cap` (request is a substring of the captured label).
- Tried and rejected: a numeric-overlap heuristic — too permissive. "148 x 210 mm DIN A5" and "105 x 148 mm DIN A6" share "148" but are obviously different formats.

Three precedence rules:

1. **Catalog matched** → use the catalog's prop_id (correct).
2. **Catalog skipped** (value-doesn't-match-captured) → DO NOT fall through to hardcoded; emit `skipped` entry with `reason: prop_id_unknown_for_value`, the captured value, captured prop_id, and a note pointing at `print24_feasibility_notes.md` §3.1/§9 (re-bootstrap with the desired value pre-selected, supply `--option-id` explicitly, or wait for the `repo` probe). Hardcoded mappings are product-specific and would silently substitute the wrong prop_id for a different brochure family.
3. **Catalog absent** for a key → try hardcoded fallback; if that's missing too → `unsupportedInputs`.

The synthesis adapter now emits a candidate even when `propertiesUpdated` is empty IF something was skipped or marked unsupported, so the user always sees the precise reason. If everything resolved cleanly AND no changes are needed (user requests captured config), return `[]` to let the captured trace replay as-is.

`request_template_applied` gains three new diagnostic lists: `catalogApplied`, `hardcodedApplied`, `skipped`. CLI `[adapter-injection-skipped]` and `[adapter-injection-unsupported]` lines already surface these.

Verified end-to-end against `unit_4f793d8.../bootstrap.json`:

| Request | Result |
|---|---|
| `quantity=10, format='DIN A6', material='130 g/m² Recycling-...'` (exact captured) | `[]` (replay as captured) |
| `quantity=10, format='DIN A6', material='130 g/m2 ...'` (user typed ASCII m2) | `[]` — NFKD lets ASCII match Unicode |
| `quantity=10, format='DIN A6', material='115 g/m² Bilderdruckpapier'` (paper change) | quantity+format applied; material SKIPPED with capturedValue=130 g/m² and the note |
| `quantity=250, format='148 x 210 mm DIN A5'` (both different) | Both SKIPPED with precise diagnostics (catalog wins over hardcoded) |
| `quantity=250, format='a5'` (no catalog for these values) | Hardcoded fallback applies for format (legacy `a5`→`268`); quantity SKIPPED |

Tests: 16 new in `tests/test_print24_site_adapter_catalog.py` covering the strict matcher (substring, NFKD, case-insensitive, rejection of different weights/formats/quantities, empty values), catalog-applies-prop-id, value-mismatch-skips, hardcoded-fallback when catalog missing, catalog-skipped-doesn't-fall-through-to-hardcoded, unsupportedInputs, no-candidate-when-captured-matches, and end-to-end against the real bootstrap. Total: 175 tests, all green.

**Implementation note:** `Print24SiteAdapter` is decorated `@dataclass(frozen=True)`, so the hardcoded-mapping class constants had to be typed `ClassVar[dict[str, str]]` to avoid being interpreted as fields (would have raised `mutable default ... not allowed`).

**Still open:** for `repo` probe to fully unlock non-captured values, the bootstrap needs to actively POST to `/api/de/itemmaster/calculation/repo` after acquiring the `user_identifier_token` cookie (per `print24_feasibility_notes.md` §9). That's a meaningful bootstrap-side change touching Playwright code — separate turn.

### -6) Print24 catalog harvesting from productDetails (v1)

Print24's bootstrap previously had `option_catalog: []` and `option_groups: 4` containing only display-preference toggles (`Nettopreise`, `Bruttopreise`, etc.) — useless for option matching. The DOM walker can't see the Next.js-rendered configurator widgets. But the captured `productDetails` AJAX response (per `print24_feasibility_notes.md` §3.3) carries `prop_details`: one entry per group with `box_name` (canonical key like `format`, `quantity`, `papierI`), `prop_id` (the integer option ID used in POST body `properties[].id`), `prop_translated` (human label), `box_name_title` (display group name).

New `src/price_extractor/print24_catalog.py`:

- `_pick_best_product_details_trace(traces)` — selects the most-recent POST to a `productDetails` URL with JSON content-type, status 200, and a body containing `prop_details`. Latest wins on ties (reflects final state).
- `harvest_print24_catalog_from_traces(traces)` — produces catalog rows in the standard shape: one row per `prop_details` entry, each with one option that has `selected=True`, `name=box_name`, `backendHints.dataPropertyId=prop_id`, `visibleLabel=prop_translated`, `groupLabel=box_name_title`.

`cli._catalog_groups_from_bootstrap` now ALWAYS calls the Print24 harvester whenever traces are available (in addition to whatever regrouped DOM-summary it has), prepending the harvested rows. Onlineprinters/Saxoprint bootstraps without productDetails traces get a no-op.

Two additional fixes to pre-validation pathways exposed by the Print24 data:

- **Quantity-matching backstop extended**: previously only accepted options with `name.startswith("input_var_")`; now also accepts `name == "quantity"` (Print24's canonical box_name). Without this, the harvested Print24 quantity row wouldn't be recognized as a quantity control.
- **New `catalog_only` match type for quantity**: Print24 has `quantity_signal.hasManualInput=False` and `presetValues=[]`, so the existing tile/manual paths can't validate quantity. The new path validates a quantity row when the catalog option carries `backendHints.dataPropertyId` (i.e., we have structured backend data). The matched row's `matchType` becomes `"catalog_only"` so downstream code can distinguish from `quantity_manual` / `quantity_preset`.

Verified end-to-end against `unit_4f793d8.../bootstrap.json`:

```
catalog row: Format       boxName=format       prop_id=222    label='105 x 148 mm DIN A6'
catalog row: Inhalt       boxName=papierI      prop_id=9413   label='130 g/m² Recycling-Bilderdruckpapier'
catalog row: Menge        boxName=quantity     prop_id=339    label='10 Stück'
... (10 rows total)

prevalidation for {quantity:10, format:'...DIN A6', material:'...Recycling-Bilderdruckpapier'}:
  valid=True, matched=3
    quantity   type=catalog_only       prop_id=339
    format     type=catalog_option     prop_id=222
    material   type=catalog_option     prop_id=9413
```

Tests: 8 new in `tests/test_print24_catalog.py` (per-entry harvesting, label fallbacks, malformed entry skipping, non-POST/non-JSON trace skipping, latest-trace preference, end-to-end prevalidation against the real bootstrap).

**Still open (v2):** the productDetails response only carries the currently-captured option per group. To support switching values (e.g., quantity 10 → 250), we need either (a) a bootstrap-side probe that triggers `repo` after acquiring the `user_identifier_token` (per `print24_feasibility_notes.md` §9), which returns all property alternatives with their pgrIds, OR (b) a way to surface "this option's prop_id is unknown, please supply --option-id" diagnostic for non-captured values. The current `Print24SiteAdapter` still uses hardcoded A5/A6/250/10 mappings; making it consume the harvested catalog (and emit the same `option_code_unknown_for_value`-style diagnostic when a requested value isn't in the catalog) is a natural follow-on.

### -5) Interpolation-mode quantity for Onlineprinters (per onlineprinters_request_modification.md §5B)

The Onlineprinters configurator has two quantity modes per `onlineprinters_request_modification.md` §5:
- **Tile mode**: a finite set of preset radio cards (10, 25, 50, …, 19000 on the user's brochure). Write the value directly into `input_var_<PROD>_<qty_group>_1`.
- **Interpolation mode**: triggered by the `Höhere Auflage angeben...` ("specify higher quantity") tile. Write the literal string `"Interpolation"` to the qty field AND write the typed quantity into `input_qty_1` (positioned immediately after the qty field in form-pair order). Per §3.3, writing only the visible number when the configurator expects Interpolation causes the backend to silently normalize to the closest preset.

Detection comes from the captured DOM, not the server. The MD spec's hint — "you can see the last Auflage value in the DOM; anything above is interpolation" — is grounded in the fact that every option tile, including the `Höhere Auflage` sentinel, ships as an `<input>` element in the configurator AJAX response with its own `data-varindex`. After the variant-scrape (turn -3) and the cap lift (turn -4), the user's bootstrap captures all 45 numeric tile presets PLUS the Interpolation tile with `data-varindex="PBRA444.135.0820000"`.

New `form_field_inference.quantity_tile_signals(catalog_groups, qty_field_name)` returns `{tilePresets, tileVarindexByValue, interpolationVarindex, supportsInterpolation, maxTilePreset}` for a given quantity form field. `cli._prevalidate_requested_options` calls it after picking the `matched_quantity_control` and stamps the result on the matched row as `quantityTileSignals`.

`OnlineprintersSiteAdapter` (both the standalone in `site_adapters.py` and the duplicate in `agents.py`) now branches on these signals when processing a quantity_manual row:

- If `requested_value` is in `tilePresets` → **tile mode**: write the value directly AND use the tile's own `data-varindex` from `tileVarindexByValue` to update the setlink (more precise than the previously-selected catalogOption's code).
- Else if `supportsInterpolation` AND `interpolationVarindex` is present → **interpolation mode**: new `_apply_interpolation_quantity` helper sets the qty field to `"Interpolation"` and finds the `input_qty_1` IMMEDIATELY AFTER the qty field in form-pair order, replacing in-place (preserves duplicate `input_qty_1` fields and their order per the MD spec — appending a new one breaks the request). The setlink option_code becomes the Interpolation tile's varindex.
- Else → existing direct-write fallback (best effort, may normalize).

`field_record` records `quantityMode: "tile" | "interpolation"`. When the typed quantity exceeds `maxTilePreset`, an `interpolationNote` is included (configurator-specific upper bound — backend will normalize if the value is too high).

**Backstop fix in `_prevalidate_requested_options`:** when the requested quantity isn't represented in any catalog group (off-preset typed values like 12345), `matched_quantity_control` would have stayed `None`, leaving `quantityTileSignals` empty and the synthesis adapter unable to switch modes. Added a second pass that picks the first catalog option whose `name` starts with `input_var_` AND whose `visibleValue` is numeric — structurally any option of the quantity tile field works since they all share the same form-field name.

Verified end-to-end against the user's `bootstrap.json` for 5 cases:

| Request | Result |
|---|---|
| `quantity=250` | `mode=tile`, `input_var_PBRA444_3_1=250`, setlink `<PBRA444.135.08250>` |
| `quantity=5000` | `mode=tile`, `input_var_PBRA444_3_1=5000`, setlink `<PBRA444.135.085000>` |
| `quantity=19000` (max tile) | `mode=tile`, `input_var_PBRA444_3_1=19000`, setlink `<PBRA444.135.0819000>` |
| `quantity=12345` (off-preset, ≤ max) | `mode=interpolation`, `input_var_PBRA444_3_1=Interpolation`, `input_qty_1=12345`, setlink `<PBRA444.135.0820000>` |
| `quantity=25000` (> max 19000) | `mode=interpolation` + note "requested qty 25000 > max tile preset 19000", `input_qty_1=25000` |

Tests: 9 new in `tests/test_onlineprinters_interpolation.py` covering tile-signal extraction (data-varindex per preset, Interpolation sentinel detection), form-pair manipulation (positional in-place modification, length preservation, insertion when no trailing input_qty_1 exists, no-op when qty field missing), and end-to-end mode-switching against the real bootstrap fixture for both tile and interpolation paths.

**Still open:** combined paper-change + interpolation-quantity. Each row is processed sequentially; the LATER row's setlink option_code wins. For paper-then-interpolation, the setlink ends up with the Interpolation tile's data-varindex which has the OLD paper baked in. Closing this needs template-substitution (replace the qty-suffix in the new-paper varindex with the interpolation slot suffix) — flagged for a follow-on if it actually breaks in practice; backend may normalize on its own.

### -4) Lift response_body_preview cap for same-site POSTs (so variant scraping sees the full configurator dump)

The 16000-char default cap in `browser_bootstrap.py:158` was truncating Onlineprinters' WEBSALE configurator AJAX responses, so the variant-scrape in `form_field_inference.parse_html_option_inputs` only saw the first ~3 paper option-tile `<input>` elements (90/100/115 g/m² in the user's brochure). Any non-current variant beyond the cut-off still fell into the `option_code_unknown_for_value` skip path from the previous turn.

Fix: bump `preview_limit` to 1 MB for traces that are (a) same-site (`infer_site_name(req.url) == result.site_name`) AND (b) POST. This catches:

- Onlineprinters' POSTs to the product page (the configurator AJAX that ships all option-tile HTML)
- Print24's `repo` / `price-matrix` (also same-site POSTs)
- Saxoprint's `get-product-prices` / `get-product-values`

All other traces (cross-origin tracking pixels, third-party scripts, etc.) stay at the 16 KB default, so bootstrap.json size doesn't explode. `get-product-values` keeps its existing 80 KB special case as a floor; `max(...)` picks the larger of the two limits if both rules apply.

Also added `response_body_len` and `response_body_preview_truncated` fields to each trace record so downstream code can tell whether a capture was cut off (e.g. a future site whose response exceeds 1 MB).

### -3) Onlineprinters paper-switching: variant scraping from response HTML (approach b)

End-to-end paper-switching now works for any variant whose `<input>` element ships in a captured configurator AJAX response. Onlineprinters' AJAX responses embed every option tile as `<input type="checkbox" name="input_var_<PROD>_<G>_<P>" value="<label>" data-varindex="<full option code>" data-prnumber="..." data-pimvarnodekey="..." data-productvariant="...">`. Parsing those gives the full (label → option-code) map for every variant that fit inside the captured preview window, including non-current selections.

Implementation extends `form_field_inference.py`:

- New `OptionVariantInfo` dataclass holds the (field_name, visible_value, data_varindex, data_prnumber, data_pimvarnodekey, data_productvariant) tuple per variant.
- `parse_html_option_inputs(text)` scans (possibly JSON-escaped) HTML for `<input ... name="input_var_..." ... data-varindex="...">` blocks and emits OptionVariantInfo records. Handles JSON unicode escapes (`<`, `>`, `&`, `&amp;`) and the WEBSALE response shape that wraps option HTML inside JSON string values.
- `collect_variants_from_traces(traces)` aggregates across all captured `response_body_preview` strings and dedupes.
- `build_form_field_map_from_traces(traces, product_url)` is the convenience entry point that combines (a) baseline-POST current-selection metadata with (b) response-HTML variant codes.
- `enrich_catalog_with_form_fields` now consumes both signals. For each cluster group: matched_field (from baseline POST) identifies the form field name + which option is currently-selected; variants_index (from response HTML) supplies the FULL option code for every other variant. If only variants are present (no baseline POST), the field name is recovered from the first variant alone.

`cli._catalog_groups_from_bootstrap` calls `build_form_field_map_from_traces` so this enrichment runs automatically whenever the bootstrap captured a configurator response containing option-tile inputs. Sites without `input_var_*` ship pattern are a no-op.

Bug fix exposed by the enrichment and corrected in `_prevalidate_requested_options` (cli.py): the quantity_manual fallback's heuristic of "first option with a name attribute" became wrong once enrichment populated paper options with the input_var name; would have written quantity 250 into the paper field. The filter now also requires the option's `visibleValue` to match `\d+(?:[.,]\d+)?`, so only the truly-numeric quantity field qualifies.

Verified end-to-end against the user's `bootstrap.json` for `--option quantity=250 --option material="115 g/m² Bilderdruckpapier"`:

- prevalidation matches: `quantity`, `material`
- synthesized body has `input_var_PBRA444_1_1 = "115 g/m² Bilderdruckpapier"`, `input_var_PBRA444_3_1 = "250"`
- synthesized setlink has `depvar_index_setparent=<PBRA444><PBRA444.115.081000>` (the new paper option code, scraped from the variant `<input>`)
- `skipped: []`, `unsupportedInputs: []`

Tests: 9 added in `tests/test_form_field_inference.py` (HTML option-input parsing including JSON-escaped HTML, attribute-order tolerance, dedup, variant aggregation across traces, enrichment of non-current options with variant codes, combined baseline+variants integration).

**Remaining caveat:** the captured `response_body_preview` is capped at 16000 chars in the recon snapshot. In the user's run we recover 3 paper variants (90, 100, 115 g/m²) but not all 8+ that the configurator actually offers. Variants beyond the preview cap would still hit `optionCodeUnknown=True` and be skipped with the precise diagnostic from the previous turn. Lifting the preview cap or capturing the full response body for configurator AJAX traces would close this. See `onlineprinters_request_modification.md` §5 for the additional quantity caveat (tile vs interpolation modes — the present implementation writes direct/tile mode; interpolation-mode quantities will need a separate path that pivots `input_var_<PROD>_3_1` to `"Interpolation"` and uses `input_qty_1`).

### -2) Form-field inference from captured POSTs (Onlineprinters paper-change groundwork)

Follow-on to the parent-group detection. Per `onlineprinters_request_modification.md` §3.3, writing only the visible label of an `input_var_*` field without also updating the SetLink causes the WEBSALE backend to silently normalize the option back to its current selection. So enriching the synthesized catalog with form-field `name` alone is not enough — we also need the depvar `(groupCode, optionCode)` pair for the requested value, or we have to refuse the write.

New module `src/price_extractor/form_field_inference.py`:

- `parse_form_field_map(post_body)` parses a captured baseline POST, extracts every `input_var_<PROD>_<group>_<position>` field (its current value), and decodes the triple-encoded `SetLink` to recover `(depvarGroupCode, depvarOptionCode)` per position from `depvar_index_setparent` (position 1) and `depvar_index_set_N` (position N+1).
- `pick_best_baseline_post(traces, product_url)` selects the captured POST most likely to be the configurator baseline (POST to product URL, contains `setlink=` AND `input_var_*`, prefers JSON response).
- `enrich_catalog_with_form_fields(catalog, form_map)` aligns each synthesized cluster group with a captured `input_var_*` field by matching one of the cluster's option labels (visibleValue, visibleLabel, or `sourceGroupLabel`) against the field's current value. Every option in the matched group gets the form `name`. The option whose label equals the field's current value also gets `selected=True` + `backendHints.dataVarindex` (the captured depvar option code) + `backendHints.depvarGroupCode` + `backendHints.depvarParam`. All OTHER options in the group get `optionCodeUnknown=True`. `sourceHints` records provenance (`formFieldEnrichedFromCapturedPost: True`).

Wired into `cli._catalog_groups_from_bootstrap` after the singleton-regroup step. Sites whose captured POSTs have no `input_var_*` fields (Print24, Saxoprint) are a no-op.

`OnlineprintersSiteAdapter.build_http_replay_candidates` (both copies — site_adapters.py and the duplicate in agents.py) gained an `option_code_unknown_for_value` skip path: when the matched option has `optionCodeUnknown=True` AND is not the currently-selected one AND `match_type != "quantity_manual"`, the adapter does NOT write the form field; it appends a `skipped` entry with the precise reason and a `note` explaining that probing alternatives or HTML scraping is required to learn the option code. Writing the label without the code is dangerous because the backend silently normalizes (per the MD doc).

Verified end-to-end against the user's actual `bootstrap.json`:

- `--option material="130 g/m² Bilderdruckpapier"` (the currently-selected paper): catalogOption now has `name="input_var_PBRA444_1_1"`, `selected=True`, `backendHints.dataVarindex="PBRA444.135.081000"`, `depvarGroupCode="PBRA444"`, `depvarParam="depvar_index_setparent"`. Synthesis would write both the form field and the setlink → real mutation succeeds.
- `--option material="115 g/m² Bilderdruckpapier"` (a different paper): catalogOption has `name="input_var_PBRA444_1_1"`, `selected=False`, `optionCodeUnknown=True`. Synthesis SKIPS this row, CLI emits `[adapter-injection-skipped]` with `reason: option_code_unknown_for_value` and the note above.

Tests: 10 new in `tests/test_form_field_inference.py` covering input_var parsing, SetLink depvar token alignment with position 1 vs N+1, baseline-POST scoring, full-metadata enrichment for current selection, `optionCodeUnknown` flagging for non-current options, group-passthrough when no option matches, and sourceHints provenance.

**Still open after this fix** — to actually mutate paper to a non-current variant on Onlineprinters, we need the depvar option code per paper variant. Two paths: (a) extend the bootstrap probe to click through paper options during recon and capture each resulting setlink → learn (label, code) tuples; (b) scrape variant URLs from the product page HTML (each paper card is likely an `<a>` with a `depvar_index_setparent=<new code>` href). Path (b) is more website-agnostic and probably faster to land. Either way, the diagnostic the user sees is now precise and actionable.

### -1) Onlineprinters parent-group detection (singleton-cluster post-processor)

Onlineprinters' paper/material UI renders each paper variant as its own DOM radio-card whose only visible label is the option text itself ("115 g/m² Bilderdruckpapier"). The in-browser extractor's `findGroupLabel` then resolves the group to the option's own text, producing one singleton group per paper variant (158 of them in the user's run). The `_catalog_groups_from_bootstrap` fallback at `cli.py:302` synthesized one `catalogOption` per singleton with empty `name`/`backendHints`, so pre-validation matched "the singleton group whose label contained the user's string" instead of "the right paper option inside a Material parent group".

Fix: new module `src/price_extractor/option_catalog_postprocess.py` exposing `regroup_singleton_clusters`. It scans summary `option_groups` for clusters of singleton entries whose labels match a printing-domain regex (paper, color, binding, finishing — extensible) and merges each cluster under a canonical synthetic parent (`material`, `material (umschlag)`, `material (innen)`, `color`, `binding`, `finishing`). Paper context (cover vs inner) is preserved on each option as `papierContext`. Single-member clusters pass through unchanged so a single stray paper-shaped label on a non-paper site doesn't get misgrouped. Wired into `_catalog_groups_from_bootstrap` for the summary-fallback path only — the JS-extracted catalog path is unaffected.

Verified end-to-end against the user's actual `bootstrap.json`: `--option material="115 g/m² Bilderdruckpapier"` now matches `groupLabel: "material (innen)"` with the 115 g/m² option inside the group. Tests: 8 new in `tests/test_option_catalog_postprocess.py` covering inner-paper merging, cover/inner split, color clusters, single-member passthrough, mixed catalogs, and an end-to-end `_prevalidate_requested_options` smoke against the real label shapes.

**Still open after this fix:** the synthesized catalog rows still have empty `name`/`backendHints`/`variantUrl` because the source summary `option_groups` never carried that data. The synthesis adapter therefore still can't mutate the form — but the previous turn's `[adapter-injection-skipped]` diagnostic now fires for the right reason (`catalog_row_missing_form_metadata`, `missing: ["control_name", "option_code", ...]`) instead of being masked by mis-matched groups. Next step for actually mutating paper: enrich the in-browser JS extractor to capture each option's form `name` and `href`/`variantUrl`, OR build a runtime learner that pulls (groupCode, optionCode) pairs from captured network-trace SetLink values. The MD spec at `onlineprinters_request_modification.md` is the contract for that work.

### 0) Multi-option configuration: coverage diagnostics + known catalog-extraction gap

Investigated user-reported bug: paper-type and other long-string options (e.g. `--option material="115 g/m² Bilderdruckpapier"`) do not flow into the request payload for Print24 or Onlineprinters; quantity does. Verified from artifacts in `.data/runs/onlineprinters.de/unit_c030aff4978830148365444fb9f4d15f4c3ca197/` and `.data/runs/print24.com/unit_4f793d86afafd1eb23c3ae14f85781fec53a8198/`.

Three distinct causes, addressed at two layers:

- **Cause 1 — user supplies an `--option` key the adapter has no rule for.** Was silent. Now surfaced:
  - `json_adapters.build_adapter_http_candidate` populates `request_template_applied.unsupportedInputs` for every `raw_requested_options` key without a matching inject rule.
  - `Print24SiteAdapter.build_http_replay_candidates` (site_adapters.py:1129) and the duplicate at `ExecutionAgent._build_print24_synthesized_candidate` (agents.py:1622) — both only handle `quantity` + `format` (hardcoded A5/A6/250/10) — now emit `unsupportedInputs` for everything else.
  - `OnlineprintersSiteAdapter.build_http_replay_candidates` and the duplicate at `ExecutionAgent._build_onlineprinters_synthesized_candidate` now emit both `unsupportedInputs` (keys not in any matched prevalidation row) and `skipped` (matched rows whose catalog metadata was too thin to mutate the form: missing `control_name`, `option_code`, or `variant_url`/setlink).
  - CLI emits `[adapter-injection-unsupported]` with an explanatory `note` whenever entries are present.

- **Cause 2 — JSON adapter rule has an empty/insufficient `value_map`.** Already surfaced in the previous turn as `[adapter-injection-skipped]` (`value_not_in_map`).

- **Cause 3 (KNOWN OPEN BLOCKER) — bootstrap catalog extraction does not capture the form-field linkage needed to mutate non-quantity options.**
  - **Onlineprinters:** the 158-entry `option_catalog` represents each paper variant as its OWN singleton group (e.g. `groupLabel: "115 g/m² BilderdruckpapierPEFC"`, `optionCount: 1`) instead of nesting paper variants under a parent "Material/Papier" group. Matched `catalogOption` rows have `name=""`, `backendHints={}`, `variantUrl=None`, `href=None` — so the adapter has nothing to write into the form. Quantity escapes only because of the `quantity_manual` fallback at site_adapters.py:1391 that scans `form_pairs` for a numeric `input_var_*` field.
  - **Print24:** `option_catalog` is empty entirely (4 groups, all with `groupLabel: None` and `option_count: 0`). The generated JSON adapter therefore has inject rules only for quantity/format (the upstream proposer can emit `material`/`color`/etc. paths after the vocabulary expansion in [[json-adapter-skipped-loud-failure-and-vocab]], but `_build_value_map_from_catalog` returns `{}` so the rules never apply with `require_mapped=True`).
  - **Fix path (not done):** the bootstrap-side option-catalog extractor needs to (a) detect parent "Material/Papier/Farbigkeit/..." grouping on Onlineprinters from DOM structure rather than per-radio labels, (b) capture each option's form `name` attribute and `variantUrl`/`href`, and (c) for Print24, identify the property option-group widget in the configurator DOM at all (currently captures zero options). Until this lands, paper/material via `--option` is silent-no-op on these two sites; the new `[adapter-injection-unsupported]` and `[adapter-injection-skipped]` diagnostics make this loud at runtime.

### 1) JSON-adapter inject pipeline: loud failure + vocabulary expansion

Two coordinated changes so the auto-generated JSON adapters (from `agentic/adapter_generation.py`) handle configurations beyond `quantity`/`format` and never silently swap in template defaults.

- **Loud failure on unmapped values.** `json_adapters.apply_injections` now accepts an optional `skipped` list and records every rule that bailed out (`value_not_in_map`, `cast_failed`, `path_unreachable`). `build_adapter_http_candidate` threads this through `request_template_applied.skipped`, and the CLI emits a dedicated `[adapter-injection-skipped]` line whenever entries are present. Closes the silent-substitution bug: previously, passing `--option quantity=500` to an adapter with `value_map={"250":"339"}` and `require_mapped=True` silently kept the template default (quantity 250) and returned a price for the wrong configuration.
- **Shared option-key alias table.** `agentic/onboarding.py` now exposes `OPTION_KEY_ALIASES` (canonical English keys → de/en alias tuples) for: quantity, format, material, pages, color, binding, finishing, orientation. Both `_infer_option_key_from_path` and the `properties.name` walker route through `_canonical_option_key`, so German property names like `papierU`, `farbenU`, `verarbeitung`, `finishingO`, `aspect_ratio`, `seitenU` resolve to canonical keys.
- **Walker dedupes by canonical key.** When multiple property entries share a canonical key (e.g. `papierU` + `papierI` both → `material`), the walker emits only the first. A follow-up to disambiguate cover/content for booklet products is open as a known limitation.
- **Generator vocabulary parity.** `_build_inject_rules` (`agentic/adapter_generation.py`) replaced its hard-coded `["quantity", "format"]` loop with the full `OPTION_KEY_ALIASES` set; `_build_value_map_from_catalog` now matches `groupLabel` against the alias tuple via `_group_label_matches_option`, so German option-catalog group labels (Farbigkeit, Veredelung, Bindung, Auflage) resolve to the canonical keys.
- Tests: `tests/test_json_adapters.py` (6 cases) for skipped-injection surfacing; new classes in `tests/test_onboarding.py` cover the alias table, the canonical resolver, the expanded properties walker, group-label matching, and `_build_inject_rules` emitting rules for color/binding/finishing when paths are present.

Known limitations (not addressed in this pass):

- One canonical key still maps to a single inject rule per adapter. Booklet products with separate inner/outer paper or color need a follow-up that distinguishes `material_outer` / `material_inner` (or similar).
- The generator still runs only via the staged-pipeline flag (`--staged-generate-runnable-adapters`). A top-level `--generate-adapter` switch on the normal pricing run remains open.

### 2) Semantic discovery stabilization (request families)

- Added request-family classification in DiscoveryAgent (pricing/schema/quantity/validation/infrastructure/unknown).
- Added lightweight clustering heuristics using shared semantic tokens (productdetails, repo, price-matrix, pricescale).
- Extended endpoint diagnostics with request_family, semantic_signals, penalties, payload_signals.
- Replay behavior is unchanged; this is a foundation refactor only.

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
- Request-family semantics and enriched endpoint diagnostics (foundation only)

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
- Use request-family rollups to de-noise endpoint ranking (no replay changes yet)
- Add family-aware planner hints without changing replay selection
- Evaluate infrastructure-family false positives across new sites

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
