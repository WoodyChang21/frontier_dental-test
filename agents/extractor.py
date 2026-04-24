from __future__ import annotations
import json
import re
from typing import Optional

import structlog
from bs4 import BeautifulSoup

from state import ProductRecord
from tools import parse_html

log = structlog.get_logger()

PRODUCT_SELECTORS = {
    # Safco uses a custom Hyva/Alpine.js Magento theme
    "name": "h1",
    "brand": None,  # comes from Algolia manufacturer_name
    "sku": None,    # comes from Algolia sku field
    "price": ".price-box .price, .price",
    "availability": None,  # comes from Algolia stock_availability
    "description": "h2.mb-4 + div.prose, #description + div, #description div.prose",
    "unit_pack_size": None,  # parsed from description text if needed
    "specs_table": "table",
    "images": "img[src*='catalog/product']",
    "alternatives": "a[href*='/product/']",
}

REQUIRED_FIELDS = ["name", "sku", "price"]
OPTIONAL_FIELDS = ["brand", "availability", "description", "unit_pack_size",
                   "specifications", "image_urls"]


def css_extract(
    soup: BeautifulSoup,
    url: str,
    category: str,
    category_hierarchy: list[str],
    run_id: str,
) -> dict:
    """
    Safco theme (Hyva/Alpine.js) CSS extraction.
    Focuses on fields NOT available from Algolia: description, specs, detail images.
    """
    fields: dict = {}

    # Name: plain h1
    h1 = soup.select_one("h1")
    if h1:
        name = h1.get_text(strip=True)
        # Filter out nav/header h1s by checking length
        if name and len(name) > 2:
            fields["name"] = name

    # Price
    price_el = soup.select_one(".price-box .price, .price")
    if price_el:
        price_text = price_el.get_text(strip=True)
        if "$" in price_text:
            fields["price"] = price_text

    # Description: the div.prose sibling after the Description h2
    desc_text = None
    # Primary: Safco Hyva theme renders description in div.product-description
    desc_el = soup.select_one("div.product-description")
    if desc_el:
        desc_text = desc_el.get_text(separator=" ", strip=True)
    if not desc_text:
        # Fallback: h2 sibling pattern (older theme variants)
        for h2 in soup.select("h2"):
            if "Description" in h2.get_text():
                sib = h2.find_next_sibling()
                if sib:
                    desc_text = sib.get_text(separator=" ", strip=True)
                    break
    if not desc_text:
        # Fallback: #description element minus its label
        desc_el = soup.select_one("#description")
        if desc_el:
            raw = desc_el.get_text(strip=True)
            desc_text = raw[len("Description"):].strip() if raw.startswith("Description") else raw
    if desc_text:
        fields["description"] = desc_text

    # Specifications: look for any table on the page
    specs: dict = {}
    for table in soup.select("table"):
        for row in table.select("tr"):
            cells = row.select("th, td")
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key and val and len(key) < 60:
                    specs[key] = val
    fields["specifications"] = specs

    # Images: only catalog product images
    image_urls: list[str] = []
    seen_imgs: set[str] = set()
    for img in soup.select("img[src*='catalog/product']"):
        src = img.get("src", "")
        # Normalize to full URL, skip thumbnails with tiny dimensions
        if src and src not in seen_imgs and "placeholder" not in src:
            seen_imgs.add(src)
            image_urls.append(src)
    fields["image_urls"] = image_urls

    # Alternative / related products (upsell blocks)
    seen_alts: set[str] = set()
    alt_products: list[str] = []
    for a in soup.select("a[href*='/product/']"):
        href = a.get("href", "")
        if href and href != url and href not in seen_alts:
            seen_alts.add(href)
            alt_products.append(href)
    fields["alternative_products"] = alt_products[:10]  # cap

    return fields


async def llm_extract(
    html: str,
    url: str,
    category: str,
    model: str,
    max_tokens: int = 2048,
    is_markdown: bool = False,
) -> dict:
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    if is_markdown:
        page_text = html[:6000]
    else:
        soup = parse_html(html)
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        page_text = soup.get_text(separator="\n", strip=True)[:6000]

    prompt = f"""You are extracting structured product data from a dental supply website.
URL: {url}
Category: {category}

Page content (trimmed):
---
{page_text}
---

Extract the following fields as JSON. Use null for missing fields.
Return ONLY valid JSON:
{{
  "name": "product name",
  "brand": "manufacturer/brand name or null",
  "sku": "SKU/item number or null",
  "price": "price string including $ or null",
  "unit_pack_size": "pack size / unit description or null",
  "availability": "in stock / out of stock or null",
  "description": "product description or null",
  "specifications": {{"key": "value"}},
  "image_urls": ["url1"],
  "alternative_products": ["url1"]
}}"""

    resp = await client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        match2 = re.search(r"\{.*\}", raw, re.DOTALL)
        if match2:
            return json.loads(match2.group())
        raise


def build_product_record(
    fields: dict,
    url: str,
    category: str,
    category_hierarchy: list[str],
    run_id: str,
    extraction_method: str,
) -> ProductRecord:
    return ProductRecord(
        url=url,
        url_hash=ProductRecord.make_hash(url),
        run_id=run_id,
        category=category,
        category_hierarchy=category_hierarchy,
        name=fields.get("name", ""),
        brand=fields.get("brand"),
        sku=fields.get("sku"),
        price=fields.get("price"),
        unit_pack_size=fields.get("unit_pack_size"),
        availability=fields.get("availability"),
        description=fields.get("description"),
        specifications=fields.get("specifications") or {},
        image_urls=fields.get("image_urls") or [],
        alternative_products=fields.get("alternative_products") or [],
        extraction_method=extraction_method,
    )
