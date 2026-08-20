from abc import ABC, abstractmethod

from app.models import NewsItem


class BaseScraper(ABC):
    @abstractmethod
    async def scrape(self, html: str) -> list[NewsItem]:
        """Scrape news items from HTML content"""
        pass

    @abstractmethod
    async def scrape_article_page(self, html: str) -> str | None:
        """Scrape a single article page and return the cover image URL."""
        pass

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Return the source name identifier"""
        pass
