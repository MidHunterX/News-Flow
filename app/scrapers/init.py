from app.models import NewsItem

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
    from fastapi import HTTPException

    from app.config import SOURCES
    from app.utils import fetch_html

    if name not in SOURCES:
        raise HTTPException(404, f"Unknown source: {name}")

    html = await fetch_html(SOURCES[name])
    scraper = SCRAPERS[name]
    return await scraper.scrape(html)
