from __future__ import annotations
import json
import sqlite3
from pathlib import Path

import pandas as pd
import structlog

log = structlog.get_logger()

JSON_COLS = ["category_hierarchy", "specifications", "image_urls", "alternative_products"]


def export_all(
    conn: sqlite3.Connection,
    output_dir: str,
    csv_name: str,
    json_name: str,
) -> dict:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    df = pd.read_sql("SELECT * FROM products ORDER BY category, name", conn)
    df = df.drop(columns=["confidence_score"], errors="ignore")

    for col in JSON_COLS:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: json.loads(x) if x else None)

    csv_path = out / csv_name
    json_path = out / json_name

    # Flatten JSON cols back to strings for CSV
    df_csv = df.copy()
    for col in JSON_COLS:
        if col in df_csv.columns:
            df_csv[col] = df_csv[col].apply(
                lambda x: json.dumps(x) if isinstance(x, (list, dict)) else x
            )
    df_csv.to_csv(csv_path, index=False)

    # JSON keeps nested structure
    records = df.to_dict(orient="records")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False, default=str)

    log.info("export_complete", csv=str(csv_path), json=str(json_path), rows=len(df))
    return {"csv": str(csv_path), "json": str(json_path), "count": len(df)}
