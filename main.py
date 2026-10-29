import asyncio
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.autoscraper import run_due_auto_scrape
from app.client import get_client
from app.curator import run_due_selection
from app.db import get_completion_interval, init_db
from app.publisher import publish_due_articles
from app.refresher import run_due_refresh
from app.routes import notifications, scrape
from app.scrapers.init import SCRAPERS
from app.utils import COVERS_DIR, PUBLICATION_COVERS_DIR
from app.wordpress import ensure_terms, sync_terms_if_stale

app = FastAPI(title="News Flow")

logger = logging.getLogger(__name__)

# Include routers
app.include_router(scrape.router)
app.include_router(notifications.router)

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve downloaded cover images.
COVERS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/covers", StaticFiles(directory=str(COVERS_DIR)), name="covers")

# Serve the archived covers of the publication workspace (previous day) too.
PUBLICATION_COVERS_DIR.mkdir(parents=True, exist_ok=True)
app.mount(
    "/publication/covers",
    StaticFiles(directory=str(PUBLICATION_COVERS_DIR)),
    name="publication_covers",
)

_completion_task: asyncio.Task | None = None


async def _completion_loop() -> None:
    """Publish and complete accepted articles once their interval elapses.

    Each due article is pushed to WordPress (when configured) right before
    being marked completed. The WordPress terms sync is refreshed here on
    its TTL so Gemini always categorizes against a current term list. The AI
    Publish curator also ticks here, prompting Gemini to pick pending
    articles once its (user-settable) interval has elapsed, every
    source is re-scraped once its (user-settable) refresh interval elapses,
    and — while the auto_scrape toggle is on — one not-yet-scraped article
    is scraped every auto_scrape_interval seconds.
    """
    while True:
        try:
            interval = await get_completion_interval()
            await publish_due_articles(interval)
            await run_due_refresh()
            await run_due_selection()
            await run_due_auto_scrape()
            await sync_terms_if_stale()
        except Exception:
            # Keep the loop alive on transient errors (e.g. locked DB).
            pass
        await asyncio.sleep(2)


@app.on_event("startup")
async def startup():
    init_db()
    # Sync WordPress categories/tags at startup (TTL-refreshed in the loop)
    # so articles can be auto-categorized before publishing.
    try:
        await ensure_terms()
    except Exception:
        # Never block startup on WordPress being unreachable.
        logger.exception("Startup WordPress terms sync failed")
    global _completion_task
    _completion_task = asyncio.create_task(_completion_loop())


@app.on_event("shutdown")
async def shutdown():
    if _completion_task is not None:
        _completion_task.cancel()
    client = await get_client()
    await client.aclose()


@app.get("/")
async def root():
    sources = "|".join([scraper for scraper in SCRAPERS])
    return {
        "name": "News Flow",
        "endpoints": {
            "/": f"Get news from all sources (optional ?source={sources})",
            "/api/scrape": f"Get JSON from all sources (optional ?source={sources})",
            "/api/refresh": "Re-scrape and refresh the cached articles (POST, optional ?source=<source>)",
        },
    }
