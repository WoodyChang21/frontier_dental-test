from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import structlog

from state import ProductRecord

log = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    url_hash             TEXT PRIMARY KEY,
    url                  TEXT NOT NULL,
    run_id               TEXT NOT NULL,
    category             TEXT,
    category_hierarchy   TEXT,
    name                 TEXT,
    brand                TEXT,
    sku                  TEXT,
    price                TEXT,
    unit_pack_size       TEXT,
    availability         TEXT,
    description          TEXT,
    specifications       TEXT,
    image_urls           TEXT,
    alternative_products TEXT,
    extraction_method    TEXT,
    confidence_score     REAL,
    scraped_at           TEXT
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id         TEXT PRIMARY KEY,
    started_at     TEXT,
    finished_at    TEXT,
    status         TEXT,
    categories     TEXT,
    total_products INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_category  ON products(category);
CREATE INDEX IF NOT EXISTS idx_sku       ON products(sku);
CREATE INDEX IF NOT EXISTS idx_run_id    ON products(run_id);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_product(conn: sqlite3.Connection, record: ProductRecord) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO products VALUES (
            :url_hash, :url, :run_id, :category, :category_hierarchy,
            :name, :brand, :sku, :price, :unit_pack_size, :availability,
            :description, :specifications, :image_urls, :alternative_products,
            :extraction_method, :confidence_score, :scraped_at
        )
        """,
        {
            "url_hash": record.url_hash,
            "url": record.url,
            "run_id": record.run_id,
            "category": record.category,
            "category_hierarchy": json.dumps(record.category_hierarchy),
            "name": record.name,
            "brand": record.brand,
            "sku": record.sku,
            "price": record.price,
            "unit_pack_size": record.unit_pack_size,
            "availability": record.availability,
            "description": record.description,
            "specifications": json.dumps(record.specifications),
            "image_urls": json.dumps(record.image_urls),
            "alternative_products": json.dumps(record.alternative_products),
            "extraction_method": record.extraction_method,
            "confidence_score": record.confidence_score,
            "scraped_at": record.scraped_at.isoformat(),
        },
    )
    conn.commit()


def upsert_batch(conn: sqlite3.Connection, records: list[ProductRecord]) -> int:
    for record in records:
        upsert_product(conn, record)
    log.info("db_batch_upsert", count=len(records))
    return len(records)


def get_existing_hashes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT url_hash FROM products").fetchall()
    return [r["url_hash"] for r in rows]


def record_run_start(
    conn: sqlite3.Connection, run_id: str, categories: list
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO scrape_runs VALUES (?,?,NULL,'running',?,0)",
        (run_id, datetime.utcnow().isoformat(), json.dumps(categories)),
    )
    conn.commit()


def record_run_end(
    conn: sqlite3.Connection, run_id: str, total: int, status: str = "completed"
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, status=?, total_products=? WHERE run_id=?",
        (datetime.utcnow().isoformat(), status, total, run_id),
    )
    conn.commit()
