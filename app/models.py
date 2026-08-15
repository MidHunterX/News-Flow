from pydantic import AnyUrl, BaseModel, HttpUrl


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


class ScrapeResponse(BaseModel):
    sources: list[str]
    count: int
    items: list[NewsItem]
