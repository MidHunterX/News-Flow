import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import MAX_ARTICLES_PER_SOURCE, SOURCES
from app.curator import enrich_accepted_article
from app.db import (AI_PUBLISH_LAST_RUN_KEY, ARTICLE_LAYOUTS,
                    DEFAULT_SETTINGS, STATUS_ACCEPTED, STATUS_REJECTED,
                    TOGGLE_ENV_KEYS, TOGGLE_SETTINGS, clear_notifications,
                    get_accepted_items, get_ai_publish_count,
                    get_ai_publish_history, get_ai_publish_interval,
                    get_ai_publish_last_run, get_all_settings,
                    get_all_toggle_states, get_article_by_id,
                    get_article_layout, get_cached_sources,
                    get_completed_items, get_completion_interval, get_items,
                    get_notifications, get_pending_items, get_rejected_items,
                    save_items, set_article_status, set_setting,
                    toggle_is_available, trim_articles)
from app.models import NewsItem, ScrapedArticleContent, ScrapeResponse
from app.publisher import publish_due_articles
from app.utils import ARTICLES_DIR

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Human-readable copy for the settings modal's feature toggles.
TOGGLE_META = {
    "ai_auto_categorization": {
        "label": "AI Auto Categorization",
        "description": "Gemini picks related WordPress categories for each article right before it is published.",
    },
    "ai_publish": {
        "label": "AI Publish",
        "description": "Gemini periodically picks pending articles to accept, avoiding stories already covered.",
    },
    "auto_publish": {
        "label": "Auto Publishing",
        "description": "Completed articles are published to WordPress automatically.",
    },
}


async def _toggle_context() -> dict:
    """Settings-modal feature toggles: state, availability, and copy."""
    states = await get_all_toggle_states()
    return {
        "toggles": {
            key: {
                **TOGGLE_META.get(key, {"label": key, "description": ""}),
                "enabled": enabled,
                "available": toggle_is_available(key),
                "required_env": list(TOGGLE_ENV_KEYS[key]),
            }
            for key, enabled in states.items()
        }
    }


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

    Also scrapes the article page to download the cover image, falling back
    to the listing-page thumbnail (``image_url``) when that download fails.
    """
    article = await get_article_by_id(article_id)
    if article is None:
        raise HTTPException(404, "Article not found")

    if not await set_article_status(article_id, STATUS_ACCEPTED):
        raise HTTPException(404, "Article not found")

    # Scrape the article page for cover image and content, then download.
    # Failures are swallowed — accept never fails because scraping did.
    await enrich_accepted_article(article)

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
    """All settings plus feature-toggle state and env availability."""
    settings = await get_all_settings()
    toggles = {
        key: {
            "enabled": enabled,
            "available": toggle_is_available(key),
            "required_env": list(TOGGLE_ENV_KEYS[key]),
        }
        for key, enabled in (await get_all_toggle_states()).items()
    }
    return {**settings, "toggles": toggles}


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
        elif key == "ai_publish_count":
            try:
                if int(value) < 1:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    400, "ai_publish_count must be a positive number"
                )
        elif key == "ai_publish_interval":
            try:
                if int(value) < 1:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    400, "ai_publish_interval must be a positive number of seconds"
                )
        elif key == "ai_publish_history":
            try:
                if int(value) < 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    400, "ai_publish_history must be a non-negative number"
                )
        elif key == "article_layout":
            if value not in ARTICLE_LAYOUTS:
                raise HTTPException(
                    400,
                    f"article_layout must be one of: {', '.join(ARTICLE_LAYOUTS)}",
                )
        elif key in TOGGLE_SETTINGS:
            if value not in ("0", "1"):
                raise HTTPException(400, f'{key} must be "1" or "0"')
            if value == "1" and not toggle_is_available(key):
                missing = ", ".join(TOGGLE_ENV_KEYS[key])
                raise HTTPException(
                    400,
                    f"{key} requires the following environment variables: {missing}",
                )
        await set_setting(key, value)
    return await get_all_settings()


@router.get("/api/notifications")
async def list_notifications(limit: int = Query(50, ge=1, le=100)):
    """Return the stored notification log (newest first) as JSON."""
    rows = await get_notifications(limit)
    return {
        "count": len(rows),
        "items": [
            {
                "id": row.id,
                "level": row.level,
                "source": row.source,
                "message": row.message,
                "article_id": row.article_id,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


@router.delete("/api/notifications")
async def delete_notifications():
    """Clear the entire notification log."""
    deleted = await clear_notifications()
    return {"ok": True, "deleted": deleted}


@router.get("/article/{article_id}", response_class=HTMLResponse)
async def article_view(request: Request, article_id: int):
    """Display the scraped cover, heading, and content for an accepted article."""
    article = await get_article_by_id(article_id)
    if article is None:
        raise HTTPException(404, "Article not found")

    # Read saved scraped content if available.
    scraped_heading = article.title
    scraped_content = ""
    cover_file = article.cover_file  # Stored by accept endpoint.
    content_file = ARTICLES_DIR / f"{article_id}.txt"
    if content_file.exists():
        raw = content_file.read_text(encoding="utf-8")
        parts = raw.split("\n\n", 1)
        scraped_heading = parts[0].strip() or article.title
        scraped_content = parts[1].strip() if len(parts) > 1 else ""

    return templates.TemplateResponse(
        request=request,
        name="article.html",
        context={
            "request": request,
            "article": article,
            "scraped_heading": scraped_heading,
            "scraped_content": scraped_content,
            "cover_file": cover_file,
            **await _toggle_context(),
        },
    )


@router.get("/", response_class=HTMLResponse)
async def get_news_ui(
    request: Request, source: str | None = Query(None, description="Filter by source")
):
    current_source = source if source in SOURCES else None
    sources = [current_source] if current_source else list(SOURCES)

    await _get_articles(sources)

    # Lazily publish + complete any accepted articles whose interval elapsed.
    interval = await get_completion_interval()
    await publish_due_articles(interval)

    items = await get_pending_items(current_source)
    accepted_items = await get_accepted_items(current_source)
    completed_items = await get_completed_items(current_source)
    rejected_items = await get_rejected_items(current_source)
    layout = await get_article_layout()

    toggles = await _toggle_context()
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
            "article_layout": layout,
            "ai_publish_enabled": toggles["toggles"]["ai_publish"]["enabled"],
            "ai_publish_interval": await get_ai_publish_interval(),
            "ai_publish_last_run": await get_ai_publish_last_run(),
            **toggles,
        },
    )
