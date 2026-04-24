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
│  batch_fetch_pages                                                           │
│  (TavilyExtract → clean markdown for ALL URLs, batched before fan-out)      │
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
│  (use pre-fetched Tavily content if available;                               │
│   else individual TavilyExtract; else httpx → Playwright as last resort)    │
│       │                                                                      │
│  classify_page                                                               │
│  (skip if Algolia data present → auto product_detail;                       │
│   else URL-depth heuristic → LLM only if still ambiguous)                   │
│       │                                                                      │
│  extract_structured                                                          │
│  (Algolia scalars win: name, SKU, price, brand, images, stock;              │
│   LLM extracts from Tavily markdown: description, specs,                    │
│   unit_pack_size, alternative_products — ~700 input tokens)                 │
│       │                                                                      │
│       ├─[raw_html absent AND Algolia missing AND score < 0.65]──► llm_fallback
│       │                                                                      │
│  validate                                                                    │
│  (dedup by url_hash, reject incomplete records, score confidence)           │
│       │                                                                      │
│      END  (result returned to reduce_category in parent graph)              │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Two key design decisions drive this architecture:**

1. **Algolia API for product discovery**: Safco's category pages use Alpine.js to hydrate product grids from Algolia. The Navigator intercepts a session API key via Playwright, then queries Algolia directly over pure HTTP — returning rich structured JSON (name, SKU, price, brand, images, availability, categories) for all products across all pages without any CSS parsing.

2. **Tavily batch pre-fetch + LLM supplementary extraction**: All product detail pages are batch-fetched via TavilyExtract before any product task is dispatched. Tavily returns clean **markdown** instead of raw HTML — the LLM extracts description, specs, pack size, and alternative products in ~700 tokens vs ~1500 for noisy HTML. This also eliminates per-product httpx/Playwright calls on the happy path.

> **Note on `extractor.py`**: The module contains a `css_extract()` function with Hyvä/Alpine.js CSS selectors — this was the original supplementary extraction strategy for this branch (httpx + BeautifulSoup instead of Tavily). The live pipeline now uses `_extract_supplementary_llm()` in `product_graph.py` instead; `css_extract()` is retained as a utility but is not called in the main graph. The `llm_extract()` function in `extractor.py` is used by the last-resort `llm_fallback` node.

---

## End-to-end Execution (Step by Step)

### Step 0 — Startup (`main.py`)

- Reads `config.yaml` (categories, Algolia facet filters, Tavily batch size/depth, delays, concurrency, `llm.model`, output paths, checkpoint DB).
- Loads `.env` from the same directory.
- Builds **`MainState`**, opens **`AsyncSqliteSaver`** (LangGraph checkpoints), compiles **`main_graph`**, and streams node updates via **`graph.astream(..., stream_mode="updates")`**.
- On shutdown, closes the shared Playwright browser.

### Step 1 — `initialize_run` (main graph)

- Ensures **`output/`** exists.
- Opens **`output/safco_products.db`**, records run start, and loads **existing `url_hash` values** into the validator's in-memory dedup set. `--fresh` skips the dedup seed so all products are re-scraped.

### Step 2 — `dispatch_categories` (parallel)

- Returns one **`Send("crawl_category", CategoryState(...))`** per category.
- LangGraph runs both category subgraphs **concurrently**.

### Step 3 — `navigate_listing` (inside each category graph)

1. **Playwright** loads the category URL once and listens for network responses to capture the **Algolia `x-algolia-api-key`**.
2. That browser session closes; the rest of discovery is **plain HTTP** to Algolia (`facetFilters` from `config.yaml`), paginating until limits or catalog end.
3. Produces **`product_urls`** and **`algolia_hits`** (name, SKU, price, brand, stock, images, category breadcrumbs).
4. Algolia does **not** supply: description, specifications, unit pack size, related products — those come from Tavily + LLM in later steps.
5. If the Algolia key cannot be intercepted, the navigator falls back to Playwright HTML scraping.

### Step 4 — `batch_fetch_pages` (Tavily batch pre-fetch)

- Before any product task is dispatched, **all product URLs for the category** are submitted to **`TavilyExtract`** in batches (`tavily_batch_size`, default 5).
- Each batch call returns **clean markdown** + **image URLs** for each page.
- Results are stored in a `tavily_content_map` keyed by URL and passed directly to each product task — eliminating per-product httpx/Playwright calls on the happy path.
- Failed URLs in a batch are recorded as `None`; those products fall back to individual Tavily or httpx/Playwright in their product subgraph.

