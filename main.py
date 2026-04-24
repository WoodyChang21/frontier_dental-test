from __future__ import annotations
import asyncio
import sys

import click
import structlog
from dotenv import load_dotenv

from pathlib import Path as _Path
load_dotenv(_Path(__file__).parent / ".env")


def setup_logging(level: str = "INFO"):
    import logging
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if sys.stderr.isatty()
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )


log = structlog.get_logger()


@click.command()
@click.option("--config", default="config.yaml", help="Path to config.yaml")
@click.option("--resume", default=None, help="Resume a previous run_id")
@click.option("--fresh", is_flag=True, default=False, help="Ignore previously scraped products and re-scrape everything")
@click.option(
    "--max-products",
    default=None,
    type=int,
    help="Override max products per category",
)
@click.option(
    "--categories",
    default=None,
    help="Comma-separated category names to scrape (must match config.yaml names)",
)
def main(
    config: str,
    resume: str | None,
    fresh: bool,
    max_products: int | None,
    categories: str | None,
):
    """Safco Dental LangGraph agentic product scraper."""
    from config import ScraperConfig

    cfg = ScraperConfig.from_yaml(config)
    setup_logging(cfg.log_level)

    if resume:
        cfg.run_id = resume
        log.info("resuming_run", run_id=resume)
    else:
        log.info("starting_run", run_id=cfg.run_id)

    if fresh:
        cfg.fresh_run = True
        log.info("fresh_run_enabled", note="previously scraped products will be re-scraped")

    if max_products:
        cfg.max_products_per_category = max_products

    if categories:
        selected = [c.strip() for c in categories.split(",")]
        cfg.categories = [c for c in cfg.categories if c["name"] in selected]
        if not cfg.categories:
            click.echo(f"No matching categories found for: {categories}", err=True)
            sys.exit(1)

    asyncio.run(_run(cfg))


async def _run(cfg):
    from graphs.main_graph import build_main_graph
    from state import MainState
    from tools import close_browser
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    thread_config = cfg.to_runnable_config()

    initial_state = MainState(
        run_config=cfg.to_run_config_dict(),
        products=[],
        errors=[],
        run_metadata={},
    )

    log.info(
        "graph_starting",
        thread_id=thread_config["configurable"]["thread_id"],
        categories=[c["name"] for c in cfg.categories],
    )

    async with AsyncSqliteSaver.from_conn_string(cfg.checkpoint_db) as checkpointer:
        graph = build_main_graph(checkpointer=checkpointer)
        try:
            async for event in graph.astream(
                initial_state,
                config=thread_config,
                stream_mode="updates",
            ):
                node_name = next(iter(event.keys()))
                data = event[node_name]
                products_delta = len(data.get("products", []))
                if products_delta:
                    log.info("node_complete", node=node_name, products_in_batch=products_delta)
                else:
                    log.info("node_complete", node=node_name)
        except KeyboardInterrupt:
            log.warning("interrupted_by_user", run_id=cfg.run_id,
                        hint=f"Resume with: python main.py --resume {cfg.run_id}")
        except Exception as e:
            log.error("run_failed", error=str(e), run_id=cfg.run_id)
            raise
        finally:
            await close_browser()
            log.info("run_finished", run_id=cfg.run_id)


if __name__ == "__main__":
    main()
