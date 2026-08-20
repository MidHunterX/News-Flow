from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import SOURCES
from app.models import NewsItem
from app.utils import clean_text, resolve_image_url

from .base import BaseScraper


class MangalamScraper(BaseScraper):
    @property
    def source_name(self) -> str:
        return "mangalam"

    async def scrape_article_page(self, html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        img = soup.select_one("figure.news-image-block img")
        if not img:
            return None
        src = img.get("src")
        return resolve_image_url(str(src) if src else None, SOURCES[self.source_name])

    async def scrape(self, html: str) -> list[NewsItem]:
        soup = BeautifulSoup(html, "html.parser")
        base_url = SOURCES[self.source_name]
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
                    source=self.source_name,
                )
            )
        return items
