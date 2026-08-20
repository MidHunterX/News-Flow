"""Playwright-based browser utilities for fetching JavaScript-rendered pages."""

import asyncio

from playwright.async_api import async_playwright

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {runtime: {}};
"""


async def fetch_rendered_html(
    url: str,
    wait_selector: str | None = None,
    wait_ms: float = 5_000,
    timeout: float = 15_000,
) -> str:
    """Launch a headless browser, navigate to *url*, and return the fully
    rendered HTML after JavaScript execution.

    Parameters
    ----------
    url:
        The page to render.
    wait_selector:
        An optional CSS selector to wait for before extracting HTML.  If
        provided, the function will wait until at least one element matching
        the selector is present in the DOM.
    wait_ms:
        Time in milliseconds to wait after DOM content has loaded for
        client-side JavaScript to execute.
    timeout:
        Maximum time in milliseconds to wait for the *wait_selector*.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1920, "height": 1080},
            )
            page = await context.new_page()
            await page.add_init_script(_STEALTH_SCRIPT)
            await page.goto(url, wait_until="domcontentloaded")

            if wait_selector:
                # Give client-side JS a head start, then wait for the selector.
                await asyncio.sleep(wait_ms / 1000)
                try:
                    await page.wait_for_selector(wait_selector, timeout=timeout)
                except Exception:
                    pass  # Fall through even if selector never appears

            return await page.content()
        finally:
            await browser.close()
