from __future__ import annotations
import uuid
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScraperConfig:
    categories: list[dict] = field(default_factory=list)
    max_pages_per_category: int = 20
    max_products_per_category: int = 500
    delay_between_requests_ms: int = 1500
    max_concurrent_products: int = 3
    request_timeout_seconds: int = 30
    playwright_headless: bool = True
    tavily_extract_depth: str = "advanced"
    tavily_batch_size: int = 5
    llm_model: str = "claude-sonnet-4-6"
    max_tokens: int = 2048
    extraction_fallback_threshold: float = 0.65
    temperature: float = 0.0
    output_dir: str = "output"
    db_filename: str = "safco_products.db"
    csv_filename: str = "safco_products.csv"
    json_filename: str = "safco_products.json"
    checkpoint_db: str = "checkpoints.db"
    log_level: str = "INFO"
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    fresh_run: bool = False

    @classmethod
    def from_yaml(cls, path: str = "config.yaml") -> "ScraperConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)
        cfg = cls()
        cfg.categories = raw.get("categories", [])
        for section in ("scraper", "llm", "storage", "logging"):
            for k, v in raw.get(section, {}).items():
                # map yaml keys to dataclass field names
                key_map = {
                    "delay_between_requests_ms": "delay_between_requests_ms",
                    "level": "log_level",
                    "model": "llm_model",
                    "extraction_fallback_threshold": "extraction_fallback_threshold",
                }
                attr = key_map.get(k, k)
                if hasattr(cfg, attr):
                    setattr(cfg, attr, v)
        return cfg

    def to_runnable_config(self) -> dict:
        return {"configurable": {"thread_id": self.run_id}}

    def to_run_config_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "categories": self.categories,
            "output_dir": self.output_dir,
            "max_pages": self.max_pages_per_category,
            "max_products": self.max_products_per_category,
            "delay_ms": self.delay_between_requests_ms,
            "max_concurrent": self.max_concurrent_products,
            "llm_model": self.llm_model,
            "extraction_threshold": self.extraction_fallback_threshold,
            "checkpoint_db": self.checkpoint_db,
            "db_filename": self.db_filename,
            "csv_filename": self.csv_filename,
            "json_filename": self.json_filename,
            "fresh_run": self.fresh_run,
            "tavily_extract_depth": self.tavily_extract_depth,
            "tavily_batch_size": self.tavily_batch_size,
        }
