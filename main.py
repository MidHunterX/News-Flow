from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException

app = FastAPI(title="News Flow Scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

SOURCES = {
    "kaumudi": "https://keralakaumudi.com/latest",
    "mangalam": "https://www.mangalam.com/category/latest-news",
}

TIMEOUT = httpx.Timeout(20.0)


def fetch_html(url: str) -> str:
    """Fetch a page and return its HTML text."""
    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Failed to fetch {url}: {exc}"
        ) from exc


def clean_text(value: str | None) -> str:
    """Normalize whitespace in extracted text."""
    return " ".join((value or "").split())


def resolve_image_url(src: str | None, base_url: str) -> str | None:
    """Resolve an image src to an absolute URL, unwrapping Next.js image proxies."""
    if not src:
        return None
    # Mangalam serves images through /_next/image?url=<encoded>&w=..&q=..
    if src.startswith("/_next/image"):
        query = urlparse(src).query
        encoded = parse_qs(query).get("url")
        if encoded:
            src = unquote(encoded[0])
    return urljoin(base_url, src)


def scrape_kaumudi(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    base_url = SOURCES["kaumudi"]
    items: list[dict] = []
    for card in soup.select("div.cat-news"):
        link = card.find("a", href=True)
        img = card.select_one("div.cat-img img")
        title_el = card.select_one("div.cat-text h5")
        desc_el = card.select_one("div.cat-text > span:not(.dt-info)")
        time_el = card.select_one("div.cat-text span.dt-info")
        items.append(
            {
                "title": clean_text(title_el.get_text()) if title_el else "",
                "url": urljoin(base_url, link["href"]) if link else None,
                "image_url": resolve_image_url(
                    img.get("src") if img else None, base_url
                ),
                "description": clean_text(desc_el.get_text()) if desc_el else "",
                "published_at": clean_text(time_el.get_text()) if time_el else "",
                "source": "kaumudi",
            }
        )
    return items


def scrape_mangalam(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    base_url = SOURCES["mangalam"]
    items: list[dict] = []

    def parse_item(item: BeautifulSoup) -> dict:
        link = item.find("a", href=True)
        img = item.select_one("figure img")
        # Main news uses <h1><a>, side items use <h3><a>
        heading = (
            item.select_one("h1 a")
            or item.select_one("h3 a")
            or item.find("a", href=True)
        )
        desc_el = item.select_one("p")
        return {
            "title": clean_text(heading.get_text()) if heading else "",
            "url": urljoin(base_url, link["href"]) if link else None,
            "image_url": resolve_image_url(img.get("src") if img else None, base_url),
            "description": clean_text(desc_el.get_text()) if desc_el else "",
            "published_at": "",
            "source": "mangalam",
        }

    container = soup.select_one("div.main-news.category-main-news") or soup
    # Main story lives in category-news-left, the rest in category-news-right.
    for block in container.select(
        "div.category-news-left .news-item, div.category-news-right .news-item"
    ):
        items.append(parse_item(block))
    return items


def scrape_source(name: str) -> list[dict]:
    if name not in SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {name}")
    html = fetch_html(SOURCES[name])
    if name == "kaumudi":
        return scrape_kaumudi(html)
    return scrape_mangalam(html)


@app.get("/")
def read_root():
    return {
        "name": "News Flow Scraper",
        "endpoints": {
            "/scrape/kaumudi": "Latest news from keralakaumudi.com",
            "/scrape/mangalam": "Latest news from mangalam.com",
            "/scrape": "Latest news from all sources",
        },
    }


@app.get("/scrape/kaumudi")
def get_kaumudi():
    return {"source": "kaumudi", "items": scrape_source("kaumudi")}


@app.get("/scrape/mangalam")
def get_mangalam():
    return {"source": "mangalam", "items": scrape_source("mangalam")}


@app.get("/scrape")
def get_all():
    items: list[dict] = []
    for name in SOURCES:
        items.extend(scrape_source(name))
    return {"sources": list(SOURCES), "count": len(items), "items": items}
