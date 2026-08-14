from pydantic import AnyUrl, BaseModel, HttpUrl


class NewsItem(BaseModel):
    title: str
    url: str | None
    image_url: str | None
    description: str
    published_at: str
    source: str


class ScrapeResponse(BaseModel):
    sources: list[str]
    count: int
    items: list[NewsItem]
