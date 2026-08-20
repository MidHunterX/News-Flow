import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.client import get_client
from app.db import complete_due_articles, get_completion_interval, init_db
from app.routes import scrape
from app.scrapers.init import SCRAPERS
from app.utils import ARTICLES_DIR

app = FastAPI(title="News Flow")

# Include routers
app.include_router(scrape.router)

# Serve downloaded cover images.
ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/covers", StaticFiles(directory=str(ARTICLES_DIR)), name="covers")

_completion_task: asyncio.Task | None = None


async def _completion_loop() -> None:
    """Periodically mark accepted articles as completed once their interval elapses."""
    while True:
        try:
            interval = await get_completion_interval()
            await complete_due_articles(interval)
        except Exception:
            # Keep the loop alive on transient errors (e.g. locked DB).
            pass
        await asyncio.sleep(2)


@app.on_event("startup")
async def startup():
    init_db()
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
