from app.models import NewsItem
from app.source_health import record_failure, record_success

from .kaumudi import KaumudiScraper
from .madhyamam import MadhyamamScraper
from .mangalam import MangalamScraper

# Registry of all scrapers
SCRAPERS = {
    "kaumudi": KaumudiScraper(),
    "mangalam": MangalamScraper(),
    "madhyamam": MadhyamamScraper(),
}


async def scrape_source(name: str) -> list[NewsItem]:
    """Fetch and parse one source's listing page.

    Failures (network, HTTP status, unparseable page) are re-raised for the
    caller to isolate, but the outcome is also recorded in
    :mod:`app.source_health` so the UI can flag a source that is down.
    """
    from fastapi import HTTPException

    from app.config import SOURCES
    from app.utils import fetch_html

    if name not in SOURCES:
        raise HTTPException(404, f"Unknown source: {name}")

    try:
        html = await fetch_html(SOURCES[name])
        scraper = SCRAPERS[name]
        items = await scraper.scrape(html)
    except Exception as exc:
        record_failure(name, exc)
        raise
    record_success(name)
    return items
