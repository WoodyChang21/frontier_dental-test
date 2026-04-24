from __future__ import annotations
import json
import structlog
from langgraph.graph import StateGraph, START, END

from state import ProductState, ProductRecord
from tools import fetch_page_httpx, render_page_playwright, parse_html
from agents.classifier import classify_with_llm
from agents.extractor import build_product_record
from agents.validator import validate_and_score

log = structlog.get_logger()


async def fetch_product_page(state: ProductState) -> dict:
    """
    Fetch product detail page content.
    If batch_fetch_pages already pre-fetched this URL, this is a no-op.
    Otherwise: try TavilyExtract first, fall back to httpx + Playwright.
    """
    # Pre-fetched by batch_fetch_pages — skip if content already injected
    if state.get("raw_html") is not None:
        return {}

    # Strip #sku fragment — it's our internal variant key, not a real page anchor
    url = state["product_url"].split("#")[0]
    cfg = state["run_config"]

    # Try TavilyExtract as the primary individual fetch
    try:
        from langchain_tavily import TavilyExtract
        extractor = TavilyExtract(
            extract_depth=cfg.get("tavily_extract_depth", "advanced"),
            include_images=True,
            format="markdown",
        )
        result = await extractor.ainvoke({"urls": [url]})
        hits = result.get("results", [])
        if hits:
            log.info("tavily_individual_fetch", url=url)
            return {
                "raw_html": hits[0].get("raw_content") or hits[0].get("content", ""),
                "tavily_images": hits[0].get("images", []),
            }
        failed = result.get("failed_results", [])
        log.warning(
            "tavily_individual_no_results",
            url=url,
            error=failed[0].get("error") if failed else "empty response",
        )
    except Exception as e:
        log.warning("tavily_individual_failed", url=url, error=str(e))

    # Playwright/httpx fallback when Tavily fails
    log.info("playwright_fallback", url=url)
    try:
        html = await fetch_page_httpx(url, delay_ms=cfg["delay_ms"])
        soup = parse_html(html)
        if not soup.select_one("h1.page-title, [itemprop='name']"):
            html = await render_page_playwright(
                url,
                delay_ms=cfg["delay_ms"],
                max_concurrent=cfg["max_concurrent"],
            )
        return {"raw_html": html, "tavily_images": []}
    except Exception as e:
        log.error("fetch_failed", url=url, error=str(e))
        return {"raw_html": None, "extraction_error": str(e)}


async def classify_product_page(state: ProductState) -> dict:
    """
    If we have Algolia data, we already know this is a product detail page.
    For the rare no-Algolia case, use URL heuristic then LLM classifier.
    """
    if state.get("algolia_data"):
        return {"page_type": "product_detail"}

    content = state.get("raw_html")
    if not content:
        return {"page_type": "irrelevant"}

    url = state["product_url"].split("#")[0]
    # URL-depth heuristic works regardless of content format
    segments = [s for s in url.split("/") if s and s not in ("https:", "http:", "www.safcodental.com")]
    if "/product/" in url or len(segments) >= 5:
        return {"page_type": "product_detail"}

    # Determine if content is Tavily markdown or raw HTML
    is_markdown = bool(state.get("tavily_images") is not None and not content.strip().startswith("<"))
    result = await classify_with_llm(
        url, content, model=state["run_config"]["llm_model"], is_markdown=is_markdown
    )
    log.info(
        "classified",
        url=url,
        page_type=result.page_type,
        confidence=result.confidence,
    )
    return {"page_type": result.page_type}


async def _extract_supplementary_llm(
    content: str,
    url: str,
    category: str,
    model: str,
    algolia: dict,
) -> tuple[dict, float]:
    """
    Extract description/specs/unit_pack_size/alternatives from clean Tavily markdown.
    When Algolia already has name/sku/price/brand, this focused prompt uses ~700
    input tokens vs ~1500 for noisy HTML in the old css+llm_fallback path.
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    # Safco's Tavily markdown is ~100K chars of site chrome before the product section.
    # Find the last H1 heading (the product title) and slice from there so the LLM
    # sees description/specs/pricing instead of navigation menus.
    h1_idx = content.rfind("\n# ")
    product_section = content[h1_idx:] if h1_idx != -1 else content[-6000:]
    trimmed = product_section[:6000]
    if algolia:
        context = (
            f"name={algolia.get('name')}, "
            f"sku={algolia.get('sku')}, "
            f"brand={algolia.get('brand')}"
        )
    else:
        context = "unknown"

    prompt = f"""Extract supplementary fields for this dental product.
Product context (from catalog): {context}
URL: {url} | Category: {category}

Page content (markdown):
---
{trimmed}
---

