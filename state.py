from __future__ import annotations
import operator
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Optional
from typing_extensions import TypedDict
from pydantic import BaseModel, Field


class ProductRecord(BaseModel):
    url: str
    url_hash: str
    run_id: str
    category: str
    category_hierarchy: list[str]
    name: str
    brand: Optional[str] = None
    sku: Optional[str] = None
    price: Optional[str] = None
    unit_pack_size: Optional[str] = None
    availability: Optional[str] = None
    description: Optional[str] = None
    specifications: dict = Field(default_factory=dict)
    image_urls: list[str] = Field(default_factory=list)
    alternative_products: list[str] = Field(default_factory=list)
    extraction_method: str = "css"
    confidence_score: float = 0.0
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

    @classmethod
    def make_hash(cls, url: str) -> str:
        return sha256(url.encode()).hexdigest()


class PageClassification(BaseModel):
    page_type: str  # "product_detail" | "listing" | "pagination" | "irrelevant"
    confidence: float
    reasoning: str


class RunConfig(TypedDict):
    run_id: str
    categories: list[dict]  # each has: url, name, algolia_filter
    output_dir: str
    max_pages: int
    max_products: int
    delay_ms: int
    max_concurrent: int
    llm_model: str
    extraction_threshold: float
    checkpoint_db: str
    db_filename: str
    csv_filename: str
    json_filename: str
    tavily_extract_depth: str
    tavily_batch_size: int


class MainState(TypedDict):
    run_config: RunConfig
    products: Annotated[list[ProductRecord], operator.add]
    errors: Annotated[list[dict], operator.add]
    run_metadata: dict


class CategoryState(TypedDict):
    category_url: str
    category_name: str
    run_config: RunConfig
    product_urls: list[str]
    algolia_hits: list[dict]          # rich pre-fetched data from Algolia
    tavily_content_map: dict          # url → {"content": str|None, "images": list[str]}
    subcategory_urls: list[str]
    current_page: int
    total_pages: int
    products: Annotated[list[ProductRecord], operator.add]
    errors: Annotated[list[dict], operator.add]


class ProductTaskState(TypedDict):
    product_url: str
    category_name: str
    category_hierarchy: list[str]
    run_config: RunConfig
    algolia_data: dict                # pre-fetched fields from Algolia (may be empty)
    prefetched_content: Optional[str] # Tavily-extracted markdown (may be None)
    prefetched_images: list[str]      # Tavily-extracted image URLs


class ProductState(TypedDict):
    product_url: str
    category_name: str
    category_hierarchy: list[str]
    run_config: RunConfig
    algolia_data: dict                # pre-fetched from Algolia (may be empty {})
    raw_html: Optional[str]           # Tavily markdown or HTML fallback
    tavily_images: list[str]          # image URLs extracted by Tavily
    page_type: Optional[str]
    product: Optional[ProductRecord]
    extraction_error: Optional[str]
