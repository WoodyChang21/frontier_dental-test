# Safco Dental Agentic Product Scraper

A LangGraph-based multi-agent scraping system that extracts structured product catalogs from [Safco Dental Supply](https://www.safcodental.com) for two categories: **Dental Exam Gloves** and **Sutures & Surgical Products**.

---

## Architecture Overview

The system is composed of three nested graphs. The Main Graph orchestrates two Category Subgraphs in parallel; each Category Subgraph fans out to N Product Subgraphs — one per URL — via its `extract_product` node.

```
┌─ MAIN GRAPH (AsyncSqliteSaver checkpointing) ──────────────────────────────┐
│                                                                              │
│  START → initialize_run                                                      │
│               │                                                              │
│               ├─ Send() ──► crawl_category [Dental Exam Gloves]  ──┐        │
│               │                                                     ├─► reduce_products → export_results → END
│               └─ Send() ──► crawl_category [Sutures & Surgical]  ──┘        │
│                             (both run in parallel)                           │
│                                                                              │
│  Each crawl_category node runs the Category Subgraph below                  │
└──────────────────────────────────────────────────────────────────────────────┘

┌─ CATEGORY SUBGRAPH (one instance per category) ────────────────────────────┐
│                                                                              │
│  navigate_listing                                                            │
│  (Playwright intercepts Algolia key → HTTP pagination → product URLs        │
│   + structured hit data: name, SKU, price, brand, images, stock)            │
│               │                                                              │
│               ├─ Send() ──► extract_product [URL 1]  ─┐                     │
│               ├─ Send() ──► extract_product [URL 2]   │                     │
│               ├─ Send() ──► extract_product [URL 3]   ├─► reduce_category → END
│               └─ Send() ──► extract_product [URL N] ──┘                     │
│                             (all run in parallel)                            │
│                                                                              │
│  Each extract_product node runs the Product Subgraph below                  │
└──────────────────────────────────────────────────────────────────────────────┘

┌─ PRODUCT SUBGRAPH (one instance per URL, runs inside extract_product) ─────┐
│                                                                              │
│  fetch_page                                                                  │
│  (httpx GET; if product markers absent → Playwright render fallback)        │
│       │                                                                      │
│  classify_page                                                               │
│  (skip if Algolia data present → auto product_detail;                       │
│   else URL-depth heuristic → LLM only if still ambiguous)                   │
│       │                                                                      │
│  extract_structured                                                          │
│  (Algolia scalars win: name, SKU, price, brand, images, stock;              │
│   CSS selectors extract: description, specs, images, alternative links)     │
│       │                                                                      │
│       ├─[no Algolia AND css_score < 0.65]──► llm_fallback                   │
│       │                                      (full HTML → LLM extraction)   │
│  validate                                                                    │
│  (dedup by url_hash, reject incomplete records, score confidence)           │
│       │                                                                      │
│      END  (result returned to reduce_category in parent graph)              │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Two key design decisions drive this architecture:**

1. **Algolia API for product discovery**: Safco's category pages use Alpine.js to hydrate product grids from Algolia. The Navigator intercepts a session API key via Playwright, then queries Algolia directly over pure HTTP — returning rich structured JSON (name, SKU, price, brand, images, availability, categories) for all products across all pages without any CSS parsing.

2. **Two-layer extraction — Algolia core + CSS supplementary**: Algolia supplies all structured scalar fields; the product detail page is fetched via httpx (Playwright fallback for client-rendered content) and parsed with CSS selectors to extract description, specifications, images, and related links. LLM is used only as a last resort when CSS coverage is too low and no Algolia data is available.

---

## End-to-end Execution (Step by Step)

### Step 0 — Startup (`main.py`)

- Reads `config.yaml` (categories, Algolia facet filters, delays, concurrency, `llm.model`, extraction threshold, output paths, checkpoint DB).
- Loads `.env` from the same directory.
- Builds **`MainState`**, opens **`AsyncSqliteSaver`** (LangGraph checkpoints), compiles **`main_graph`**, and streams node updates via **`graph.astream(..., stream_mode="updates")`**.
- On shutdown, closes the shared Playwright browser.

### Step 1 — `initialize_run` (main graph)

- Ensures **`output/`** exists.
- Opens **`output/safco_products.db`**, records run start, and loads **existing `url_hash` values** into the validator's in-memory dedup set. `--fresh` skips this so all products are re-scraped.

### Step 2 — `dispatch_categories` (parallel)

- Returns one **`Send("crawl_category", CategoryState(...))`** per category.
- LangGraph runs both category subgraphs **concurrently**.

### Step 3 — `navigate_listing` (inside each category graph)

1. **Playwright** loads the category URL once and listens for network responses to capture the **Algolia `x-algolia-api-key`**.
2. That browser session closes; the rest of discovery is **plain HTTP** to Algolia (`facetFilters` from `config.yaml`), paginating until limits or catalog end.
3. Produces **`product_urls`** and **`algolia_hits`** (name, SKU, price, brand, stock, images, category breadcrumbs).
4. Algolia does **not** supply: description, specifications, unit pack size, related products — those come from the CSS extraction of each product detail page.
5. If the Algolia key cannot be intercepted, the navigator falls back to Playwright HTML scraping for URL collection.

### Step 4 — `dispatch_product_tasks` (parallel fan-out)

- One **`Send("extract_product", ...)`** per URL, with **`algolia_data`** injected.
- All product tasks are scheduled at once; Playwright is capped by a global `asyncio.Semaphore` (`max_concurrent_products`, default 3).

### Step 5 — `fetch_page` (product subgraph)

- **httpx** GET with browser-like headers.
- Parses the response and checks for product markers (`h1.page-title`, `[itemprop='name']`). If absent, the page is client-rendered — falls back to **Playwright** to obtain the fully rendered DOM.
- Returns raw HTML for CSS parsing in the next step.

### Step 6 — `classify_page`

- If `algolia_data` is present (normal path), classification is **skipped** — page is treated as `product_detail`.
- If not: URL-depth heuristic first (`/product/` or ≥5 path segments → `product_detail`); if still ambiguous, **`classify_with_llm`** is called.

### Step 7 — `extract_structured`

- **Algolia wins** on all scalar fields it provides: name, brand, SKU, price, availability, images.
- **`css_extract()`** (from `agents/extractor.py`) parses the rendered HTML with Hyvä/Alpine.js-oriented selectors:
  - `description`: `div.product-description`, `h2` sibling pattern, `#description`
  - `specifications`: all `<table>` rows → `{key: value}` dict
  - `image_urls`: `img[src*='catalog/product']`, placeholders excluded
  - `alternative_products`: `a[href*='/product/']`, capped at 10
- **CSS confidence score**: `(required_found / 2) × 0.6 + (optional_found / 3) × 0.4` where required = name + price, optional = description + images + specifications.
- **Final score**: `min(1.0, css_score + 0.3)` when Algolia supplied a name (`algolia+css` path); `css_score` alone otherwise (`css`).

### Step 8 — `llm_fallback` (conditional)

- Only triggers when **both** conditions are met: no Algolia data for this URL, **and** CSS confidence is below `extraction_fallback_threshold` (default 0.65).
- Strips noisy tags, sends trimmed HTML (up to 6000 chars) to **OpenAI** (`llm_extract`, JSON mode).
- With normal Algolia-backed runs, this path **never triggers**.

### Step 9 — `validate`

- Deduplicates by `url_hash` (in-memory + DB-seeded).
- Rejects records with missing/short name.
- Applies confidence penalties for missing SKU, price, brand, description, images.

### Step 10 — Category completion + SQLite flush

- When `crawl_category` finishes, all products are **`upsert_batch`'d** to `safco_products.db` immediately.

### Step 11 — `reduce_products`

- Merges both category branches into `MainState["products"]` and logs total.

### Step 12 — `export_results`

- Reads the DB with pandas; writes `output/safco_products.csv` and `output/safco_products.json`.

---

## Parallelism Summary

| Level | What runs in parallel | Throttle |
|---|---|---|
| Categories | One `crawl_category` per category | 2 concurrent (configurable) |
| Products | All `extract_product` tasks fan out simultaneously | Global Playwright semaphore (`max_concurrent_products`, default 3) |
| Across categories | Both categories' product work overlaps | Same global semaphore |

---

## Where the LLM Is Used

| Step | Model | When |
|---|---|---|
| `classify_page` | `llm.model` (config) | Only if no Algolia data and URL heuristic is ambiguous |
| `llm_fallback` | `llm.model` (config) | Only if no Algolia data AND CSS confidence < 0.65 |

For a typical run where every URL has an Algolia hit, **LLM is never called**. It exists purely as a safety net for layout drift or Algolia misses.

---

## Agent Responsibilities

| Agent | File | Responsibility |
|---|---|---|
| **Navigator** | `agents/navigator.py` | Launches Playwright once per category to intercept the Algolia session key, then queries Algolia via HTTP across all pages to build `product_urls` + `algolia_hits`. Falls back to Playwright HTML scraping if key interception fails. |
| **Page Classifier** | `agents/classifier.py` | Determines page type using URL-depth heuristics; calls the LLM only for ambiguous pages. Skipped entirely when Algolia data is present. |
| **Extractor** | `agents/extractor.py` | `css_extract()` parses rendered HTML with Hyvä/Alpine.js CSS selectors for description, specs, images, and related links. `llm_extract()` is the full-page LLM fallback for the rare no-Algolia low-coverage case. `build_product_record()` assembles the final `ProductRecord`. |
| **Validator** | `agents/validator.py` | Deduplicates by SHA256(url), rejects records with missing/short names, and applies confidence penalties for missing fields. |
| **Storage** | `storage/db.py` + `storage/exporter.py` | Idempotent SQLite upsert keyed on `url_hash` (`INSERT OR REPLACE`). Reads DB with pandas to export CSV and JSON at run end. |

---

## Why This Approach

| Decision | Alternative | Reason |
|---|---|---|
| Algolia API (via intercepted session key) | CSS scraping the product grid | Algolia gives structured JSON for every product — name, SKU, price, brand, images, stock — without fragile selectors. One Playwright call per category to capture the key; all pagination via plain HTTP. |
| CSS selectors for supplementary fields | LLM extraction on every product | CSS parsing is deterministic, fast, and has zero API cost. Selectors are centralised in `PRODUCT_SELECTORS` and easy to update when the theme changes. LLM is reserved for genuine ambiguity. |
| httpx → Playwright (not Tavily) | Tavily batch pre-fetch | Direct HTTP keeps the tool dependency minimal. Playwright handles client-rendered pages when httpx returns incomplete HTML. No external extraction API needed for the detail page fetch. |
| Deterministic LangGraph graph | ReAct agent loop | The pipeline is fully deterministic: navigate → extract → validate → persist. `Send()` fan-out gives true parallelism without LLM routing overhead. |

---

## Sample Output Dataset

A live sample of 20 scraped products (10 per category) is included in the `output/` directory:

| File | Description |
|---|---|
| `output/safco_products.json` | Nested JSON — full field structure per product |
| `output/safco_products.csv` | Flat CSV — all fields, JSON columns serialised as strings |
| `output/safco_products.db` | SQLite database — queryable with standard SQL tools |

These were generated with `python main.py --max-products 10` against both categories:

| Category | Products | Avg Confidence | With Description |
|---|---|---|---|
| Dental Exam Gloves | 10 | 0.78 | 5 |
| Sutures & Surgical Products | 10 | 0.59 | 1 |

**Run timing** (run `db5c0691`, 2026-04-24):

| Phase | Duration |
|---|---|
| Algolia key extraction + URL collection (both categories, parallel) | ~53s |
| Product extraction — httpx/Playwright + CSS parsing (20 URLs, parallel) | ~69s |
| **Total** | **122.9s (~2 min)** |

The Algolia key extraction is a fixed one-time cost per category regardless of product count. At this rate, 100 products would take roughly ~12 minutes; the extraction phase scales with concurrency (`max_concurrent_products` in `config.yaml`).

To reproduce or extend the sample, see the [Run](#run) section below.

---

## Setup & Execution

### Requirements

- Python 3.11+
- **OpenAI** API key — used only by the LLM classifier and `llm_fallback` node (rare paths); model name set in `config.yaml` (default `gpt-4o-mini`)
- No Tavily key needed — detail pages are fetched directly via httpx/Playwright

### Install

```bash
# 1. Install uv (fast Python package manager)
pip install uv

# 2. Create and activate a virtual environment
uv venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# 3. Install all pinned dependencies
uv pip install -r requirements.txt

# 4. Install the Playwright browser
playwright install chromium
```

### Secrets Management

```bash
cp .env.example .env
# Edit .env and set:
#   OPENAI_API_KEY   — required for classifier/llm_fallback paths
#   LANGSMITH_API_KEY — optional; enables LangGraph tracing
```

In production, replace `.env` with a secrets manager (AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault). The application reads keys from environment variables, so any injection mechanism works without code changes.

### Config-Driven Execution

All runtime behaviour is controlled by `config.yaml`:

```yaml
scraper:
  max_pages_per_category: 20        # Algolia pagination limit
  max_products_per_category: 500    # URL collection cap
  delay_between_requests_ms: 1200   # Rate limiting between requests
  max_concurrent_products: 3        # Playwright semaphore

llm:
  model: "gpt-4o-mini"
  extraction_fallback_threshold: 0.65  # CSS score below this triggers llm_fallback
```

### Run

```bash
# Full scrape (both categories)
python main.py

# Quick test — limit products per category
python main.py --max-products 10

# Fresh run — ignore previously scraped products
python main.py --fresh

# Resume a crashed/interrupted run
python main.py --resume <run_id>

# Scrape a single category
python main.py --categories "Dental Exam Gloves"
```

Output is written to `./output/`:

| File | Description |
|---|---|
| `safco_products.db` | SQLite database (queryable) |
| `safco_products.csv` | Flat CSV export |
| `safco_products.json` | Nested JSON export |

### Query the database

```bash
sqlite3 output/safco_products.db \
  "SELECT category, COUNT(*), AVG(confidence_score) FROM products GROUP BY category;"

sqlite3 output/safco_products.db \
  "SELECT name, sku, price, brand FROM products WHERE category='Dental Exam Gloves' LIMIT 10;"
```

---

## Output Schema

| Field | Type | Source |
|---|---|---|
| `url_hash` | TEXT (PK) | SHA256(url) — idempotency key |
| `url` | TEXT | Algolia hit |
| `run_id` | TEXT | Config |
| `category` | TEXT | Config |
| `category_hierarchy` | JSON array | Algolia `categories.level1` split by `///` |
| `name` | TEXT | Algolia `name` / CSS `h1` fallback |
| `brand` | TEXT | Algolia `manufacturer_name` |
| `sku` | TEXT | Algolia `sku` |
| `price` | TEXT | Algolia `price.USD.default_formated` / CSS `.price-box` |
| `unit_pack_size` | TEXT | CSS description parsing |
| `availability` | TEXT | Algolia `stock_availability` |
| `description` | TEXT | CSS from product detail page |
| `specifications` | JSON object | CSS `<table>` parsing |
| `image_urls` | JSON array | Algolia > CSS `img[src*=catalog]` |
| `alternative_products` | JSON array | CSS `a[href*=/product/]` |
| `extraction_method` | TEXT | `"algolia+css"` / `"css"` / `"llm_fallback"` |
| `confidence_score` | REAL | 0.0–1.0 field coverage metric |
| `scraped_at` | TEXT | UTC ISO timestamp |

---

## Limitations

1. **Algolia key expiry**: The session API key is valid for ~23 hours. It is re-extracted fresh at the start of each run via Playwright.

2. **CSS selector maintenance**: Selectors target Safco's Hyvä/Alpine.js Magento theme. A theme update may break description or spec extraction. All selectors are centralised in `PRODUCT_SELECTORS` in `extractor.py` — a single file to update. Wire an integration test that asserts field coverage > 0.8 on a known product URL to catch regressions early.

3. **Playwright required for two reasons**: (a) one launch per category to intercept the Algolia key, (b) fallback for client-rendered product pages where httpx returns incomplete HTML.

4. **No login**: Operates as a guest user. Pricing may be incomplete for non-authenticated sessions.

5. **Specification coverage**: Safco pages often embed specs in description prose rather than structured tables. `css_extract()` captures table-based specs; prose-embedded specs stay in the `description` field.

---

## Failure Handling

| Failure | Handling |
|---|---|
| Algolia key not intercepted | Falls back to Playwright HTML scraping for URL collection |
| httpx fetch fails (429 / 5xx) | `tenacity` exponential backoff (4 attempts, 2–30s wait) |
| Playwright timeout on detail page | Error logged, product skipped; run continues |
| CSS extracts nothing useful | Confidence score stays low; if no Algolia data, routes to `llm_fallback` |
| LLM API error in `llm_fallback` | Error logged; product dropped |
| Missing required fields | Product rejected by validator (`product_rejected` log event) |
| Duplicate URL | SHA256 dedup in-memory + `INSERT OR REPLACE` in SQLite |
| Crash mid-run | `AsyncSqliteSaver` checkpoints at every node boundary; `--resume <run_id>` restarts from last completed node |

---

## Scaling to Full-Site Production

1. **URL discovery**: Replace per-category Algolia queries with a full index sweep (`query=""`, no facet filter) to discover all categories and products at once.

2. **Playwright pool**: Replace the singleton browser with a remote headless Chrome fleet (`connect_over_cdp()`). The Algolia key extraction and client-rendered page fallback are the only mandatory Playwright steps.

3. **Algolia key management**: Store the extracted key in Redis with a TTL matching its `validUntil`. All workers share the cached key rather than each spawning their own Playwright session.

4. **CSS selector versioning**: Track `extraction_method = "algolia+css"` ratio per run. A drop signals selector drift. Maintain a golden-set of test URLs with expected field assertions.

5. **Storage**: Replace SQLite with PostgreSQL. Use `langgraph-checkpoint-postgres` for checkpointing. The `upsert_batch` logic is already idempotent.

6. **Orchestration**: Schedule daily runs via Airflow or LangGraph Platform's cron trigger. Run category-level sub-DAGs in parallel.

7. **Deployment path**:

   | Stage | Stack |
   |---|---|
   | POC (now) | Local Python process, SQLite, file output |
   | Staging | Dockerised container, PostgreSQL, LangSmith tracing |
   | Production | Cloud Run / ECS task, PostgreSQL RDS, Airflow scheduler, Redis key cache, S3 output |

---

## Data Quality Monitoring

1. **Confidence score distribution**: Alert if average `confidence_score` drops below 0.7 for a category — signals a CSS selector regression or Algolia schema change.

2. **Field completeness**: Track `COUNT(*) WHERE description IS NULL` per run. A spike means CSS description selectors broke.

3. **Run-over-run product count delta**: Compare `nbHits` from Algolia against `COUNT(*)` in the DB. A gap > 5% warrants investigation.

4. **LLM fallback rate**: Monitor `COUNT(*) WHERE extraction_method='llm_fallback'`. A rising rate means CSS selectors are failing and the LLM safety net is catching more cases — time to fix the selectors.

5. **Extraction method distribution**: Monitor `COUNT(*) GROUP BY extraction_method`. The expected dominant value is `"algolia+css"`. Any shift toward `"css"` or `"llm_fallback"` signals Algolia or CSS regression respectively.

6. **Price sanity check**: Flag products with price outside `[$0.01, $10,000]` as anomalies for manual review.
