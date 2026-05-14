# Contributing to Webscraping Framework

Thank you for your interest in contributing! This document outlines how to set up your development environment, run tests, and extend the framework.

---

## Development Setup

### Prerequisites
- Python 3.11+
- conda (recommended for reproducibility)
- Playwright (for browser automation)

### Option A: Using conda (recommended)
```bash
git clone <your-repo>
cd webscraping-framework
conda env create -f environment.yml
conda activate price-extractor
```

Then install Playwright browsers:
```bash
playwright install chromium
```

### Option B: Using pip + venv
```bash
git clone <your-repo>
cd webscraping-framework
python -m venv .venv
.venv\Scripts\activate  # Windows
# or: source .venv/bin/activate  # Unix

pip install -r requirements.txt
playwright install chromium
```

### Option C: Using pip + editable install (development)
```bash
pip install -e ".[dev]"
playwright install chromium
```

### Option D: With agentic features
If you want to use LangGraph-based adapter generation:
```bash
pip install -e ".[agentic,dev]"
playwright install chromium
```

### Verify setup
```bash
python -m price_extractor.cli --help
python -m unittest discover -s tests -v
```

---

## Running Tests

### All tests
```bash
python -m unittest discover -s tests -v
```

### Specific test file
```bash
python -m unittest tests.test_onboarding -v
```

### With coverage
```bash
python -m pytest tests/ --cov=src/price_extractor
```

---

## Project Structure

```
src/price_extractor/
├── cli.py                      # CLI entry point
├── agents.py                   # Agent pipeline (discovery, execution, validation, repair)
├── browser_bootstrap.py        # Playwright-based browser interaction + trace capture
├── knowledge_store.py          # SQLite persistence layer
├── site_adapters.py            # Per-site HTTP replay synthesis (Print24, Onlineprinters)
├── json_adapters.py            # JSON adapter loading and injection
├── models.py                   # Data models
├── orchestrator.py             # Orchestration loop + retry logic
└── agentic/
    ├── onboarding.py           # LangGraph-based onboarding (optional)
    └── adapter_generation.py   # Auto-generate JSON adapters from catalog

tests/
├── test_*.py                   # Unit tests for each module
└── fixtures/                   # Test fixtures and offline data

.data/
├── knowledge.db                # SQLite: endpoints, normalizations, recon snapshots, templates
├── runs/                       # Per-run artifacts (summary, state, bootstrap, traces)
└── http-responses/             # Dumped HTTP response bodies for debugging

docs/
└── architecture.md             # High-level design document
```

---

## Adding Support for a New Site

### 1. Bootstrap and capture
```bash
python -m price_extractor.cli \
  --url "https://new-site.example/product/xyz" \
  --recon-only \
  --verbose
```

This generates artifacts in `.data/runs/new-site.example/...`

### 2. Review captured signals
- **`bootstrap.json`**: option groups, cookies, detected request families
- **`network_traces.json`**: captured requests/responses for HTTP replay
- **`option_catalog.json`**: structured option groups (if available)

### 3. Test HTTP replay
```bash
python -m price_extractor.cli \
  --url "https://new-site.example/product/xyz" \
  --request-only \
  --force-recon-refresh \
  --option quantity=250 \
  --verbose
```

### 4. Create a site adapter (optional)
If the default HTTP replay doesn't work, add a site-specific adapter in `site_adapters.py`:

```python
def _build_new_site_synthesized_candidate(
    self,
    state: RunState,
    effective_options: dict[str, Any],
) -> dict[str, Any] | None:
    site_name = str(state.target.site_name or "").lower()
    if "new-site" not in site_name:
        return None
    
    # Custom logic to synthesize payload from effective_options
    # ...
    
    return {
        "url": endpoint,
        "method": "POST",
        "source": "new_site_synthesized",
        "body_text": json.dumps(custom_payload),
        "trace_override": template_trace,
        "request_template_applied": {...},
    }
```

Then call it from `_try_http_replay()`.

### 5. Add offline test fixture
Create a minimal test fixture in `tests/fixtures/` with:
- `network_traces.json` (captured requests)
- `bootstrap.json` (captured bootstrap signals)
- `cookies.json` (session cookies)

Example test:
```python
def test_new_site_http_replay(self):
    # Load fixture
    with open('tests/fixtures/new_site_traces.json') as f:
        traces = json.load(f)
    
    # Run execution
    result = ExecutionAgent().run(state)
    
    # Verify
    self.assertTrue(result.success)
    self.assertGreater(result.price_value, 0)
```

---

## Key Patterns

### Option Normalization
When the server accepts a different value than requested, record it:
```python
normalization_signals.append(f"config_normalized key={key} requested={requested} accepted={accepted}")
```

### Request Synthesis
For sites with complex request formats (Onlineprinters form payloads, Print24 property IDs):
- Capture the template trace during bootstrap
- Extract the template payload
- Modify only specific fields (e.g., property IDs, form fields)
- Preserve order and duplicates (use list of tuples, not dicts)

### Validation
Always verify that the extracted price corresponds to the requested configuration:
```python
if extracted_price is None:
    # Fall back to captured traces or heuristic
    ...
else:
    # Validate requested options vs server response
    accept_config, signals = self._extract_server_accepted_configuration(payload, effective_options)
```

---

## Code Style

- Follow PEP 8
- Use type hints
- Docstrings for public methods
- Keep agent methods < 200 lines (consider breaking into helpers)

---

## Debugging Tips

### Enable verbose logging
```bash
python -m price_extractor.cli --url "..." --verbose
```

### Inspect artifacts
```powershell
# View last run state
$latest = Get-ChildItem .data/runs -Recurse -Filter "state_snapshot.json" | Sort-Object LastWriteTime -Desc | Select-Object -First 1
Get-Content $latest.FullName | ConvertFrom-Json | ConvertTo-Json -Depth 50 | more
```

### Check HTTP responses
```bash
ls .data/http-responses/
# Open the dumped response JSON in your editor
```

### Trace matching diagnostics
Look for `matchStrategy`, `matchConfidence`, `normalizedEndpoint` in the run summary to understand how replay endpoints are being matched.

---

## Known Limitations

- Replay is endpoint-centric, not pipeline-aware (schema → quantity → price)
- Infrastructure endpoints can false-positive rank higher than weak pricing signals
- Some sites require manual request-template design
- Configurators with highly dynamic UX may not be captured well

See TASKS.md for open items.

---

## Questions?

Refer to:
- **Architecture**: `docs/architecture.md`
- **Examples**: `README.md`
- **Task tracking**: `TASKS.md`
- **Context**: `CONTEXT.md`

