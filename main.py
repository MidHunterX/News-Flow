from fastapi import FastAPI

from app.client import get_client
from app.routes import scrape

app = FastAPI(title="News Flow")

# Include routers
app.include_router(scrape.router)


@app.on_event("shutdown")
async def shutdown():
    client = await get_client()
    await client.aclose()


@app.get("/")
async def root():
    return {
        "name": "News Flow Scraper",
        "endpoints": {
            "/scrape": "Get news from all sources (optional ?source=kaumudi|mangalam)",
        },
    }
