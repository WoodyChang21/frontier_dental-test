# Safco Dental Agentic Product Scraper

A LangGraph-based multi-agent scraping system that extracts structured product catalogs from [Safco Dental Supply](https://www.safcodental.com) for two categories: **Dental Exam Gloves** and **Sutures & Surgical Products**.

Two implementation branches are provided, each with the same high-level graph architecture but a different strategy for extracting product detail page content. See the comparison below to understand the trade-offs.

> **This `main` branch runs the Tavily-based implementation (`algolia-tavily`).** It was chosen as the primary version because it produces more complete records (100% description coverage vs ~50% for CSS) and runs faster in wall-clock time. The CSS-based variant lives in the `algolia-css` branch.

---

## Branches

| Branch | Detail Page Strategy | LLM Usage | Sample Run (20 products) |
|---|---|---|---|
| [`algolia-css`](../../tree/algolia-css) | httpx → Playwright; CSS selectors (Hyvä/Alpine.js theme) | Fallback only — triggers when no Algolia data and CSS finds no description | ~117s |
| [`algolia-tavily`](../../tree/algolia-tavily) | TavilyExtract batch pre-fetch (clean markdown) | Every product — focused prompt for description, specs, pack size | ~35s |

Both branches share the same **three-graph LangGraph architecture**: a Main Graph dispatches two Category Subgraphs in parallel; each Category Subgraph fans out to N Product Subgraphs — one per URL — via `Send()`.

---

## Approach Comparison

### What is the same

- **Algolia API for product discovery.** Both branches intercept the Algolia session key via Playwright, then query Algolia over plain HTTP to collect all product URLs and structured scalar fields (name, SKU, price, brand, images, availability, category hierarchy). This is the key insight that drives both approaches: Safco's product grids are powered by Algolia, which exposes clean structured JSON without any CSS parsing.
- **Two-layer extraction.** Algolia wins on all scalar fields it provides. The detail page fills supplementary fields (description, specifications, unit pack size, related products) that Algolia does not carry.
- **Validation.** SHA256 deduplication by URL, rejection of missing/short names, idempotent SQLite upsert.
- **Checkpointing and resume.** `AsyncSqliteSaver` checkpoints at every node boundary; `--resume <run_id>` restarts from the last completed node.
- **Output schema.** Identical SQLite/CSV/JSON field structure across both branches.

### What differs

**`algolia-css`** fetches each product detail page directly (httpx, Playwright fallback) and parses it with CSS selectors targeting Safco's Hyvä/Alpine.js theme. The LLM is a last-resort fallback that only fires when both Algolia data and CSS description extraction are absent — which almost never happens in practice. This makes it cheaper to run but yields lower description coverage (~50% on Sutures & Surgical Products in the sample run) because many product descriptions are rendered dynamically and CSS cannot reach them without a full browser render.

**`algolia-tavily`** batch-pre-fetches all product URLs through TavilyExtract before any product task runs. Tavily returns clean markdown instead of raw HTML; the LLM then extracts description, specs, pack size, and alternative products from that markdown on every product (~700 input tokens per call). The trade-off is a higher per-run API cost but significantly better description coverage (100% in the sample run) and faster wall-clock time because the batch pre-fetch and all LLM calls fan out in parallel.

In short: `algolia-css` is cheaper and more self-contained; `algolia-tavily` is faster in wall-clock time and produces more complete records.

---

## How This Was Built

This project was completed with AI assistance throughout the implementation. The overall workflow was human-directed: I specified the high-level LangGraph node structure and subgraph layout — which nodes to create, how they connect, and how the `Send()` fan-out should work for parallel product extraction.

A key early discovery, made with AI help, was the presence of the Algolia API powering Safco's product catalog. Rather than scraping the rendered HTML grid, intercepting the Algolia session key and querying the API directly gave structured JSON for every product with no CSS fragility.

The first implementation used CSS-based detail page extraction (`algolia-css`). It works, but based on my prior experience with AI agent data scraping, CSS approaches tend to be brittle against dynamic rendering and theme changes. I then implement a Tavily-based variant (`algolia-tavily`), which from experience typically produces more stable and complete results — at the cost of more LLM calls. The Tavily approach confirmed this: 100% description coverage vs ~50% for CSS on the same 20-product sample, and faster wall-clock time despite the additional API calls.

---

## Common Setup & Run

Both branches use the same install steps and CLI interface. Check out the branch you want to run, then follow the instructions below.

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

### Secrets

Create a `.env` file in the project root (never commit this):

```
OPENAI_API_KEY=sk-...        # required by both branches
TAVILY_API_KEY=tvly-...      # required by algolia-tavily only
LANGSMITH_API_KEY=ls__...    # optional — enables LangGraph tracing
```

### Run

```bash
# Full scrape (both categories)
python main.py

# Quick test — 10 products per category
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
