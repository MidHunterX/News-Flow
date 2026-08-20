from abc import ABC, abstractmethod

from app.models import NewsItem, ScrapedArticleContent


class BaseScraper(ABC):
    @abstractmethod
    async def scrape(self, html: str) -> list[NewsItem]:
        """Scrape news items from HTML content"""
        pass

    @abstractmethod
    async def scrape_article_page(self, html: str) -> ScrapedArticleContent | None:
        """Scrape a single article page and return heading, content, and cover image URL."""
        pass

    @property
    def needs_browser(self) -> bool:
        """Return True if the article page requires a headless browser to render."""
        return False

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Return the source name identifier"""
        pass
