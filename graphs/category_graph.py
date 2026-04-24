from __future__ import annotations
import structlog
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from state import CategoryState, ProductTaskState, ProductRecord
from agents.navigator import collect_category_urls, normalize_algolia_hit

log = structlog.get_logger()


async def navigate_listing(state: CategoryState) -> dict:
    cfg = state["run_config"]
    log.info("navigating_category", category=state["category_name"], url=state["category_url"])

    # Find algolia_filter for this category from run_config
    cat_cfg = next(
        (c for c in cfg["categories"] if c["name"] == state["category_name"]),
        {},
    )
    algolia_filter = cat_cfg.get("algolia_filter", state["category_name"])

    product_urls, algolia_hits = await collect_category_urls(
        category_url=state["category_url"],
        category_name=state["category_name"],
        algolia_filter=algolia_filter,
        max_pages=cfg["max_pages"],
        delay_ms=cfg["delay_ms"],
        max_concurrent=cfg["max_concurrent"],
    )
    product_urls = product_urls[: cfg.get("max_products", 500)]
    # Trim hits to match capped URL list
    algolia_hits = algolia_hits[: len(product_urls)]

    log.info(
        "urls_collected",
        category=state["category_name"],
        product_count=len(product_urls),
        algolia_hits=len(algolia_hits),
    )
    return {
        "product_urls": product_urls,
        "algolia_hits": algolia_hits,
        "subcategory_urls": [],
        "total_pages": max(1, len(product_urls) // 24 + 1),
        "tavily_content_map": {},
    }


async def batch_fetch_pages(state: CategoryState) -> dict:
    """
    Pre-fetch all product page content via TavilyExtract in batches before
    dispatching individual product tasks. Eliminates per-product httpx/Playwright
    calls and CSS selector fragility for detail-page fields.
    """
    from langchain_tavily import TavilyExtract

    cfg = state["run_config"]
    urls = state["product_urls"]
    batch_size = cfg.get("tavily_batch_size", 5)
    extract_depth = cfg.get("tavily_extract_depth", "advanced")

    extractor = TavilyExtract(
        extract_depth=extract_depth,
        include_images=True,
        format="markdown",
    )
    content_map: dict = {}

    log.info(
        "batch_fetch_start",
        category=state["category_name"],
        total_urls=len(urls),
        batch_size=batch_size,
        extract_depth=extract_depth,
    )

    # Variants share a family_url; strip #fragment before fetching so Tavily
    # only fetches each base page once, then fan the result back to all variants.
    base_url_map: dict[str, str] = {}  # base_url → first variant url seen (for logging)
    base_to_variants: dict[str, list[str]] = {}
    for u in urls:
        base = u.split("#")[0]
        base_url_map[base] = u
        base_to_variants.setdefault(base, []).append(u)

    base_urls = list(base_url_map.keys())

    for i in range(0, len(base_urls), batch_size):
        batch = base_urls[i : i + batch_size]
        try:
            result = await extractor.ainvoke({"urls": batch})
            for hit in result.get("results", []):
                fetched_base = hit["url"].split("#")[0]
                entry = {
                    "content": hit.get("raw_content") or hit.get("content", ""),
                    "images": hit.get("images", []),
                }
                for variant_url in base_to_variants.get(fetched_base, [fetched_base]):
                    content_map[variant_url] = entry
            for fail in result.get("failed_results", []):
                failed_base = fail["url"].split("#")[0]
                entry = {"content": None, "images": []}
                for variant_url in base_to_variants.get(failed_base, [failed_base]):
                    content_map[variant_url] = entry
                log.warning(
                    "tavily_batch_url_failed",
                    url=fail["url"],
                    error=fail.get("error"),
                )
        except Exception as e:
            log.error("tavily_batch_error", batch_start=i, error=str(e))
            for base in batch:
                for variant_url in base_to_variants.get(base, [base]):
                    content_map[variant_url] = {"content": None, "images": []}

    fetched = sum(1 for v in content_map.values() if v["content"])
    log.info(
        "batch_fetch_complete",
        category=state["category_name"],
        total=len(urls),
        fetched=fetched,
        failed=len(urls) - fetched,
    )
    return {"tavily_content_map": content_map}


def dispatch_product_tasks(state: CategoryState) -> list[Send]:
    cfg = state["run_config"]
    hits_by_url = {h.get("url"): h for h in state.get("algolia_hits", [])}
    content_map = state.get("tavily_content_map", {})
    sends = []
    for url in state["product_urls"]:
        hit = hits_by_url.get(url, {})
        algolia_data = (
            normalize_algolia_hit(hit, state["category_name"], cfg["run_id"])
            if hit
            else {}
        )
        pre = content_map.get(url, {})
        sends.append(
            Send(
                "extract_product",
                ProductTaskState(
                    product_url=url,
                    category_name=state["category_name"],
                    category_hierarchy=algolia_data.get(
                        "category_hierarchy", [state["category_name"]]
                    ),
                    run_config=cfg,
                    algolia_data=algolia_data,
                    prefetched_content=pre.get("content"),
                    prefetched_images=pre.get("images", []),
                ),
            )
        )
    log.info(
        "dispatching_products",
        category=state["category_name"],
        count=len(sends),
    )
    return sends


async def extract_product(state: ProductTaskState) -> dict:
    from graphs.product_graph import product_graph

    result = await product_graph.ainvoke(
        {
            "product_url": state["product_url"],
            "category_name": state["category_name"],
            "category_hierarchy": state["category_hierarchy"],
            "run_config": state["run_config"],
            "algolia_data": state.get("algolia_data", {}),
            "raw_html": state.get("prefetched_content"),
            "tavily_images": state.get("prefetched_images", []),
            "page_type": None,
            "product": None,
            "extraction_error": None,
        }
    )
    product = result.get("product")
    if product:
        return {"products": [product]}
    return {"products": []}


def reduce_category(state: CategoryState) -> dict:
    log.info(
        "category_complete",
        category=state["category_name"],
        products_extracted=len(state.get("products", [])),
    )
    return {}


def build_category_graph():
    g = StateGraph(CategoryState)
    g.add_node("navigate_listing", navigate_listing)
    g.add_node("batch_fetch_pages", batch_fetch_pages)
    g.add_node("extract_product", extract_product)
    g.add_node("reduce_category", reduce_category)

    g.add_edge(START, "navigate_listing")
    g.add_edge("navigate_listing", "batch_fetch_pages")
    g.add_conditional_edges(
        "batch_fetch_pages",
        dispatch_product_tasks,
        ["extract_product"],
    )
    g.add_edge("extract_product", "reduce_category")
    g.add_edge("reduce_category", END)

    return g.compile()


category_graph = build_category_graph()
