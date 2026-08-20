import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import MAX_ARTICLES_PER_SOURCE, SOURCES
from app.db import (DEFAULT_SETTINGS, STATUS_ACCEPTED, STATUS_REJECTED,
                    complete_due_articles, get_accepted_items,
                    get_all_settings, get_article_by_id, get_cached_sources,
                    get_completed_items, get_completion_interval, get_items,
                    get_pending_items, get_rejected_items, save_items,
                    set_article_status, set_setting, trim_articles)
from app.models import NewsItem, ScrapeResponse
from app.scrapers.init import SCRAPERS, scrape_source
from app.utils import download_image, fetch_html

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
    await trim_articles(MAX_ARTICLES_PER_SOURCE)
    return ScrapeResponse(sources=sources, count=len(items), items=items)


@router.post("/api/articles/{article_id}/accept")
async def accept_article(article_id: int):
    """Mark an article as accepted, starting its completion countdown.

    Also scrapes the article page to download the cover image.
    """
    article = await get_article_by_id(article_id)
    if article is None:
        raise HTTPException(404, "Article not found")

    if not await set_article_status(article_id, STATUS_ACCEPTED):
        raise HTTPException(404, "Article not found")

    # Scrape the article page for cover image and download it.
    if article.url and article.source in SCRAPERS:
        try:
            html = await fetch_html(article.url)
            cover_url = await SCRAPERS[article.source].scrape_article_page(html)
            if cover_url:
                await download_image(cover_url)
        except Exception:
            pass  # Don't fail the accept if scraping fails

    return {"ok": True}


@router.post("/api/articles/{article_id}/reject")
async def reject_article(article_id: int):
    """Mark an article as rejected, removing it from the pending list."""
    if not await set_article_status(article_id, STATUS_REJECTED):
        raise HTTPException(404, "Article not found")
    return {"ok": True}


@router.post("/api/articles/{article_id}/clear")
async def clear_article_status(article_id: int):
    """Clear the status flag, moving the article back to the pending list."""
    if not await set_article_status(article_id, None):
        raise HTTPException(404, "Article not found")
    return {"ok": True}


@router.get("/api/settings")
async def get_settings():
    return await get_all_settings()


@router.post("/api/settings")
async def update_settings(settings: dict[str, str]):
    """Update user-overridable app settings (only known keys are accepted)."""
    for key, value in settings.items():
        if key not in DEFAULT_SETTINGS:
            raise HTTPException(400, f"Unknown setting: {key}")
        if key == "completion_interval":
            try:
                if int(value) < 1:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    400, "completion_interval must be a positive number of seconds"
                )
        await set_setting(key, value)
    return await get_all_settings()


@router.get("/", response_class=HTMLResponse)
async def get_news_ui(
    request: Request, source: str | None = Query(None, description="Filter by source")
):
    current_source = source if source in SOURCES else None
    sources = [current_source] if current_source else list(SOURCES)

    await _get_articles(sources)

    # Lazily mark any accepted articles whose completion interval has elapsed.
    interval = await get_completion_interval()
    await complete_due_articles(interval)

    items = await get_pending_items(current_source)
    accepted_items = await get_accepted_items(current_source)
    completed_items = await get_completed_items(current_source)
    rejected_items = await get_rejected_items(current_source)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "items": items,
            "accepted_items": accepted_items,
            "completed_items": completed_items,
            "rejected_items": rejected_items,
            "sources": SOURCES.keys(),
            "selected_source": current_source,
            "count": len(items),
            "completion_interval": interval,
        },
    )
