from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import SOURCES
from app.models import NewsItem
from app.utils import clean_text, resolve_image_url

from .base import BaseScraper


class KaumudiScraper(BaseScraper):
    @property
    def source_name(self) -> str:
        return "kaumudi"

    async def scrape(self, html: str) -> list[NewsItem]:
        soup = BeautifulSoup(html, "html.parser")
        base_url = SOURCES[self.source_name]
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
                    source=self.source_name,
                )
            )
        return items
