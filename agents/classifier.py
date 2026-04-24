from __future__ import annotations
import json
from typing import Optional

import structlog
from bs4 import BeautifulSoup

from state import PageClassification

log = structlog.get_logger()


def classify_by_url_and_css(
    url: str, soup: BeautifulSoup
) -> Optional[PageClassification]:
    has_product_title = bool(soup.select_one("h1.page-title"))
    has_sku = bool(soup.select_one("[itemprop='sku'], .product.attribute.sku"))
    has_add_to_cart = bool(
        soup.select_one("#product-addtocart-button, button[data-role='tocart']")
    )
    has_product_grid = bool(soup.select_one("ol.products.list, .products-grid"))

    if (has_product_title and has_sku) or has_add_to_cart:
        return PageClassification(
            page_type="product_detail",
            confidence=0.95,
            reasoning="Magento product detail signals: title+sku or add-to-cart button",
        )
    if has_product_grid:
        return PageClassification(
            page_type="listing",
            confidence=0.90,
            reasoning="Magento product grid found",
        )
    # URL depth heuristic: /catalog/category/subcategory/product-slug
    segments = [s for s in url.split("/") if s and s not in ("https:", "http:", "www.safcodental.com")]
    if len(segments) >= 3 and "catalog" in segments:
        catalog_idx = segments.index("catalog")
        depth = len(segments) - catalog_idx
        if depth >= 3:
            return PageClassification(
                page_type="product_detail",
                confidence=0.75,
                reasoning=f"URL depth {depth} from /catalog/ suggests product page",
            )
    return None  # Ambiguous — escalate to LLM


async def classify_with_llm(
    url: str, html: str, model: str, is_markdown: bool = False
) -> PageClassification:
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    if is_markdown:
        snippet = html[:800]
    else:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        snippet = soup.get_text(separator=" ", strip=True)[:800]

    prompt = f"""Classify this dental supply webpage.
URL: {url}
Page text snippet: {snippet}

Reply with exactly one of these page types:
- product_detail: A page showing a single product with name, price, SKU
- listing: A category page listing multiple products
- pagination: A pagination/navigation page only
- irrelevant: Login, error, or non-product page

Reply as JSON only: {{"page_type": "...", "confidence": 0.0, "reasoning": "one sentence"}}"""

    resp = await client.chat.completions.create(
        model=model,
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(match.group()) if match else {
            "page_type": "irrelevant", "confidence": 0.5, "reasoning": "parse error"
        }
    log.info("llm_classification", url=url, **data)
    return PageClassification(**data)