### Step 5 — `dispatch_product_tasks` (parallel fan-out)

- One **`Send("extract_product", ...)`** per URL, with **`algolia_data`** and **pre-fetched Tavily content** (`prefetched_content`, `prefetched_images`) injected into the `ProductTaskState`.
- All product tasks are scheduled at once; Playwright use is capped by a global `asyncio.Semaphore` (`max_concurrent_products`, default 3) only if the Playwright fallback path triggers.

### Step 6 — `fetch_page` (product subgraph)

- If `raw_html` is already present (injected from `batch_fetch_pages`), this node is a **no-op**.
- Otherwise tries **individual TavilyExtract** for that URL.
- Last resort: **httpx** GET → checks for product markers (`h1.page-title`, `[itemprop='name']`) → **Playwright** if the page is client-rendered.

### Step 7 — `classify_page`

- If `algolia_data` is present (normal path), classification is **skipped** — page is treated as `product_detail`.
- If not: URL-depth heuristic first (`/product/` or ≥5 path segments → `product_detail`); if still ambiguous, **`classify_with_llm`** (detects whether content is Tavily markdown or raw HTML and adjusts prompt accordingly).

### Step 8 — `extract_structured`

- **Algolia wins** on all scalar fields it provides: name, brand, SKU, price, availability, images.
- **`_extract_supplementary_llm`** sends the Tavily markdown (trimmed to 3000 chars) to **OpenAI** with a focused prompt to extract: `description`, `unit_pack_size`, `specifications`, `alternative_products`. Uses ~700 input tokens.
- Image priority: Algolia > Tavily-extracted > LLM-extracted.
- Confidence: `min(1.0, llm_score + 0.35)` when Algolia provided a name (`algolia+tavily_llm` path); `llm_score` alone otherwise (`tavily_llm`).

### Step 9 — `llm_fallback` (conditional)

- Only triggers when **all three conditions are met**: page content is absent (`raw_html is None`), Algolia data is also absent, AND confidence is below `extraction_fallback_threshold` (default 0.65).
- Sends whatever content is available to the full `llm_extract` path (from `extractor.py`), which handles both HTML and markdown inputs.
- With normal Algolia-backed runs, this path **never triggers**.

### Step 10 — `validate`

- Deduplicates by `url_hash` (in-memory + DB-seeded).
- Rejects records with missing/short name.
- Applies confidence penalties for missing SKU, price, brand, description, images.

### Step 11 — Category completion + SQLite flush

- When `crawl_category` finishes, all products for that category are **`upsert_batch`'d** to `safco_products.db` immediately (crash safety).

### Step 12 — `reduce_products`

- Merges both category branches into `MainState["products"]` and logs total.

### Step 13 — `export_results`

- Reads the DB with pandas; writes `output/safco_products.csv` and `output/safco_products.json`.

---

## Parallelism Summary

| Level | What runs in parallel | Throttle |
|---|---|---|
| Categories | One `crawl_category` per category | 2 concurrent (configurable) |
| Batch fetch | All category URLs submitted to Tavily before fan-out | `tavily_batch_size` per API call |
| Products | All `extract_product` tasks fan out simultaneously | Global semaphore only if Playwright fallback triggers |
| Across categories | Both categories' product work overlaps | Same global Playwright semaphore |

---

## Where the LLM Is Used

| Step | Model | When |
|---|---|---|
| `classify_page` | `llm.model` (config) | Only if no Algolia data and URL heuristic is ambiguous |
| `extract_structured` | `llm.model` (config) | **Every product** — focused prompt for description/specs/pack_size/alternatives from Tavily markdown |
| `llm_fallback` | `llm.model` (config) | Only if no Algolia data AND no page content AND confidence < 0.65 |

The LLM is never used for routing decisions or fields that Algolia already supplies. The `extract_structured` call is intentionally narrow — Algolia provides name/SKU/price/brand, so the prompt only asks for the 4 supplementary fields (~700 input tokens per product).

---

## Agent Responsibilities

