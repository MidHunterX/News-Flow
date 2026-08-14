import asyncio

from fastapi import APIRouter, HTTPException, Query

from app.config import SOURCES
from app.models import ScrapeResponse
from app.scrapers.init import SCRAPERS, scrape_source

router = APIRouter()


@router.get("/scrape", response_model=ScrapeResponse)
async def get_news(source: str | None = Query(None, description="Filter by source")):
    if source:
        if source not in SOURCES:
            raise HTTPException(404, f"Unknown source: {source}")
        items = await scrape_source(source)
        return ScrapeResponse(sources=[source], count=len(items), items=items)
    else:
        tasks = [scrape_source(name) for name in SOURCES]
        results = await asyncio.gather(*tasks)
        items = [item for sublist in results for item in sublist]
        return ScrapeResponse(sources=list(SOURCES), count=len(items), items=items)
