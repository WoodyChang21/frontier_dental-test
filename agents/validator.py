from __future__ import annotations
import structlog
from state import ProductRecord

log = structlog.get_logger()

_seen_hashes: set[str] = set()

CONFIDENCE_PENALTIES = {
    "no_sku": 0.10,
    "no_price": 0.10,
    "no_brand": 0.05,
    "no_description": 0.05,
    "no_images": 0.05,
}


def validate_and_score(
    record: ProductRecord,
) -> tuple[ProductRecord, bool, str]:
    if record.url_hash in _seen_hashes:
        return record, False, "duplicate"
    _seen_hashes.add(record.url_hash)

    if not record.name or len(record.name.strip()) < 2:
        return record, False, "missing_name"

    score = record.confidence_score
    if not record.sku:
        score -= CONFIDENCE_PENALTIES["no_sku"]
    if not record.price:
        score -= CONFIDENCE_PENALTIES["no_price"]
    if not record.brand:
        score -= CONFIDENCE_PENALTIES["no_brand"]
    if not record.description:
        score -= CONFIDENCE_PENALTIES["no_description"]
    if not record.image_urls:
        score -= CONFIDENCE_PENALTIES["no_images"]

    record = record.model_copy(update={"confidence_score": max(0.0, round(score, 3))})
    log.info(
        "validated",
        url=record.url,
        score=record.confidence_score,
        method=record.extraction_method,
        sku=record.sku,
    )
    return record, True, "ok"


def seed_seen_hashes(existing_hashes: list[str]) -> None:
    _seen_hashes.update(existing_hashes)


def reset_seen_hashes() -> None:
    _seen_hashes.clear()
