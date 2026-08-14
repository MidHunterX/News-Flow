import asyncio
import logging
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from pydantic import AnyUrl, BaseModel, HttpUrl

# ---------- Configuration ----------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SOURCES = {
    "kaumudi": "https://keralakaumudi.com/latest",
    "mangalam": "https://www.mangalam.com/category/latest-news",
}
TIMEOUT = httpx.Timeout(20.0)


# ---------- Pydantic Models ----------
class NewsItem(BaseModel):
    title: str
    url: str | None
    image_url: str | None
    description: str
    published_at: str
    source: str


class ScrapeResponse(BaseModel):
    sources: list[str]
    count: int
    items: list[NewsItem]


# ---------- HTTP Client (reused) ----------
# Use AsyncClient for async endpoints
client = httpx.AsyncClient(
    headers={"User-Agent": USER_AGENT},
    timeout=TIMEOUT,
    follow_redirects=True,
)


# ---------- Helpers ----------
def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def resolve_image_url(src: str | None, base_url: str) -> str | None:
    if not src:
        return None
    if src.startswith("/_next/image"):
        query = urlparse(src).query
        encoded = parse_qs(query).get("url")
        if encoded:
            src = unquote(encoded[0])
    # Use the origin (scheme + netloc) for absolute joining
    base_parsed = urlparse(base_url)
    origin = f"{base_parsed.scheme}://{base_parsed.netloc}"
    return urljoin(origin, src)


async def fetch_html(url: str) -> str:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Failed to fetch {url}: {exc}") from exc


# ---------- Scrapers (async) ----------
async def scrape_kaumudi(html: str) -> list[NewsItem]:
    soup = BeautifulSoup(html, "html.parser")
    base_url = SOURCES["kaumudi"]
    items = []
    for card in soup.select("div.cat-news"):
        link = card.find("a", href=True)
        img = card.select_one("div.cat-img img")
        title_el = card.select_one("div.cat-text h5")
        desc_el = card.select_one("div.cat-text > span:not(.dt-info)")
        time_el = card.select_one("div.cat-text span.dt-info")

        href = link.get("href") if link else None
        src = img.get("src") if img else None

        items.append(
            NewsItem(
                title=clean_text(title_el.get_text()) if title_el else "",
                url=urljoin(base_url, str(href)) if link and href else None,
                image_url=resolve_image_url(str(src) if img else None, base_url),
                description=clean_text(desc_el.get_text()) if desc_el else "",
                published_at=clean_text(time_el.get_text()) if time_el else "",
                source="kaumudi",
            )
        )
    return items


async def scrape_mangalam(html: str) -> list[NewsItem]:
    soup = BeautifulSoup(html, "html.parser")
    base_url = SOURCES["mangalam"]
    items = []
    container = soup.select_one("div.main-news.category-main-news") or soup
    for block in container.select(
        "div.category-news-left .news-item, div.category-news-right .news-item"
    ):
        link = block.find("a", href=True)
        img = block.select_one("figure img")
        heading = (
            block.select_one("h1 a")
            or block.select_one("h3 a")
            or block.find("a", href=True)
        )
        desc_el = block.select_one("p")

        href = link.get("href") if link else None
        src = img.get("src") if img else None

        items.append(
            NewsItem(
                title=clean_text(heading.get_text()) if heading else "",
                url=urljoin(base_url, str(href)) if link and href else None,
                image_url=resolve_image_url(str(src) if img else None, base_url),
                description=clean_text(desc_el.get_text()) if desc_el else "",
                published_at="",
                source="mangalam",
            )
        )
    return items


SCRAPERS = {
    "kaumudi": scrape_kaumudi,
    "mangalam": scrape_mangalam,
}


async def scrape_source(name: str) -> list[NewsItem]:
    if name not in SOURCES:
        raise HTTPException(404, f"Unknown source: {name}")
    html = await fetch_html(SOURCES[name])
    scraper = SCRAPERS[name]
    return await scraper(html)


# ---------- FastAPI App ----------
app = FastAPI(title="News Flow Scraper")


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()


@app.get("/")
async def root():
    return {
        "name": "News Flow Scraper",
        "endpoints": {
            "/scrape": "Get news from all sources (optional ?source=kaumudi|mangalam)",
        },
    }


@app.get("/scrape", response_model=ScrapeResponse)
async def get_news(source: str | None = None):
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
