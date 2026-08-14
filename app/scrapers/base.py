from abc import ABC, abstractmethod

from app.models import NewsItem


class BaseScraper(ABC):
    @abstractmethod
    async def scrape(self, html: str) -> list[NewsItem]:
        """Scrape news items from HTML content"""
        pass

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Return the source name identifier"""
        pass
