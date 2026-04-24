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
    }


def dispatch_product_tasks(state: CategoryState) -> list[Send]:
    cfg = state["run_config"]
    hits_by_url = {h.get("url"): h for h in state.get("algolia_hits", [])}
    sends = []
    for url in state["product_urls"]:
        hit = hits_by_url.get(url, {})
        algolia_data = (
            normalize_algolia_hit(hit, state["category_name"], cfg["run_id"])
            if hit
            else {}
        )
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
            "raw_html": None,
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
    g.add_node("extract_product", extract_product)
    g.add_node("reduce_category", reduce_category)

    g.add_edge(START, "navigate_listing")
    g.add_conditional_edges(
        "navigate_listing",
        dispatch_product_tasks,
        ["extract_product"],
    )
    g.add_edge("extract_product", "reduce_category")
    g.add_edge("reduce_category", END)

    return g.compile()


category_graph = build_category_graph()
