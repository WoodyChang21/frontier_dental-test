# Safco Dental Agentic Product Scraper

A LangGraph-based multi-agent scraping system that extracts structured product catalogs from [Safco Dental Supply](https://www.safcodental.com) for two categories: **Dental Exam Gloves** (101 products) and **Sutures & Surgical Products** (56 products).

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
         ↓ Send() × N product URLs (parallel fan-out)
  extract_product × N
         ↓
  reduce_category → END

Product Subgraph  (one per URL)
  fetch_page (httpx → Playwright fallback)
         → classify_page (rule-based; LLM only if ambiguous)
         → extract_structured (Algolia data + CSS selectors merged)
         → [confidence < 0.65] → llm_fallback
         → validate → END
```

**Key discovery**: Safco uses the Algolia search API for its product catalog. The Navigator extracts a session API key from the page, then queries Algolia directly — returning rich JSON (name, SKU, price, brand, images, availability, categories) for all products without CSS parsing. The product detail page is then fetched via httpx to supplement with description, specifications, and related products.

---

## End-to-end execution (step by step)

This section walks through one full run of `python main.py` and how parallelism, throttling, and LLM calls fit together.

### Step 0 — Startup (`main.py`)

- Reads `config.yaml` (categories, Algolia facet filters, delays, concurrency, `llm.model`, extraction threshold, output paths, checkpoint DB).
- Loads `.env` from the same directory as `main.py`.
- Builds **`MainState`**, opens **`AsyncSqliteSaver`** (LangGraph checkpoints — used with `--resume <run_id>` via the same `thread_id` as `run_id`), compiles **`main_graph`**, and streams node updates with **`graph.astream(..., stream_mode="updates")`**.
- On shutdown, closes the shared Playwright browser.

### Step 1 — `initialize_run` (main graph)

- Ensures **`output/`** exists.
- Opens **`output/safco_products.db`**, records run start, and loads **existing `url_hash` values** into the validator’s in-memory set (`seed_seen_hashes`). That **dedupes** products already stored from earlier runs on the same machine/DB.
- Note: **`--resume`** is separate — it uses **LangGraph’s checkpoint SQLite** so the orchestration can continue after an interrupt; dedupe is about **not re-counting the same product URL** in the validator / upsert logic.

### Step 2 — `dispatch_categories` (parallel)

- Returns one **`Send("crawl_category", CategoryState(...))`** per category.
- LangGraph runs **both category subgraphs concurrently** (e.g. Dental Exam Gloves and Sutures & Surgical Products).

### Step 3 — `navigate_listing` (inside each category graph)

1. **Playwright** loads the category URL once and **listens for network responses** to capture the **Algolia `x-algolia-api-key`** the storefront uses.
2. That browser session closes; the rest of discovery is **plain HTTP** to Algolia (`facetFilters` from `config.yaml`), paginating until limits or end of catalog.
3. Produces **`product_urls`** plus **`algolia_hits`** (structured fields: name, SKU, price, brand, stock, images, category breadcrumbs).
4. **Algolia does not** supply: long **description**, rich **specifications**, **related product URLs** — those come from each **product detail page** in later steps.
5. If the key cannot be captured, the navigator **falls back** to Playwright HTML scraping (links / embedded JSON / “next” pagination).

### Step 4 — `dispatch_product_tasks` (parallel fan-out)

- One **`Send("extract_product", ...)`** per product URL, passing **normalized `algolia_data`** when available.
- All product tasks are **scheduled** at once; **actual** Playwright use is capped by a **global `asyncio.Semaphore`** in `tools.py` (`max_concurrent_products`, default 3). Categories share the same pool, so you never run more than that many browser contexts concurrently.

### Step 5 — `fetch_page` (product subgraph)

- **First**: **`httpx`** GET with browser-like headers.
- **Then**: parse HTML and look for product markers (e.g. `h1.page-title`, `[itemprop='name']`). Safco’s **Hyvä / client-rendered** theme often means the initial HTML is incomplete, so the code **falls back to Playwright** to obtain fully rendered DOM (larger HTML, slower but reliable).

### Step 6 — `classify_page`

- If **`algolia_data`** is present (normal path), classification is **skipped** and the page is treated as **`product_detail`**.
- If not: **CSS / URL heuristics** in `classifier.py`; if still ambiguous, **`classify_with_llm`** (OpenAI, model from `run_config["llm_model"]` / `config.yaml`).

### Step 7 — `extract_structured`

- **Merges** Algolia scalars with **CSS** fields from `extractor.py` (description via Hyvä-oriented selectors, **tables → specifications**, **`img[src*='catalog/product']`**, **`a[href*='/product/']`** for related links, capped). Algolia wins on overlapping scalars.
- **CSS confidence** (for routing only) is computed in `css_extract`: required CSS signals **name + price** (weight **0.6**), optional **description / images / specifications** (weight **0.4**). After merge, **`final_score = min(1.0, css_score + 0.3)`** when Algolia supplied a name (`algolia+css` path).

### Step 8 — `llm_fallback` (conditional)

- Runs only if **there is no Algolia payload** for this URL **and** merged confidence is **below** `extraction_fallback_threshold` in `config.yaml` (default **0.65**).
- Strips noisy tags, sends **trimmed text** (up to **6000** chars) to **OpenAI** (`llm_extract`, JSON mode), model from config (default **`gpt-4o-mini`**).
- With Algolia-backed runs, this path **rarely triggers** because `should_use_llm_fallback` skips LLM when `algolia_data` exists.

### Step 9 — `validate`

- **Duplicate** URLs by `url_hash` (in-memory + DB-backed seeding).
- Rejects records with missing/short **name**.
- Applies **penalties** for missing SKU, price, brand, description, images; updates **`confidence_score`**.

### Step 10 — Category completion + SQLite flush

- When a **`crawl_category`** node finishes, **all products for that category** are **`upsert_batch`**’d to **`safco_products.db`** immediately (crash safety before the next category or export).

### Step 11 — `reduce_products`

- Parallel category branches have merged into **`MainState["products"]`**; this step mainly logs / attaches **total** metadata.

### Step 12 — `export_results`

- Reads the DB with pandas, writes **`output/safco_products.csv`** and **`output/safco_products.json`** (nested JSON for list/dict columns).

### Parallelism summary

| Level | What runs in parallel | Throttle |
|--------|------------------------|----------|
| Categories | One `crawl_category` per category | Only 2 categories in the default config |
| Products per category | Many `extract_product` invocations | Global semaphore: max concurrent Playwright sessions |
| Across categories | Both categories’ product work overlaps | Same global semaphore |

### Where the LLM is used

| Step | LLM? | When |
|------|------|------|
| `classify_page` | OpenAI (`llm_model` from `config.yaml`) | Only if **no** Algolia data **and** heuristics are ambiguous |
| `llm_fallback` | OpenAI (`llm_model`) | Only if **no** Algolia data **and** score is below `extraction_fallback_threshold` |
| Everything else | No | Algolia HTTP + httpx/Playwright + BeautifulSoup |

For a typical Safco run **with** Algolia hits for every URL, **LLM calls are effectively unused**; they matter for HTML-only fallback or layout drift.

---

## Agent Responsibilities

| Agent | Location | Responsibility |
|---|---|---|
| **Navigator** | `agents/navigator.py` | Extracts Algolia session key via Playwright, queries Algolia API with correct `facetFilters` for each category across all pages |
| **Page Classifier** | `agents/classifier.py` | Determines page type (product_detail / listing / irrelevant) using DOM signals; LLM fallback for ambiguous pages (~5% of cases) |
| **Extractor** | `agents/extractor.py` | Merges Algolia pre-fetched data with CSS-extracted detail-page fields (description, specs, images). LLM fallback when CSS coverage < 0.65 |
| **Validator** | `agents/validator.py` | Validates required fields, adjusts confidence score, deduplicates by URL hash |
| **Storage** | `storage/db.py` + `storage/exporter.py` | SQLite upsert (idempotent on `url_hash`) + CSV/JSON export |

---

## Why This Approach

1. **Algolia API instead of HTML scraping for navigation**: Safco's category listing pages use Alpine.js to hydrate product grids from Algolia. Querying Algolia directly gives us structured JSON for all 157 products across both categories without CSS parsing fragility. One Playwright call per category to extract the session key; all pagination via pure HTTP.

2. **Structured graph over ReAct agent**: The pipeline is deterministic — navigate → extract → validate → persist. Using a `Send()` fan-out pattern gives true parallelism across product URLs. A ReAct agent would serialize all product extractions through a single LLM loop, costing ~500 LLM calls for routing decisions that don't need AI reasoning.

3. **LLM used selectively**: Only two scenarios trigger an LLM call — (a) ambiguous page classification (~5% of pages), (b) extraction fallback when CSS coverage score < 0.65. Everything else is deterministic code.

4. **Two-layer extraction**: Algolia supplies the core fields (name, SKU, price, brand, availability, images). The product detail page (httpx, SSR) supplies description and specifications. Merging them gives higher coverage than either source alone.

---

## Setup & Execution

### Requirements
- Python 3.11+
- An **OpenAI** API key (used only for optional **classification** and **LLM extraction fallback**; model name comes from `config.yaml`, default `gpt-4o-mini`)

### Install

```bash
cd safco_scraper
python -m ensurepip
python -m pip install openai "langgraph>=0.2.70" langgraph-checkpoint-sqlite \
  playwright beautifulsoup4 lxml tenacity structlog "pydantic>=2.7.0" \
  pandas pyyaml rich click python-dotenv httpx
python -m playwright install chromium
```

### Configure

```bash
cp .env.example .env
# Edit .env and set OPENAI_API_KEY (required if LLM fallback/classifier paths run)
```

Optionally edit `config.yaml` to change rate limits, product caps, or output directory.

### Run

```bash
# Full scrape (both categories)
python main.py

# Limit products for a quick test
python main.py --max-products 10

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
| `name` | TEXT | Algolia `name` / CSS `h1` |
| `brand` | TEXT | Algolia `manufacturer_name` |
| `sku` | TEXT | Algolia `sku` |
| `price` | TEXT | Algolia `price.USD.default_formated` |
| `unit_pack_size` | TEXT | CSS / description parsing |
| `availability` | TEXT | Algolia `stock_availability` |
| `description` | TEXT | CSS from product detail page |
| `specifications` | JSON object | CSS table parsing |
| `image_urls` | JSON array | Algolia + CSS `img[src*=catalog]` |
| `alternative_products` | JSON array | CSS related links |
| `extraction_method` | TEXT | `"algolia+css"` / `"css"` / `"llm_fallback"` |
| `confidence_score` | REAL | 0.0–1.0 field coverage |
| `scraped_at` | TEXT | UTC ISO timestamp |

---

## Limitations

1. **Algolia key expiry**: The session API key extracted from the page is valid for ~23 hours. Long-running jobs may need to refresh it. Production fix: re-extract the key at the start of each run (already implemented) or integrate with Algolia's public search-only key if Safco exposes a permanent one.

2. **Playwright required for key extraction**: One Playwright browser launch per category (2 total) is needed to intercept the Algolia key. This adds ~10s startup overhead per category.

3. **No login**: The scraper operates as a guest user. If Safco gates product data behind login, some fields (especially pricing) may be unavailable.

4. **Specifications**: Many Safco product pages don't use structured spec tables — specs are embedded in the description text. The current extractor captures table-based specs; future work could use an LLM to parse description-embedded specs.

5. **Rate limits**: Defaults to 1.2s delay per product detail request. For full production runs, reduce `delay_between_requests_ms` with care to avoid 429s.

---

## Failure Handling

| Failure | Handling |
|---|---|
| Algolia key not intercepted | Falls back to Playwright HTML scraping for URL collection |
| HTTP 429 / 5xx on product detail | `tenacity` retries with exponential backoff (4 attempts, 2–30s wait) |
| Playwright timeout | Error logged, product skipped; run continues |
| LLM API error in fallback | Error logged, product kept with CSS-only data |
| Missing required fields | Product rejected by validator (logged as `product_rejected`) |
| Duplicate URL | Deduplication via SHA256 hash; `INSERT OR REPLACE` in SQLite |
| Crash mid-run | `AsyncSqliteSaver` checkpoints at node boundaries; `--resume run_id` continues the same LangGraph thread |

---

## Scaling to Full-Site Production

1. **URL discovery**: Replace per-category Algolia queries with a full index sweep (`query=""`, no facet filter) to discover all categories and products at once.

2. **Parallelism**: Increase `max_concurrent_products` (currently 3). Each product subgraph is independent — the `Send()` fan-out scales horizontally. In production, deploy with LangGraph Platform and set `max_concurrent_tasks` at the worker level.

3. **Playwright pool**: Replace the singleton browser with a browser pool (e.g., Playwright's `BrowserType.connect_over_cdp()` pointing to a remote Chrome fleet). This decouples scraping workers from browser processes.

4. **Algolia key management**: Store the extracted key in Redis with a TTL matching its `validUntil`. Workers share the key pool rather than each spawning a Playwright session.

5. **Storage**: Replace SQLite with PostgreSQL. Use `langgraph-checkpoint-postgres` for checkpointing. The `upsert_batch` logic is already idempotent.

6. **Orchestration**: Schedule daily runs via Airflow or LangGraph Platform's cron trigger. Run category-level sub-DAGs in parallel (2 today, scalable to N categories).

7. **Selector maintenance**: All CSS selectors are centralized in `PRODUCT_SELECTORS` dicts. Wire a weekly integration test that fetches a known product URL and asserts field coverage > 0.8. Alert on regression.

---

## Data Quality Monitoring

1. **Confidence score distribution**: Alert if average `confidence_score` drops below 0.7 for a category (signals site layout change).

2. **Field completeness**: Track `COUNT(*) WHERE description IS NULL` per run. Spike = extraction regression.

3. **Run-over-run delta**: Compare `nbHits` from Algolia with `COUNT(*)` in the DB after each run. Gap > 5% = investigate.

4. **LLM fallback rate**: Log `COUNT(*) WHERE extraction_method='llm_fallback'`. Rising rate = CSS selector drift.

5. **Price sanity check**: Flag products with price outside [$0.01, $10,000] range as anomalies.

6. **Deduplication rate**: Track URLs rejected as duplicates per run. Sudden spike = crawler loop bug.