| Agent | File | Responsibility |
|---|---|---|
| **Navigator** | `agents/navigator.py` | Launches Playwright once per category to intercept the Algolia session key, then queries Algolia via HTTP across all pages to build `product_urls` + `algolia_hits`. Falls back to Playwright HTML scraping if key interception fails. |
| **Page Classifier** | `agents/classifier.py` | Determines page type using URL-depth heuristics; calls the LLM only for ambiguous pages. Skipped entirely when Algolia data is present. |
| **Extractor** | `agents/extractor.py` | Provides `css_extract()` (CSS-based, not used in the main pipeline), `llm_extract()` (used by `llm_fallback` node), and `build_product_record()`. Supplementary extraction in the main path is handled by `_extract_supplementary_llm()` in `product_graph.py`. |
| **Validator** | `agents/validator.py` | Deduplicates by SHA256(url), rejects records with missing/short names, and applies confidence penalties for missing fields. |
| **Storage** | `storage/db.py` + `storage/exporter.py` | Idempotent SQLite upsert keyed on `url_hash` (`INSERT OR REPLACE`). Reads DB with pandas to export CSV and JSON at run end. |

---

## Why This Approach

| Decision | Alternative | Reason |
|---|---|---|
| Algolia API (via intercepted session key) | CSS scraping the product grid | Algolia gives structured JSON for every product — name, SKU, price, brand, images, stock — without fragile selectors. One Playwright call per category to capture the key; all pagination via plain HTTP. |
| TavilyExtract batch pre-fetch | httpx + CSS selectors per product | Tavily handles JS-rendered pages, returns clean markdown, and supports batch calls. Pre-fetching all URLs before the fan-out eliminates per-product browser launches on the happy path. CSS selectors (`css_extract()`) were the original design for this purpose but have been superseded. |
| LLM on clean Tavily markdown | Full HTML passed to LLM | The supplementary extraction prompt receives ≤3000 chars of clean markdown with Algolia context prepended. The LLM only needs to find 4 fields — ~700 input tokens vs ~1500 for noisy HTML. |
| Deterministic LangGraph graph | ReAct agent loop | The pipeline is fully deterministic: navigate → batch fetch → extract → validate → persist. `Send()` fan-out gives true parallelism without LLM routing overhead. |

---

## Setup & Execution

### Requirements

- Python 3.11+
- **OpenAI** API key — used by `extract_structured` (supplementary LLM call on every product) and optional classifier/fallback
- **Tavily** API key — used by `batch_fetch_pages` and individual `fetch_page` fallback
- **LangSmith** API key — optional; enables LangGraph tracing

### Install

```bash
cd safco_scraper
pip install openai "langgraph>=0.2.70" langgraph-checkpoint-sqlite \
  langchain-tavily playwright beautifulsoup4 lxml tenacity structlog \
  "pydantic>=2.7.0" pandas pyyaml rich click python-dotenv httpx
python -m playwright install chromium
```

### Secrets Management

API keys are loaded from a `.env` file via `python-dotenv`. **Never commit `.env` to source control** (it is in `.gitignore`).

```bash
cp .env.example .env
```

Edit `.env` and set:

```
OPENAI_API_KEY=sk-...        # required
TAVILY_API_KEY=tvly-...      # required
LANGSMITH_API_KEY=ls__...    # optional
```

In production, replace `.env` with a secrets manager (AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault). The application reads keys from environment variables, so any injection mechanism works without code changes.

### Config-Driven Execution

All runtime behaviour is controlled by `config.yaml`:

