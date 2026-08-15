import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import SOURCES
from app.models import ScrapeResponse
from app.scrapers.init import SCRAPERS, scrape_source

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/api/scrape", response_model=ScrapeResponse)
async def get_news_api(
    source: str | None = Query(None, description="Filter by source")
):
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


@router.get("/", response_class=HTMLResponse)
async def get_news_ui(
    request: Request, source: str | None = Query(None, description="Filter by source")
):
    current_source = source if source in SOURCES else None

    if current_source:
        items = await scrape_source(current_source)
        sources_list = [current_source]
    else:
        tasks = [scrape_source(name) for name in SOURCES]
        results = await asyncio.gather(*tasks)
        items = [item for sublist in results for item in sublist]
        sources_list = list(SOURCES)

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
