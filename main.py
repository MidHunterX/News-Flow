from fastapi import FastAPI

from app.client import get_client
from app.db import init_db
from app.routes import scrape
from app.scrapers.init import SCRAPERS

app = FastAPI(title="News Flow")

# Include routers
app.include_router(scrape.router)


@app.on_event("startup")
async def startup():
    init_db()


@app.on_event("shutdown")
async def shutdown():
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
