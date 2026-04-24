from __future__ import annotations
import structlog
from state import ProductRecord

log = structlog.get_logger()

_seen_hashes: set[str] = set()


def validate_and_score(
    record: ProductRecord,
) -> tuple[ProductRecord, bool, str]:
    if record.url_hash in _seen_hashes:
        return record, False, "duplicate"
    _seen_hashes.add(record.url_hash)

    if not record.name or len(record.name.strip()) < 2:
        return record, False, "missing_name"

    log.info(
        "validated",
        url=record.url,
        method=record.extraction_method,
        sku=record.sku,
    )
    return record, True, "ok"


def seed_seen_hashes(existing_hashes: list[str]) -> None:
    _seen_hashes.update(existing_hashes)


def reset_seen_hashes() -> None:
    _seen_hashes.clear()
