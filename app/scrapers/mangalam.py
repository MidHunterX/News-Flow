from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import SOURCES
from app.models import NewsItem, ScrapedArticleContent
from app.utils import clean_text, resolve_image_url

from .base import BaseScraper


class MangalamScraper(BaseScraper):
    @property
    def source_name(self) -> str:
        return "mangalam"

    @property
    def needs_browser(self) -> bool:
        return True

    async def scrape_article_page(self, html: str) -> ScrapedArticleContent | None:
        soup = BeautifulSoup(html, "html.parser")
        container = soup.select_one("div.single-news-content") or soup
        heading_el = container.select_one("h1")
        heading = clean_text(heading_el.get_text()) if heading_el else ""

        article_el = container.select_one("div.article")
        if article_el:
            # Replace <br> tags with newlines to preserve paragraph breaks.
            for br in article_el.find_all("br"):
                br.replace_with("\n")
            lines = [line.strip() for line in article_el.get_text(separator="\n").splitlines()]
            content = "\n".join([line for line in lines if line])
        else:
            content = ""

        img = container.select_one("figure.news-image-block img")
        cover_url = None
        if img:
            src = img.get("src")
            cover_url = resolve_image_url(str(src) if src else None, SOURCES[self.source_name])

        if not heading and not content:
            return None

        return ScrapedArticleContent(heading=heading, content=content, cover_path=cover_url)

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
