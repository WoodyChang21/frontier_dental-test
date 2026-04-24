from __future__ import annotations
import asyncio
import logging
from typing import Optional

import httpx
import structlog
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

log = structlog.get_logger()

_browser = None
_playwright_instance = None
_semaphore: Optional[asyncio.Semaphore] = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def get_browser(headless: bool = True):
    global _browser, _playwright_instance
    if _browser is None or not _browser.is_connected():
        from playwright.async_api import async_playwright
        _playwright_instance = await async_playwright().start()
        _browser = await _playwright_instance.chromium.launch(headless=headless)
    return _browser


async def get_semaphore(max_concurrent: int = 3) -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max_concurrent)
    return _semaphore


async def close_browser():
    global _browser, _playwright_instance
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None


async def render_page_playwright(
    url: str,
    delay_ms: int = 1500,
    timeout_ms: int = 30_000,
    headless: bool = True,
    max_concurrent: int = 3,
    wait_selector: str = ".product-item, .products-grid, .column.main, h1.page-title",
) -> str:
    sem = await get_semaphore(max_concurrent)
    browser = await get_browser(headless)

    async with sem:
        context = await browser.new_context(
            user_agent=USER_AGENT,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="load", timeout=timeout_ms)
            try:
                await page.wait_for_selector(wait_selector, timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(delay_ms / 1000)
            html = await page.content()
            log.info("playwright_rendered", url=url, html_len=len(html))
            return html
        except Exception as e:
            log.error("playwright_error", url=url, error=str(e))
            raise
        finally:
            await page.close()
            await context.close()


@retry(
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException)
    ),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=before_sleep_log(logging.getLogger(), logging.WARNING),
    reraise=True,
)
async def fetch_page_httpx(
    url: str,
    timeout: int = 30,
    delay_ms: int = 1500,
) -> str:
    await asyncio.sleep(delay_ms / 1000)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:
        response = await client.get(url)
        if response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()
        log.info("httpx_fetch", url=url, status=response.status_code)
        return response.text


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")
