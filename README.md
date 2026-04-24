# Safco Dental Agentic Product Scraper

A LangGraph-based multi-agent scraping system that extracts structured product catalogs from [Safco Dental Supply](https://www.safcodental.com) for two categories: **Dental Exam Gloves** and **Sutures & Surgical Products**.

---

## Architecture Overview

```
Main Graph  (AsyncSqliteSaver checkpointing)
  START → initialize_run
         ↓ Send() × 2 categories (parallel)
  crawl_category × 2
         ↓ (sequential reduce)
  reduce_products → export_results → END

Category Subgraph  (one per category)
  navigate_listing (Algolia API → all product URLs + pre-fetched data)
         ↓
  batch_fetch_pages (TavilyExtract → clean markdown for all URLs in batches)
         ↓ Send() × N product URLs (parallel fan-out)
  extract_product × N
         ↓
  reduce_category → END

Product Subgraph  (one per URL)
  fetch_page (Tavily pre-fetched content injected; individual Tavily fallback;
              httpx + Playwright as last resort)
         → classify_page (Algolia data → auto product_detail; else heuristic + LLM)
         → extract_structured (Algolia scalars + Tavily markdown → LLM supplementary)
         → [no Algolia + no content] → llm_fallback
         → validate → END
```

**Key discoveries**:
1. Safco uses the Algolia search API for its product catalog. The Navigator extracts a session API key from the page via Playwright, then queries Algolia directly — returning rich JSON (name, SKU, price, brand, images, availability, categories) without any CSS parsing.
2. Product detail pages are fetched in bulk via **TavilyExtract** before any product tasks are dispatched. Tavily returns clean **markdown** instead of raw HTML, which an LLM can extract description/specs/unit_pack_size/alternatives from in ~700 tokens vs ~1500 for noisy HTML.

---

## End-to-end execution (step by step)

### Step 0 — Startup (`main.py`)

- Reads `config.yaml` (categories, Algolia facet filters, Tavily batch size/depth, delays, concurrency, `llm.model`, output paths, checkpoint DB).
- Loads `.env` from the same directory.
- Builds **`MainState`**, opens **`AsyncSqliteSaver`** (LangGraph checkpoints), compiles **`main_graph`**, and streams node updates via **`graph.astream(..., stream_mode="updates")`**.
- On shutdown, closes the shared Playwright browser.

### Step 1 — `initialize_run` (main graph)

- Ensures **`output/`** exists.
- Opens **`output/safco_products.db`**, records run start, and loads **existing `url_hash` values** into the validator's in-memory dedup set (`seed_seen_hashes`).
- `--fresh` skips the dedup seed so all products are re-scraped regardless of prior runs.

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
- Results are stored in a `tavily_content_map` keyed by URL and passed directly to each product task — eliminating per-product httpx/Playwright calls entirely on the happy path.
- Failed URLs in a batch are recorded; those products fall back to individual Tavily or httpx/Playwright fetch in their product subgraph.

### Step 5 — `dispatch_product_tasks` (parallel fan-out)

- One **`Send("extract_product", ...)`** per URL, with **`algolia_data`** and **pre-fetched Tavily content** (`prefetched_content`, `prefetched_images`) injected directly into the `ProductTaskState`.
- All product tasks are scheduled at once; actual Playwright use is capped by a global `asyncio.Semaphore` (`max_concurrent_products`, default 3) only when the Playwright fallback path is triggered.

### Step 6 — `fetch_page` (product subgraph)

- If `raw_html` is already present (injected from `batch_fetch_pages`), this node is a **no-op**.
- Otherwise tries **individual TavilyExtract** for that URL.
- Last resort: **httpx** GET → checks for product markers → **Playwright** if the page is client-rendered.

### Step 7 — `classify_page`

- If `algolia_data` is present (normal path), classification is **skipped** — page is treated as `product_detail`.
- If not: URL-depth heuristic first (`/product/` or ≥5 path segments → `product_detail`); if still ambiguous, **`classify_with_llm`** (detects whether content is Tavily markdown or raw HTML and adjusts the prompt accordingly).

### Step 8 — `extract_structured`

- **Algolia** wins on all scalar fields it provides: name, brand, SKU, price, availability, images.
- **`_extract_supplementary_llm`** sends the Tavily markdown (trimmed to 3000 chars) to **OpenAI** with a focused prompt to extract: `description`, `unit_pack_size`, `specifications`, `alternative_products`. Uses ~700 input tokens vs ~1500 for noisy HTML in the old CSS approach.
- Image priority: Algolia > Tavily-extracted > LLM-extracted.
- Confidence: `min(1.0, llm_score + 0.35)` when Algolia provided a name (`algolia+tavily_llm` path); `llm_score` alone otherwise (`tavily_llm`).

### Step 9 — `llm_fallback` (conditional)

- Only triggers when **both** Algolia data and page content are absent — i.e., Tavily batch failed and individual Tavily also failed.
- Sends whatever content is available to the full `llm_extract` path (HTML or markdown, detected at runtime).

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

## Parallelism summary

| Level | What runs in parallel | Throttle |
|---|---|---|
| Categories | One `crawl_category` per category | 2 categories in default config |
| Batch fetch | All category URLs submitted to Tavily before fan-out | `tavily_batch_size` (default 5 per API call) |
| Products per category | Many `extract_product` invocations | Global semaphore only if Playwright fallback triggers |
| Across categories | Both categories' product work overlaps | Same global semaphore |

---

## Where the LLM is used

| Step | LLM? | When |
|---|---|---|
| `classify_page` | OpenAI (`llm_model`) | Only if no Algolia data and URL heuristic is ambiguous |
| `extract_structured` | OpenAI (`llm_model`) | **Every product** — focused prompt on Tavily markdown for description/specs/pack_size/alternatives |
| `llm_fallback` | OpenAI (`llm_model`) | Only if no Algolia data AND no page content at all |

The `extract_structured` LLM call is intentionally narrow: Algolia already supplies name/SKU/price/brand so the prompt only asks for the 4 supplementary fields. This keeps token cost low (~700 input tokens per product vs ~1500 in a full HTML fallback).

---

## Agent Responsibilities

| Agent | Location | Responsibility |
|---|---|---|
| **Navigator** | `agents/navigator.py` | Extracts Algolia session key via Playwright, queries Algolia API with `facetFilters` for each category across all pages |
| **Page Classifier** | `agents/classifier.py` | Determines page type using URL heuristics; LLM fallback for ambiguous pages (works on both markdown and HTML) |
| **Extractor** | `agents/extractor.py` | Merges Algolia scalars with LLM-extracted supplementary fields from Tavily markdown |
| **Validator** | `agents/validator.py` | Validates required fields, adjusts confidence score, deduplicates by URL hash |
| **Storage** | `storage/db.py` + `storage/exporter.py` | SQLite upsert (idempotent on `url_hash`) + CSV/JSON export |

---

## Why This Approach

1. **Algolia API for navigation**: Safco's category pages use Alpine.js to hydrate product grids from Algolia. Querying Algolia directly gives structured JSON for all products without CSS parsing fragility. One Playwright call per category to extract the session key; all pagination via pure HTTP.

2. **TavilyExtract for content**: Instead of httpx + CSS selectors, Tavily fetches all product detail pages in batches and returns clean markdown. This eliminates CSS selector maintenance, handles client-rendered pages automatically, and produces text that an LLM can parse with far fewer tokens than raw HTML.

3. **LLM on clean markdown, not noisy HTML**: The supplementary extraction prompt receives Tavily markdown (≤3000 chars) with Algolia context prepended. The LLM only needs to find 4 fields it can't see in Algolia — description, pack size, specifications, and related product links. Token cost per product: ~700 input tokens.

4. **Structured graph over ReAct**: The pipeline is deterministic — navigate → batch fetch → extract → validate → persist. `Send()` fan-out gives true parallelism across product URLs without LLM routing overhead.

---

## Setup & Execution

### Requirements

- Python 3.11+
- **OpenAI** API key (for `extract_structured` supplementary LLM call on every product, and optional classifier/fallback)
- **Tavily** API key (for `batch_fetch_pages` and individual `fetch_page` fallback)
- **LangSmith** API key (optional; for tracing)

### Install

```bash
cd safco_scraper
pip install openai "langgraph>=0.2.70" langgraph-checkpoint-sqlite \
  langchain-tavily playwright beautifulsoup4 lxml tenacity structlog \
  "pydantic>=2.7.0" pandas pyyaml rich click python-dotenv httpx
python -m playwright install chromium
```

### Configure

```bash
cp .env.example .env
# Edit .env and set:
#   OPENAI_API_KEY   — required
#   TAVILY_API_KEY   — required
#   LANGSMITH_API_KEY — optional
```

Optionally edit `config.yaml` to change `tavily_batch_size`, `tavily_extract_depth`, rate limits, product caps, or output directory.

### Run

```bash
# Full scrape (both categories)
python main.py

# Limit products for a quick test (~25s for 6 products per category)
python main.py --max-products 6

# Fresh run — ignore previously scraped products
python main.py --fresh

# Resume a crashed/interrupted run
python main.py --resume <run_id>

# Scrape only one category
python main.py --categories "Dental Exam Gloves"
```

Output is written to `./output/`:
- `safco_products.db` — SQLite database (queryable)
- `safco_products.csv` — flat CSV
- `safco_products.json` — nested JSON with all fields

### Query the database

```bash
sqlite3 output/safco_products.db "SELECT category, COUNT(*), AVG(confidence_score) FROM products GROUP BY category;"
sqlite3 output/safco_products.db "SELECT name, sku, price, brand FROM products WHERE category='Dental Exam Gloves' LIMIT 10;"
```

---

## Output Schema

| Field | Type | Source |
|---|---|---|
| `url_hash` | TEXT (PK) | SHA256(url) — idempotency key |
| `url` | TEXT | Algolia hit |
| `run_id` | TEXT | Config |
| `category` | TEXT | Config |
| `category_hierarchy` | JSON array | Algolia `categories.level1` |
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
| `confidence_score` | REAL | 0.0–1.0 field coverage |
| `scraped_at` | TEXT | UTC ISO timestamp |

---

## Limitations

1. **Algolia key expiry**: The session API key is valid for ~23 hours. Re-extracted fresh at the start of each run.

2. **Tavily token cost**: Every product incurs an LLM call in `extract_structured`. For full-catalog runs (800+ products), factor in ~700 × N input tokens. Reduce cost by caching Tavily content between runs or switching to a cheaper model for the supplementary extraction step.

3. **Playwright required for key extraction**: One Playwright browser launch per category (2 total) to intercept the Algolia key. Adds ~10s startup overhead per category.

4. **No login**: Operates as a guest user. Pricing may be incomplete for non-authenticated sessions.

5. **Specification coverage**: Safco pages often embed specs in description prose rather than tables. The LLM extracts structured specs when they exist; unstructured specs stay in the description field.

---

## Failure Handling

| Failure | Handling |
|---|---|
| Algolia key not intercepted | Falls back to Playwright HTML scraping for URL collection |
| Tavily batch URL failure | Recorded in `tavily_content_map` as `None`; individual Tavily retry in product subgraph |
| Individual Tavily failure | httpx + Playwright fallback |
| HTTP 429 / 5xx on product detail | `tenacity` retries with exponential backoff (4 attempts, 2–30s wait) |
| LLM API error in `extract_structured` | `llm_fields = {}`, product kept with Algolia-only data |
| Missing required fields | Product rejected by validator (`product_rejected` log) |
| Duplicate URL | Deduplication via SHA256 hash; `INSERT OR REPLACE` in SQLite |
| Crash mid-run | `AsyncSqliteSaver` checkpoints at node boundaries; `--resume run_id` continues |

---

## Scaling to Full-Site Production

1. **URL discovery**: Replace per-category Algolia queries with a full index sweep (`query=""`, no facet filter) to discover all categories and products at once.

2. **Tavily parallelism**: Increase `tavily_batch_size` and run batch requests concurrently across categories. Each batch is an independent API call.

3. **LLM cost reduction**: Cache Tavily markdown content between runs. For unchanged product pages, skip the supplementary LLM call and reuse the prior extraction.

4. **Playwright pool**: Replace the singleton browser with a remote Chrome fleet (Playwright `connect_over_cdp()`). The Algolia key extraction is the only mandatory Playwright step.

5. **Algolia key management**: Store the extracted key in Redis with a TTL matching its `validUntil`. Workers share the key pool rather than each spawning a Playwright session.

6. **Storage**: Replace SQLite with PostgreSQL. Use `langgraph-checkpoint-postgres` for checkpointing. The `upsert_batch` logic is already idempotent.

7. **Orchestration**: Schedule daily runs via Airflow or LangGraph Platform's cron trigger. Run category-level sub-DAGs in parallel.

---

## Data Quality Monitoring

1. **Confidence score distribution**: Alert if average `confidence_score` drops below 0.7 for a category (signals Algolia schema change or Tavily content degradation).

2. **Field completeness**: Track `COUNT(*) WHERE description IS NULL` per run. Spike = LLM extraction regression.

3. **Run-over-run delta**: Compare `nbHits` from Algolia with `COUNT(*)` in the DB. Gap > 5% = investigate Tavily failure rate.

4. **Tavily failure rate**: Log `failed` count from `batch_fetch_complete`. Rising rate = Tavily access issue or site blocking.

5. **Extraction method distribution**: Monitor `COUNT(*) GROUP BY extraction_method`. Shift from `algolia+tavily_llm` toward `llm_fallback` = Algolia or Tavily regression.

6. **Price sanity check**: Flag products with price outside [$0.01, $10,000] as anomalies.
