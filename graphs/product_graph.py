from __future__ import annotations
import structlog
from langgraph.graph import StateGraph, START, END

from state import ProductState, ProductRecord
from tools import fetch_page_httpx, render_page_playwright, parse_html
from agents.classifier import classify_with_llm
from agents.extractor import css_extract, llm_extract, build_product_record
from agents.validator import validate_and_score

log = structlog.get_logger()


async def fetch_product_page(state: ProductState) -> dict:
    """
    Fetch product detail page via httpx; fall back to Playwright for
    client-rendered pages (Safco uses a Hyvä/Alpine.js theme).
    """
    url = state["product_url"]
    cfg = state["run_config"]

    try:
        html = await fetch_page_httpx(url, delay_ms=cfg["delay_ms"])
        soup = parse_html(html)
        # If the rendered HTML lacks product markers, the page is client-rendered
        if not soup.select_one("h1.page-title, [itemprop='name']"):
            html = await render_page_playwright(
                url,
                delay_ms=cfg["delay_ms"],
                max_concurrent=cfg["max_concurrent"],
            )
        return {"raw_html": html}
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

    url = state["product_url"]
    segments = [s for s in url.split("/") if s and s not in ("https:", "http:", "www.safcodental.com")]
    if "/product/" in url or len(segments) >= 5:
        return {"page_type": "product_detail"}

    result = await classify_with_llm(
        url, content, model=state["run_config"]["llm_model"], is_markdown=False
    )
    log.info("classified", url=url, page_type=result.page_type, confidence=result.confidence)
    return {"page_type": result.page_type}


async def extract_structured(state: ProductState) -> dict:
    """
    Merge Algolia core fields with CSS-extracted supplementary fields.
    Algolia wins on all scalar fields it provides (name, SKU, price, brand,
    availability, images). CSS extraction supplies description, specifications,
    and alternative product links from the rendered detail page HTML.
    """
    if state.get("page_type") != "product_detail":
        log.info("skipping_non_product", url=state["product_url"])
        return {"product": None}

    cfg = state["run_config"]
    algolia = state.get("algolia_data") or {}

    css_fields: dict = {}
    css_score = 0.0
    if state.get("raw_html"):
        soup = parse_html(state["raw_html"])
        css_fields, css_score = css_extract(
            soup=soup,
            url=state["product_url"],
            category=state["category_name"],
            category_hierarchy=state["category_hierarchy"],
            run_id=cfg["run_id"],
        )

    # Image priority: Algolia > CSS-extracted
    image_urls = (
        algolia.get("image_urls")
        or css_fields.get("image_urls", [])
    )

    merged = {
        "name":               algolia.get("name") or css_fields.get("name", ""),
        "brand":              algolia.get("brand") or css_fields.get("brand"),
        "sku":                algolia.get("sku") or css_fields.get("sku"),
        "price":              algolia.get("price") or css_fields.get("price"),
        "availability":       algolia.get("availability") or css_fields.get("availability"),
        "description":        css_fields.get("description"),
        "unit_pack_size":     css_fields.get("unit_pack_size"),
        "specifications":     css_fields.get("specifications", {}),
        "image_urls":         image_urls,
        "alternative_products": css_fields.get("alternative_products", []),
    }

    # Algolia supplies the primary scalars; CSS adds supplementary coverage
    algolia_boost = 0.3 if algolia.get("name") else 0.0
    final_score = min(1.0, css_score + algolia_boost)
    method = "algolia+css" if algolia else "css"

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
    Last-resort extraction: no Algolia data AND CSS confidence below threshold.
    Strips noisy tags and passes cleaned HTML to the full LLM extractor.
    """
    cfg = state["run_config"]
    current = state.get("product")
    log.info(
        "llm_fallback_triggered",
        url=state["product_url"],
        score=current.confidence_score if current else 0,
    )
    try:
        fields = await llm_extract(
            html=state["raw_html"],
            url=state["product_url"],
            category=state["category_name"],
            model=cfg["llm_model"],
            is_markdown=False,
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
    # LLM fallback only when Algolia has no data AND CSS score is below threshold.
    # When Algolia supplies core fields, the record is kept even with low CSS coverage.
    if not state.get("algolia_data"):
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
