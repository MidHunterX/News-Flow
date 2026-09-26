"""Offline (mock-driven) tests for app/scrapers.

Each source is tested against local HTML fixtures under tests/fixtures/,
so the suite never touches the network. Live smoke testing was moved to
scripts/health_check.py.

For each active source this module verifies:
1. Listing-page parsing: titles, URLs, resolved image URLs.
2. Article-page scraping: heading, content and cover image URL.
3. Degraded behavior on pages that do not match the expected structure.
4. The scrape_source pipeline (fetch + parse) with mocked HTTP.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import NewsItem
from app.scrapers.init import SCRAPERS, scrape_source
from app.utils import resolve_image_url

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Parametrization: every registered scraper is covered
# ---------------------------------------------------------------------------

ALL_SOURCES = sorted(SCRAPERS.keys())  # ["kaumudi", "madhyamam", "mangalam"]


@pytest.fixture(params=ALL_SOURCES)
def source(request) -> str:
    """Run scraping tests against every registered scraper."""
    return request.param


def _list_fixture(source: str) -> str:
    return load_fixture(f"{source}_list.html")


def _article_fixture(source: str) -> str:
    return load_fixture(f"{source}_article.html")


# ---------------------------------------------------------------------------
# Listing page tests
# ---------------------------------------------------------------------------


class TestScrapeListPage:
    async def test_parses_articles_from_listing(self, source):
        scraper = SCRAPERS[source]
        items = await scraper.scrape(_list_fixture(source))

        assert len(items) >= 2, "Expected at least two articles in the fixture"
        for idx, item in enumerate(items):
            assert item.title, f"Article {idx} has empty title"
            assert item.url, f"Article {idx} ({item.title!r}) has no URL"
            assert item.source == source

    async def test_first_item_is_the_latest(self, source):
        items = await SCRAPERS[source].scrape(_list_fixture(source))

        latest = items[0]
        assert latest.title.startswith("First"), (
            f"Expected the first fixture article first, got {latest.title!r}"
        )
        assert latest.description, "Latest article should carry a description"

    async def test_resolves_relative_urls(self, source):
        items = await SCRAPERS[source].scrape(_list_fixture(source))

        for item in items:
            assert item.url.startswith("http"), (
                f"Article URL was not absolutized: {item.url!r}"
            )

    async def test_image_url_resolution(self, source):
        """Images resolve to absolute URLs; the newest relative image is absolutized."""
        items = await SCRAPERS[source].scrape(_list_fixture(source))

        # First two fixture articles carry images, third does not.
        assert items[0].image_url is not None, "First article should have an image"
        assert items[0].image_url.startswith("http")
        assert items[2].image_url is None, "Third fixture article has no image"


# ---------------------------------------------------------------------------
# Article page tests
# ---------------------------------------------------------------------------


class TestScrapeArticlePage:
    async def test_extracts_heading_content_cover(self, source):
        if source == "madhyamam":
            pytest.xfail(
                "MadhyamamScraper.scrape_article_page is an unimplemented stub; "
                "its fixture is the TDD target"
            )
        scraped = await SCRAPERS[source].scrape_article_page(_article_fixture(source))

        assert scraped is not None, "scrape_article_page returned None for the fixture"
        assert scraped.heading, "Heading is empty"
        assert "First" in scraped.heading
        assert scraped.content, "Content is empty"
        assert scraped.cover_path is not None, "Fixture article includes a cover image"
        assert scraped.cover_path.startswith("http"), (
            f"Cover path is not an absolute URL: {scraped.cover_path!r}"
        )

    async def test_content_preserves_paragraphs(self, source):
        if source == "madhyamam":
            pytest.xfail(
                "MadhyamamScraper.scrape_article_page is an unimplemented stub; "
                "its fixture is the TDD target"
            )
        scraped = await SCRAPERS[source].scrape_article_page(_article_fixture(source))

        assert scraped is not None
        lines = [line for line in scraped.content.splitlines() if line.strip()]
        assert len(lines) >= 3, (
            f"Expected >=3 content lines, got {lines!r}"
        )

    async def test_returns_none_when_page_has_no_article(self, source):
        html = "<html><body><p>nothing here</p></body></html>"
        scraped = await SCRAPERS[source].scrape_article_page(html)
        assert scraped is None


# ---------------------------------------------------------------------------
# Degraded cases
# ---------------------------------------------------------------------------


class TestDegradedPages:
    async def test_listing_page_without_articles_yields_nothing(self, source):
        html = "<html><body><div class='wrapper'>no news today</div></body></html>"
        items = await SCRAPERS[source].scrape(html)
        assert items == []

    async def test_mangalam_skeleton_page_returns_none(self):
        """Mangalam sometimes serves a JS loading skeleton instead of content."""
        html = load_fixture("mangalam_article_skeleton.html")
        scraped = await SCRAPERS["mangalam"].scrape_article_page(html)
        assert scraped is None


# ---------------------------------------------------------------------------
# Pipeline (fetch + parse) with mocked HTTP
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    """Replaces the httpx singleton: maps URLs to fixture bodies."""

    def __init__(self, routes: dict[str, str]):
        self._routes = routes
        self.requested: list[str] = []

    async def get(self, url: str) -> FakeResponse:
        self.requested.append(url)
        if url not in self._routes:
            raise AssertionError(f"Unexpected HTTP GET in test: {url}")
        return FakeResponse(self._routes[url])


@pytest.fixture
def fake_http(monkeypatch):
    """Patch app.utils.fetch_html to serve fixtures instead of the network."""
    from app import utils as app_utils

    def _install(routes: dict[str, str]) -> FakeClient:
        client = FakeClient(routes)

        async def fake_fetch_html(url: str) -> str:
            return (await client.get(url)).text

        monkeypatch.setattr(app_utils, "fetch_html", fake_fetch_html)
        return client

    return _install


class TestScrapeSourcePipeline:
    @pytest.mark.asyncio
    async def test_scrape_source_fetches_and_parses(self, fake_http):
        from app.config import SOURCES

        source = "kaumudi"
        list_url = SOURCES[source]
        client = fake_http({list_url: _list_fixture(source)})

        items = await scrape_source(source)

        assert client.requested == [list_url]
        assert len(items) >= 2
        assert all(isinstance(item, NewsItem) for item in items)
        assert all(item.source == source for item in items)

    @pytest.mark.asyncio
    async def test_scrape_source_unknown_source_raises(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            await scrape_source("does-not-exist")
        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# URL / image helpers
# ---------------------------------------------------------------------------


class TestImageResolution:
    def test_next_image_proxy_is_unwrapped(self):
        url = resolve_image_url(
            "/_next/image?url=https%3A%2F%2Fimages.mangalam.com%2Fa.jpg&w=640&q=75",
            "https://www.mangalam.com",
        )
        assert url == "https://images.mangalam.com/a.jpg"

    def test_relative_image_is_joined_to_origin(self):
        url = resolve_image_url("/img/x.jpg", "https://keralakaumudi.com/latest")
        assert url == "https://keralakaumudi.com/img/x.jpg"

    def test_none_and_empty_are_none(self):
        assert resolve_image_url(None, "https://example.com") is None
        assert resolve_image_url("", "https://example.com") is None


# ---------------------------------------------------------------------------
# Cover image download with mocked HTTP
# ---------------------------------------------------------------------------


class TestCoverImageDownload:
    @pytest.mark.asyncio
    async def test_download_image_writes_file(self, tmp_path, monkeypatch):
        from app import utils as app_utils

        cover_bytes = b"\x89PNG\r\n\x1a\nfake-image-data"
        png_url = "https://images.example.com/cover.png"

        # download_image reads resp.content (bytes) after raise_for_status().
        class BytesResponse:
            def __init__(self, content: bytes):
                self.content = content

            def raise_for_status(self) -> None:
                return None

        class FakeBytesClient:
            async def get(self, url: str) -> BytesResponse:
                return BytesResponse(cover_bytes)

        async def fake_get_client():
            return FakeBytesClient()

        monkeypatch.setattr(app_utils, "COVERS_DIR", tmp_path)
        monkeypatch.setattr("app.client.get_client", fake_get_client)

        local_path = await app_utils.download_image(png_url)

        assert local_path is not None
        saved = Path(local_path)
        assert saved.exists()
        assert saved.read_bytes() == cover_bytes
        assert saved.name == "cover.png"