```yaml
scraper:
  max_pages_per_category: 20        # Algolia pagination limit
  max_products_per_category: 500    # URL collection cap
  delay_between_requests_ms: 1200   # Rate limiting
  max_concurrent_products: 3        # Playwright semaphore (fallback path only)
  tavily_batch_size: 5              # URLs per Tavily batch API call
  tavily_extract_depth: "advanced"  # "basic" | "advanced"

llm:
  model: "gpt-4o-mini"
  extraction_fallback_threshold: 0.65
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
| `name` | TEXT | Algolia `name` |
| `brand` | TEXT | Algolia `manufacturer_name` |
| `sku` | TEXT | Algolia `sku` |
| `price` | TEXT | Algolia `price.USD.default_formated` |
| `unit_pack_size` | TEXT | LLM from Tavily markdown |
| `availability` | TEXT | Algolia `stock_availability` |
| `description` | TEXT | LLM from Tavily markdown |
| `specifications` | JSON object | LLM from Tavily markdown |
| `image_urls` | JSON array | Algolia > Tavily images > LLM |
| `alternative_products` | JSON array | LLM from Tavily markdown |
| `extraction_method` | TEXT | `"algolia+tavily_llm"` / `"tavily_llm"` / `"llm_fallback"` |
| `confidence_score` | REAL | 0.0–1.0 field coverage metric |
| `scraped_at` | TEXT | UTC ISO timestamp |

---

## Limitations

1. **Algolia key expiry**: The session API key is valid for ~23 hours. It is re-extracted fresh at the start of each run via Playwright.

2. **LLM token cost at scale**: Every product incurs an LLM call in `extract_structured` (~700 input tokens). For a full-catalog run (800+ products), cache Tavily markdown between runs or switch to a cheaper model for the supplementary extraction step.

3. **Playwright required for key extraction**: One Playwright browser launch per category (2 total) to intercept the Algolia session key. Adds ~10s startup overhead per category; otherwise the scraper is pure HTTP.

4. **No login**: Operates as a guest user. Pricing may be incomplete for non-authenticated sessions.

5. **Specification coverage**: Safco pages often embed specs in description prose rather than structured tables. The LLM extracts structured specs when they exist; unstructured specs remain in the `description` field.

6. **Tavily rate limits**: The default `tavily_batch_size: 5` is conservative. Higher values speed up batch pre-fetch but may trigger rate limiting depending on the Tavily plan.

7. **`css_extract()` not active**: `extractor.py` retains a CSS-based extraction function (Hyvä/Alpine.js selectors for description, specs, images, related links). It is not called in the current pipeline. If Tavily access is unavailable, wiring `css_extract()` as the supplementary extraction path is a viable fallback.

---

## Failure Handling

| Failure | Handling |
|---|---|
| Algolia key not intercepted | Falls back to Playwright HTML scraping for URL collection |
| Tavily batch URL failure | Recorded as `None` in `tavily_content_map`; individual Tavily retry in product subgraph |
| Individual Tavily failure | httpx GET → Playwright render fallback |
| HTTP 429 / 5xx on product detail | `tenacity` exponential backoff (4 attempts, 2–30s wait) |
| LLM API error in `extract_structured` | `llm_fields = {}`; product kept with Algolia-only data |
| Missing required fields | Product rejected by validator (`product_rejected` log event) |
| Duplicate URL | SHA256 dedup in-memory + `INSERT OR REPLACE` in SQLite |
| Crash mid-run | `AsyncSqliteSaver` checkpoints at every node boundary; `--resume <run_id>` restarts from last completed node |

---

## Scaling to Full-Site Production

1. **URL discovery**: Replace per-category Algolia queries with a full index sweep (`query=""`, no facet filter) to discover all categories and products at once.

2. **Tavily parallelism**: Increase `tavily_batch_size` and submit batch requests concurrently across categories. Each batch is an independent API call.

3. **LLM cost reduction**: Cache Tavily markdown content keyed on `url_hash` between runs. For unchanged product pages, skip the supplementary LLM call and reuse the prior extraction.

4. **Playwright pool**: Replace the singleton browser with a remote headless Chrome fleet (`connect_over_cdp()`). The Algolia key extraction is the only mandatory Playwright step per category.

5. **Algolia key management**: Store the extracted key in Redis with a TTL matching its `validUntil`. All workers share the cached key rather than each spawning their own Playwright session.

6. **Storage**: Replace SQLite with PostgreSQL. Use `langgraph-checkpoint-postgres` for checkpointing. The `upsert_batch` logic is already idempotent.

7. **Orchestration**: Schedule daily runs via Airflow or LangGraph Platform's cron trigger. Run category-level sub-DAGs in parallel.

8. **Deployment path**:

   | Stage | Stack |
   |---|---|
   | POC (now) | Local Python process, SQLite, file output |
   | Staging | Dockerised container, PostgreSQL, LangSmith tracing |
   | Production | Cloud Run / ECS task, PostgreSQL RDS, Airflow scheduler, Redis key cache, S3 output |

---

## Data Quality Monitoring

1. **Confidence score distribution**: Alert if average `confidence_score` drops below 0.7 for a category — signals an Algolia schema change or Tavily content degradation.

2. **Field completeness**: Track `COUNT(*) WHERE description IS NULL` per run. A spike means LLM extraction is regressing.

3. **Run-over-run product count delta**: Compare `nbHits` from Algolia against `COUNT(*)` in the DB. A gap > 5% warrants investigation into Tavily failure rate.

4. **Tavily failure rate**: Log `failed` count from `batch_fetch_complete`. A rising rate indicates Tavily access issues or site-side blocking.

5. **Extraction method distribution**: Monitor `COUNT(*) GROUP BY extraction_method`. A shift from `algolia+tavily_llm` toward `llm_fallback` signals an Algolia or Tavily regression.

6. **Price sanity check**: Flag products with price outside `[$0.01, $10,000]` as anomalies for manual review.
