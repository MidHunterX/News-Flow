from pydantic import BaseModel


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
    # Print publication: marked as published in the printed paper from the
    # /publication page (distinct from the WP pipeline's completed status).
    is_print_published: bool = False
    # Print layout: page (1-based) this article is grouped onto from the
    # publication sidebar; None until placed.
    page_number: int | None = None
    # Order inside that page (0-based); None for rows grouped before the
    # sidebar could sort a page's articles.
    page_position: int | None = None


class ScrapeResponse(BaseModel):
    sources: list[str]
    count: int
    items: list[NewsItem]
