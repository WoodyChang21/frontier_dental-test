from __future__ import annotations
import re
import asyncio
from typing import Optional
from urllib.parse import urlencode

import httpx
import structlog

log = structlog.get_logger()

ALGOLIA_HOST = "https://a5ulkntm8n-dsn.algolia.net"
ALGOLIA_APP_ID = "A5ULKNTM8N"
# Products index — use ordered variant for consistent pagination
ALGOLIA_PRODUCTS_INDEX = "safco_prod_default_products_ordered_qty_desc"


async def extract_algolia_key(category_url: str, delay_ms: int = 2000) -> Optional[str]:
    """
    Load the category page via Playwright and intercept the Algolia API key
    from the first products query. Key is valid ~23 hours per session.
    """
    from tools import render_page_playwright
    from playwright.async_api import async_playwright

    captured_key: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        async def on_response(resp):
            url = resp.url
            if "algolia.net/1/indexes" in url and "products" in url:
                m = re.search(r"x-algolia-api-key=([^&]+)", url)
                if m and not captured_key:
                    captured_key.append(m.group(1))

        page.on("response", on_response)
        try:
            await page.goto(category_url, wait_until="load", timeout=30000)
            await asyncio.sleep(delay_ms / 1000)
        except Exception as e:
            log.error("algolia_key_extraction_failed", url=category_url, error=str(e))
        finally:
            await page.close()
            await context.close()
            await browser.close()

    key = captured_key[0] if captured_key else None
    if key:
        log.info("algolia_key_extracted", key_prefix=key[:20] + "...")
    else:
        log.warning("algolia_key_not_found", url=category_url)
    return key


ALGOLIA_PRODUCTS_INDEX = "safco_prod_default_products"


