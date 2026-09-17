from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import SOURCES
from app.models import NewsItem, ScrapedArticleContent
from app.utils import clean_text, resolve_image_url

from .base import BaseScraper


class MadhyamamScraper(BaseScraper):
    # Fallback base URL for when the source is disabled in app.config.SOURCES.
    BASE_URL = "https://www.madhyamam.com"

    @property
    def source_name(self) -> str:
        return "madhyamam"

    async def scrape_article_page(self, html: str) -> ScrapedArticleContent | None:
        return None

    async def scrape(self, html: str) -> list[NewsItem]:
        soup = BeautifulSoup(html, "html.parser")
        base_url = SOURCES.get(self.source_name, self.BASE_URL)
        items = []

        for card in soup.select("div.other-news div.exclude-news-item"):
            link_el = card.select_one("div.heading h3.hd a")
            img_el = card.select_one("div.story-image img")
            title_el = card.select_one("div.heading h3.hd")
            desc_el = card.select_one("div.description p")
            time_el = card.select_one("div.post-time span")

            href = link_el.get("href") if link_el else None
            src = img_el.get("data-src") or img_el.get("src") if img_el else None

            items.append(
                NewsItem(
                    title=clean_text(title_el.get_text()) if title_el else "",
                    url=urljoin(base_url, str(href)) if href else None,
                    image_url=resolve_image_url(str(src) if img_el else None, base_url),
                    description=clean_text(desc_el.get_text()) if desc_el else "",
                    published_at=clean_text(time_el.get_text()) if time_el else "",
                    source=self.source_name,
                )
            )
        return items
