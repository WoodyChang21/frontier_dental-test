from __future__ import annotations
from pathlib import Path

import structlog
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from state import MainState, CategoryState
from storage.db import (
    get_connection,
    upsert_batch,
    get_existing_hashes,
    record_run_start,
    record_run_end,
)
from storage.exporter import export_all
from agents.validator import seed_seen_hashes

log = structlog.get_logger()


async def initialize_run(state: MainState) -> dict:
    cfg = state["run_config"]
    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)

    db_path = str(out / cfg["db_filename"])
    conn = get_connection(db_path)
    record_run_start(conn, cfg["run_id"], cfg["categories"])

    existing = set()
    if not cfg.get("fresh_run"):
        existing = get_existing_hashes(conn)
        seed_seen_hashes(existing)
    conn.close()

    log.info(
        "run_initialized",
        run_id=cfg["run_id"],
        categories=len(cfg["categories"]),
        existing_products=len(existing),
    )
    return {"run_metadata": {"db_path": db_path, "initialized": True}}


def dispatch_categories(state: MainState) -> list[Send]:
    cfg = state["run_config"]
    sends = []
    for cat in cfg["categories"]:
        sends.append(
            Send(
                "crawl_category",
                CategoryState(
                    category_url=cat["url"],
                    category_name=cat["name"],
                    run_config=cfg,
                    product_urls=[],
                    algolia_hits=[],
                    subcategory_urls=[],
                    current_page=1,
                    total_pages=0,
                    products=[],
                    errors=[],
                ),
            )
        )
    log.info("dispatching_categories", count=len(sends))
    return sends


async def crawl_category(state: CategoryState) -> dict:
    from graphs.category_graph import category_graph

    result = await category_graph.ainvoke(state)
    products = result.get("products", [])
    errors = result.get("errors", [])

    # Flush to SQLite immediately for crash safety
    if products:
        cfg = state["run_config"]
        db_path = str(Path(cfg["output_dir"]) / cfg["db_filename"])
        conn = get_connection(db_path)
        upsert_batch(conn, products)
        conn.close()
        log.info(
            "category_persisted",
            category=state["category_name"],
            count=len(products),
        )

    return {"products": products, "errors": errors}


async def reduce_products(state: MainState) -> dict:
    total = len(state.get("products", []))
    log.info("reduce_complete", total_products=total)
    return {"run_metadata": {**state.get("run_metadata", {}), "total_products": total}}


async def export_results(state: MainState) -> dict:
    cfg = state["run_config"]
    db_path = str(Path(cfg["output_dir"]) / cfg["db_filename"])
    conn = get_connection(db_path)

    result = export_all(
        conn=conn,
        output_dir=cfg["output_dir"],
        csv_name=cfg["csv_filename"],
        json_name=cfg["json_filename"],
    )
    total = result["count"]
    record_run_end(conn, cfg["run_id"], total)
    conn.close()

    log.info("export_complete", **result)
    return {
        "run_metadata": {**state.get("run_metadata", {}), "exports": result}
    }


def build_main_graph(checkpointer=None):
    g = StateGraph(MainState)
    g.add_node("initialize_run", initialize_run)
    g.add_node("crawl_category", crawl_category)
    g.add_node("reduce_products", reduce_products)
    g.add_node("export_results", export_results)

    g.add_edge(START, "initialize_run")
    g.add_conditional_edges(
        "initialize_run",
        dispatch_categories,
        ["crawl_category"],
    )
    g.add_edge("crawl_category", "reduce_products")
    g.add_edge("reduce_products", "export_results")
    g.add_edge("export_results", END)

    return g.compile(checkpointer=checkpointer)