async def query_algolia_products(
    api_key: str,
    algolia_filter: str,
    page: int = 0,
    hits_per_page: int = 50,
) -> dict:
    """
    Query Algolia products index using facetFilters (matching the site's own query format).
    algolia_filter: e.g. "Dental Supplies /// Dental Exam Gloves"
    Returns raw Algolia response with hits, nbHits, nbPages.
    """
    url = (
        f"{ALGOLIA_HOST}/1/indexes/{ALGOLIA_PRODUCTS_INDEX}/query"
        f"?x-algolia-agent=safco-scraper-1.0"
        f"&x-algolia-api-key={api_key}"
        f"&x-algolia-application-id={ALGOLIA_APP_ID}"
    )
    payload = {
        "query": "",
        "facetFilters": [[f"categories.level1:{algolia_filter}"]],
        "hitsPerPage": hits_per_page,
        "page": page,
        "attributesToRetrieve": [
            "name", "sku", "url", "price", "manufacturer_name",
            "stock_availability", "thumbnail_url", "image_url",
            "categories", "family_url", "family_title", "type_id",
            "objectID",
        ],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def collect_category_urls(
    category_url: str,
    category_name: str,
    algolia_filter: str,
    max_pages: int,
    delay_ms: int,
    max_concurrent: int,
    algolia_key: Optional[str] = None,
) -> tuple[list[str], list[dict]]:
    """
    Returns (product_urls, algolia_hits) for the category.
    Uses Algolia API for complete, paginated product discovery.
    Falls back to HTML scraping if Algolia unavailable.
    """
    if algolia_key is None:
        algolia_key = await extract_algolia_key(category_url, delay_ms=delay_ms)

    if algolia_key:
        return await _collect_via_algolia(
            algolia_key, algolia_filter, category_name, max_pages
        )
    else:
        log.warning("falling_back_to_html_scrape", category=category_name)
        urls, _ = await _collect_via_html(
            category_url, category_name, max_pages, delay_ms, max_concurrent
        )
        return urls, []


async def _collect_via_algolia(
    api_key: str,
    algolia_filter: str,
    category_name: str,
    max_pages: int,
) -> tuple[list[str], list[dict]]:
    """Query all Algolia pages for the category, collect URLs + rich hit data."""
    all_hits: list[dict] = []
    page = 0

    while page < max_pages:
        try:
            result = await query_algolia_products(api_key, algolia_filter, page=page)
        except Exception as e:
            log.error("algolia_query_failed", page=page, error=str(e))
            break

        hits = result.get("hits", [])
        nb_pages = result.get("nbPages", 1)
        nb_hits = result.get("nbHits", 0)

        if page == 0:
            log.info(
                "algolia_catalog_info",
                category=category_name,
                total_products=nb_hits,
                total_pages=nb_pages,
            )

        if not hits:
            break

        all_hits.extend(hits)
        log.info(
            "algolia_page_fetched",
            page=page,
            hits=len(hits),
            total_so_far=len(all_hits),
        )

        if page >= nb_pages - 1:
            break
        page += 1

    # Variant-aware deduplication:
    # - Algolia returns one "grouped" hit (the family page) + N "simple" hits (each variant SKU)
    # - Simple hits have /catalog/product/view/id/... URLs that 404; use family_url instead
    # - Grouped hits often have real images; simple hits use placeholder URLs
    # Strategy:
    #   1. For any family that has simple hits → emit one row per variant, URL = family_url#sku
    #   2. For families with only a grouped hit → emit one row with URL = family_url
    #   3. Inject the grouped hit's image into simple hits (simples have placeholder images)

    grouped_by_family: dict[str, dict] = {}   # family_url → grouped hit
    simples_by_family: dict[str, list[dict]] = {}  # family_url → [simple hits]

    for hit in all_hits:
        family_url = hit.get("family_url") or hit.get("url")
        if not family_url:
            continue
        if hit.get("type_id") == "grouped":
            grouped_by_family[family_url] = hit
        else:
            simples_by_family.setdefault(family_url, []).append(hit)

    deduped: list[dict] = []

    # Emit all families — prefer simple variants when available
    all_families = set(grouped_by_family) | set(simples_by_family)
    for family_url in all_families:
        grouped = grouped_by_family.get(family_url)
        simples = simples_by_family.get(family_url, [])

        if simples:
            # One row per variant; borrow images from the grouped hit if available
            grouped_images = []
            if grouped:
                img = grouped.get("image_url") or grouped.get("thumbnail_url")
                if img:
                    grouped_images = [img]
            for s in simples:
                sku = s.get("sku")
                if isinstance(sku, list):
                    sku = sku[0] if sku else None
                variant_url = f"{family_url}#{sku}" if sku else family_url
                variant_hit = {
                    **s,
                    "url": variant_url,
                    "family_url": family_url,
                }
                # Use grouped images when simple has only placeholder images
                if grouped_images and not (
                    s.get("image_url") and "placeholder" not in s.get("image_url", "")
                ):
                    variant_hit["image_url"] = grouped_images[0]
                deduped.append(variant_hit)
        elif grouped:
            # No simples — use the grouped hit as-is
            deduped.append({**grouped, "url": family_url})

    urls = [h["url"] for h in deduped]
    return urls, deduped


async def _collect_via_html(
    category_url: str,
    category_name: str,
    max_pages: int,
    delay_ms: int,
    max_concurrent: int,
) -> tuple[list[str], list[str]]:
    """HTML fallback: parse product links from rendered Playwright pages."""
    from tools import render_page_playwright, parse_html
    from urllib.parse import urljoin

    product_urls: list[str] = []
    subcategory_urls: list[str] = []
    seen_urls: set[str] = set()
    current_url = category_url

    for page_num in range(1, max_pages + 1):
        log.info("html_navigate_page", page=page_num, url=current_url)
        try:
            html = await render_page_playwright(current_url, delay_ms=delay_ms,
                                                max_concurrent=max_concurrent)
        except Exception as e:
            log.error("html_render_failed", url=current_url, error=str(e))
            break

        soup = parse_html(html)

        # Collect product URLs from script tags (embedded JSON)
        import re as _re
        for script in soup.select("script"):
            txt = script.string or ""
            for url in _re.findall(
                r'"url"\s*:\s*"(https://www\.safcodental\.com/product/[^"]+)"', txt
            ):
                if url not in seen_urls:
                    seen_urls.add(url)
                    product_urls.append(url)

        # Standard link fallback
        for a in soup.select("a.product-item-link"):
            href = a.get("href", "")
            if href and href not in seen_urls:
                seen_urls.add(href)
                product_urls.append(href)

        next_btn = soup.select_one("a.action.next")
        if not next_btn or not next_btn.get("href"):
            break
        current_url = urljoin("https://www.safcodental.com", next_btn["href"])

    return list(dict.fromkeys(product_urls)), subcategory_urls


def extract_breadcrumb(soup) -> list[str]:
    items = soup.select("ul.items.breadcrumb li span")
    return [item.get_text(strip=True) for item in items if item.get_text(strip=True)]


def normalize_algolia_hit(hit: dict, category_name: str, run_id: str) -> dict:
    """
    Convert an Algolia hit into a partial ProductRecord-compatible dict.
    Still needs description/specs/alternatives from the detail page.
    """
    price_usd = hit.get("price", {}).get("USD", {})
    price_str = price_usd.get("default_formated") or (
        f"${price_usd['default']:.2f}" if price_usd.get("default") else None
    )

    sku = hit.get("sku")
    if isinstance(sku, list):
        sku = sku[0] if sku else None

    # Build category hierarchy from Algolia's nested categories
    categories = hit.get("categories", {})
    hierarchy = []
    for level in ["level0", "level1", "level2"]:
        for cat in categories.get(level, []):
            if category_name.lower() in cat.lower() or not hierarchy:
                hierarchy = [c.strip() for c in cat.split("///")]
                break
    if not hierarchy:
        hierarchy = [category_name]

    return {
        "name": hit.get("name", ""),
        "brand": hit.get("manufacturer_name"),
        "sku": sku,
        "price": price_str,
        "availability": hit.get("stock_availability"),
        "image_urls": [
            u for u in [hit.get("image_url"), hit.get("thumbnail_url")] if u
        ],
        "category_hierarchy": hierarchy,
        # description, specifications, alternative_products filled by extractor
    }
