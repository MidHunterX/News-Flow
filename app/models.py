from pydantic import AnyUrl, BaseModel, HttpUrl


class ScrapedArticleContent(BaseModel):
    """Content scraped from a single article page."""
    heading: str
    content: str
    cover_path: str | None = None


class NewsItem(BaseModel):
    title: str
    url: str | None
    image_url: str | None
    description: str
    published_at: str
    source: str
    # Database bookkeeping (None until loaded from the DB).
    id: int | None = None
    status: str | None = None
    accepted_at: str | None = None
    cover_file: str | None = None
    # WordPress category IDs chosen by Gemini before publishing (JSON column;
    # None until the publisher marks them).
    wp_category_ids: list[int] | None = None


class ScrapeResponse(BaseModel):
    sources: list[str]
    count: int
    items: list[NewsItem]