Return ONLY valid JSON with these fields (null if not found):
{{"description": "product description paragraph", "unit_pack_size": "e.g. 100/box or 1 each", "specifications": {{"key": "value"}}, "alternative_products": ["url1", "url2"]}}"""

    resp = await client.chat.completions.create(
        model=model,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    try:
        fields = json.loads(resp.choices[0].message.content.strip())
    except Exception:
        fields = {}

    score = 0.5 if fields.get("description") else 0.2
    return fields, score


async def extract_structured(state: ProductState) -> dict:
    """
    Merge Algolia core fields with Tavily+LLM supplementary fields.
    Algolia wins on all scalar fields it provides.
    LLM extracts description/specs/alternatives from clean Tavily markdown.
    """
    if state.get("page_type") != "product_detail":
        log.info("skipping_non_product", url=state["product_url"])
        return {"product": None}

    cfg = state["run_config"]
    algolia = state.get("algolia_data") or {}

    llm_fields: dict = {}
    llm_score = 0.0
    if state.get("raw_html"):
        llm_fields, llm_score = await _extract_supplementary_llm(
            content=state["raw_html"],
            url=state["product_url"],
            category=state["category_name"],
            model=cfg["llm_model"],
            algolia=algolia,
        )

    # Image priority: Algolia > Tavily-extracted > LLM-extracted
    image_urls = (
        algolia.get("image_urls")
        or state.get("tavily_images", [])
        or llm_fields.get("image_urls", [])
    )

    merged = {
        "name": algolia.get("name") or llm_fields.get("name", ""),
        "brand": algolia.get("brand") or llm_fields.get("brand"),
        "sku": algolia.get("sku") or llm_fields.get("sku"),
        "price": algolia.get("price") or llm_fields.get("price"),
        "availability": algolia.get("availability") or llm_fields.get("availability"),
        "description": llm_fields.get("description"),
        "unit_pack_size": llm_fields.get("unit_pack_size"),
        "specifications": llm_fields.get("specifications", {}),
        "image_urls": image_urls,
        "alternative_products": llm_fields.get("alternative_products", []),
    }

    algolia_boost = 0.35 if algolia.get("name") else 0.0
    final_score = min(1.0, llm_score + algolia_boost)
    method = "algolia+tavily_llm" if algolia else "tavily_llm"

    record = build_product_record(
        fields=merged,
        url=state["product_url"],
        category=state["category_name"],
        category_hierarchy=state["category_hierarchy"],
        run_id=cfg["run_id"],
        extraction_method=method,
        confidence_score=final_score,
    )
    log.info(
        "extracted",
        url=state["product_url"],
        score=final_score,
        method=method,
        name=record.name,
    )
    return {"product": record}


async def llm_extract_fallback(state: ProductState) -> dict:
    """
    Last-resort extraction: Tavily failed AND Algolia had no data.
    Passes whatever content is available (HTML or markdown) to the full LLM extractor.
    """
    cfg = state["run_config"]
    current = state.get("product")
    log.info(
        "llm_fallback_triggered",
        url=state["product_url"],
        score=current.confidence_score if current else 0,
    )
    try:
        from agents.extractor import llm_extract

        # Detect if content is already clean markdown (from Tavily fallback)
        content = state["raw_html"]
        is_markdown = bool(
            state.get("tavily_images") is not None
            and content
            and not content.strip().startswith("<")
        )
        fields = await llm_extract(
            html=content,
            url=state["product_url"],
            category=state["category_name"],
            model=cfg["llm_model"],
            is_markdown=is_markdown,
        )
        record = build_product_record(
            fields=fields,
            url=state["product_url"],
            category=state["category_name"],
            category_hierarchy=state["category_hierarchy"],
            run_id=cfg["run_id"],
            extraction_method="llm_fallback",
            confidence_score=0.80,
        )
        log.info("llm_extracted", url=state["product_url"], name=record.name)
        return {"product": record}
    except Exception as e:
        log.error("llm_fallback_failed", url=state["product_url"], error=str(e))
        return {}


async def validate_product(state: ProductState) -> dict:
    product = state.get("product")
    if not product:
        return {}
    updated, is_valid, reason = validate_and_score(product)
    if not is_valid:
        log.warning("product_rejected", url=state["product_url"], reason=reason)
        return {"product": None}
    return {"product": updated}


def should_use_llm_fallback(state: ProductState) -> str:
    product = state.get("product")
    if product is None:
        return "validate"
    # LLM now runs inside extract_structured whenever content is present.
    # Only enter the separate fallback node if content was completely absent
    # AND Algolia also missed this product.
    if state.get("raw_html") is None and not state.get("algolia_data"):
        threshold = state["run_config"].get("extraction_threshold", 0.65)
        if product.confidence_score < threshold:
            return "llm_fallback"
    return "validate"


def build_product_graph():
    g = StateGraph(ProductState)
    g.add_node("fetch_page", fetch_product_page)
    g.add_node("classify_page", classify_product_page)
    g.add_node("extract_structured", extract_structured)
    g.add_node("llm_fallback", llm_extract_fallback)
    g.add_node("validate", validate_product)

    g.add_edge(START, "fetch_page")
    g.add_edge("fetch_page", "classify_page")
    g.add_edge("classify_page", "extract_structured")
    g.add_conditional_edges(
        "extract_structured",
        should_use_llm_fallback,
        {"llm_fallback": "llm_fallback", "validate": "validate"},
    )
    g.add_edge("llm_fallback", "validate")
    g.add_edge("validate", END)

    return g.compile()


product_graph = build_product_graph()
