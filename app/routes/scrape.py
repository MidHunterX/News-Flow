import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import SOURCES
from app.db import get_cached_sources, get_items, save_items
from app.models import NewsItem, ScrapeResponse
from app.scrapers.init import SCRAPERS, scrape_source

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _validate_sources(sources: list[str]) -> None:
    for name in sources:
        if name not in SOURCES:
            raise HTTPException(404, f"Unknown source: {name}")


async def _get_articles(sources: list[str]) -> list[NewsItem]:
    """Return cached articles, scraping (and caching) any source not yet stored."""
    cached = await get_cached_sources()
    missing = [name for name in sources if name not in cached]
    if missing:
        results = await asyncio.gather(*[scrape_source(name) for name in missing])
        fresh_items = [item for sublist in results for item in sublist]
        await save_items(fresh_items)
    return await get_items(sources[0] if len(sources) == 1 else None)


@router.get("/api/scrape", response_model=ScrapeResponse)
async def get_news_api(
    source: str | None = Query(None, description="Filter by source")
):
    if source:
        _validate_sources([source])
        sources = [source]
    else:
        sources = list(SOURCES)
    items = await _get_articles(sources)
    return ScrapeResponse(sources=sources, count=len(items), items=items)


@router.post("/api/refresh", response_model=ScrapeResponse)
async def refresh_news(
    source: str | None = Query(None, description="Filter by source")
):
    """Re-scrape the requested sources and replace their cached articles."""
    if source:
        _validate_sources([source])
        sources = [source]
    else:
        sources = list(SOURCES)
    results = await asyncio.gather(*[scrape_source(name) for name in sources])
    items = [item for sublist in results for item in sublist]
    await save_items(items)
    return ScrapeResponse(sources=sources, count=len(items), items=items)


@router.get("/", response_class=HTMLResponse)
async def get_news_ui(
    request: Request, source: str | None = Query(None, description="Filter by source")
):
    current_source = source if source in SOURCES else None

    if current_source:
        items = await _get_articles([current_source])
    else:
        items = await _get_articles(list(SOURCES))

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "items": items,
            "sources": SOURCES.keys(),
            "selected_source": current_source,
            "count": len(items),
        },
    )
