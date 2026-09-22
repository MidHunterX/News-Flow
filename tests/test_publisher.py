"""Offline tests for the WordPress publisher (app/publisher.py).

HTTP is faked with the FakeClient/monkeypatch pattern from test_scrapers.py
(no network); DB-backed tests bind ``app.db.articles`` to a throwaway SQLite
engine so the real newsflow.db is never touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.publisher as publisher
from app.db.constants import STATUS_ACCEPTED, STATUS_COMPLETED, now_iso
from app.db.models import Article, Base
from app.models import NewsItem
from app.publisher import (body_to_html, is_configured, publish_article,
                           publish_due_articles, read_article_content)

WP_BASE = "https://wp.example.com"
POSTS_URL = f"{WP_BASE}/wp-json/wp/v2/posts"
MEDIA_URL = f"{WP_BASE}/wp-json/wp/v2/media"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, json_data: dict | None = None, status_code: int = 200):
        self._json = json_data or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json


class FakeClient:
    """Records every post() call; the handler decides the response."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)


@pytest.fixture
def wp_env(monkeypatch):
    """Pretend WordPress publishing is configured."""
    monkeypatch.setattr(publisher, "WORDPRESS_URL", WP_BASE)
    monkeypatch.setattr(publisher, "WORDPRESS_USERNAME", "editor")
    monkeypatch.setattr(publisher, "WORDPRESS_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr(publisher, "WORDPRESS_CATEGORY_ID", "")
    return monkeypatch


@pytest.fixture
def article_dirs(tmp_path, monkeypatch):
    """Point the publisher's content/covers dirs at a temp workspace."""
    articles_dir = tmp_path / "articles"
    covers_dir = tmp_path / "covers"
    articles_dir.mkdir()
    covers_dir.mkdir()
    monkeypatch.setattr(publisher, "ARTICLES_DIR", articles_dir)
    monkeypatch.setattr(publisher, "COVERS_DIR", covers_dir)
    return articles_dir, covers_dir


def make_article(**overrides) -> NewsItem:
    defaults = dict(
        id=1,
        title="First Article",
        url="https://example.com/a",
        image_url=None,
        description="Short description",
        published_at="2026-09-19",
        source="kaumudi",
        status=STATUS_ACCEPTED,
        cover_file=None,
    )
    defaults.update(overrides)
    return NewsItem(**defaults)


# ---------------------------------------------------------------------------
# Payload building
# ---------------------------------------------------------------------------


class TestBodyToHtml:
    def test_wraps_lines_in_paragraphs(self):
        assert body_to_html("one\ntwo") == "<p>one</p>\n<p>two</p>"

    def test_skips_blank_lines_and_escapes(self):
        html = body_to_html("first\n\n<b>&raw</b>")
        assert html == "<p>first</p>\n<p>&lt;b&gt;&amp;raw&lt;/b&gt;</p>"

    def test_empty_body_is_empty(self):
        assert body_to_html("") == ""
        assert body_to_html("\n \n") == ""


class TestReadArticleContent:
    def test_reads_saved_heading_and_body(self, article_dirs):
        articles_dir, _ = article_dirs
        (articles_dir / "1.txt").write_text("Heading\n\nPara one\n\nPara two",
                                            encoding="utf-8")
        heading, body = read_article_content(1)
        assert heading == "Heading"
        assert body == "Para one\n\nPara two"

    def test_missing_file_returns_empty(self, article_dirs):
        assert read_article_content(999) == ("", "")

    def test_none_id_returns_empty(self, article_dirs):
        assert read_article_content(None) == ("", "")


class TestPostFields:
    def test_status_is_publish(self):
        fields = publisher._post_fields(make_article(), "H", "body", None)
        assert fields["status"] == "publish"
        assert fields["title"] == "H"
        assert fields["content"] == "<p>body</p>"

    def test_title_falls_back_to_article_title(self):
        fields = publisher._post_fields(make_article(), "", "", None)
        assert fields["title"] == "First Article"
        # Description stands in when nothing was scraped.
        assert fields["content"] == "<p>Short description</p>"

    def test_featured_media_included_when_uploaded(self):
        fields = publisher._post_fields(make_article(), "H", "body", 42)
        assert fields["featured_media"] == 42

    def test_category_id_parsed_when_valid(self, wp_env):
        wp_env.setattr(publisher, "WORDPRESS_CATEGORY_ID", "5")
        fields = publisher._post_fields(make_article(), "H", "body", None)
        assert fields["categories"] == [5]

    def test_invalid_category_id_ignored(self, wp_env):
        wp_env.setattr(publisher, "WORDPRESS_CATEGORY_ID", "news")
        fields = publisher._post_fields(make_article(), "H", "body", None)
        assert "categories" not in fields


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


class TestPublishArticle:
    async def test_unconfigured_returns_none_without_http(self, monkeypatch):
        monkeypatch.setattr(publisher, "WORDPRESS_URL", "")
        monkeypatch.setattr(publisher, "WORDPRESS_USERNAME", "")
        monkeypatch.setattr(publisher, "WORDPRESS_APP_PASSWORD", "")
        assert await publish_article(make_article()) is None

    async def test_publishes_post_and_returns_link(self, wp_env, article_dirs,
                                                   monkeypatch):
        articles_dir, _ = article_dirs
        (articles_dir / "1.txt").write_text("Heading\n\nBody line", encoding="utf-8")

        def handler(url, kwargs):
            if url == POSTS_URL:
                return FakeResponse({"id": 7, "link": f"{WP_BASE}/?p=7"})
            raise AssertionError(f"Unexpected POST {url}")

        client = FakeClient(handler)

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        link = await publish_article(make_article())
        assert link == f"{WP_BASE}/?p=7"
        assert [url for url, _ in client.calls] == [POSTS_URL]

    async def test_uploads_cover_before_post(self, wp_env, article_dirs,
                                             monkeypatch):
        articles_dir, covers_dir = article_dirs
        (articles_dir / "1.txt").write_text("H\n\nB", encoding="utf-8")
        (covers_dir / "cover.png").write_bytes(b"\x89PNG fake")

        def handler(url, kwargs):
            if url == MEDIA_URL:
                return FakeResponse({"id": 42})
            if url == POSTS_URL:
                return FakeResponse({"id": 7, "link": f"{WP_BASE}/?p=7"})
            raise AssertionError(f"Unexpected POST {url}")

        client = FakeClient(handler)

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        assert await publish_article(make_article(cover_file="/covers/cover.png"))

        media_calls = [c for c in client.calls if c[0] == MEDIA_URL]
        post_calls = [c for c in client.calls if c[0] == POSTS_URL]
        assert len(media_calls) == 1 and len(post_calls) == 1
        # Cover upload happens first; the post references its media ID.
        assert client.calls[0][0] == MEDIA_URL
        filename, content, mime = media_calls[0][1]["files"]["file"]
        assert filename == "cover.png" and content == b"\x89PNG fake" and mime == "image/png"
        assert post_calls[0][1]["json"]["featured_media"] == 42

    async def test_basic_auth_header(self, wp_env, article_dirs, monkeypatch):
        def handler(url, kwargs):
            return FakeResponse({"id": 7, "link": f"{WP_BASE}/?p=7"})

        client = FakeClient(handler)

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        await publish_article(make_article())
        assert client.calls[0][1]["headers"]["Authorization"].startswith("Basic ")

    async def test_http_failure_returns_none(self, wp_env, article_dirs,
                                             monkeypatch):
        def handler(url, kwargs):
            return FakeResponse(status_code=500)

        async def fake_get_client():
            return FakeClient(handler)

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        assert await publish_article(make_article()) is None

    async def test_cover_upload_failure_still_publishes_post(self, wp_env,
                                                             article_dirs,
                                                             monkeypatch):
        articles_dir, _ = article_dirs
        (articles_dir / "1.txt").write_text("H\n\nB", encoding="utf-8")

        def handler(url, kwargs):
            if url == MEDIA_URL:
                return FakeResponse(status_code=500)
            return FakeResponse({"id": 7, "link": f"{WP_BASE}/?p=7"})

        client = FakeClient(handler)

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        article = make_article(cover_file="/covers/cover.png")  # file missing
        assert await publish_article(article) == f"{WP_BASE}/?p=7"
        # No media call was attempted for a non-existent cover file.
        assert all(url != MEDIA_URL for url, _ in client.calls)
        post_json = next(json for url, call in client.calls
                         for json in [call["json"]] if url == POSTS_URL)
        assert "featured_media" not in post_json

    async def test_missing_content_falls_back_to_description(self, wp_env,
                                                             article_dirs,
                                                             monkeypatch):
        def handler(url, kwargs):
            return FakeResponse({"id": 7, "link": f"{WP_BASE}/?p=7"})

        client = FakeClient(handler)

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        await publish_article(make_article())
        post_json = next(call["json"] for url, call in client.calls
                         if url == POSTS_URL)
        assert post_json["title"] == "First Article"
        assert post_json["content"] == "<p>Short description</p>"


# ---------------------------------------------------------------------------
# Completion orchestration
# ---------------------------------------------------------------------------


class TestPublishDueArticles:
    async def test_publishes_before_marking_completed(self, wp_env, monkeypatch):
        """Publishing happens first; only then is the article completed."""
        events: list[str] = []
        due = [make_article(id=3)]

        async def fake_get_due(interval):
            events.append(f"get_due:{interval}")
            return due

        async def fake_publish(article):
            events.append(f"publish:{article.id}")
            return f"{WP_BASE}/?p=1"

        async def fake_mark(ids):
            events.append(f"mark:{ids}")
            return len(ids)

        monkeypatch.setattr(publisher, "get_due_articles", fake_get_due)
        monkeypatch.setattr(publisher, "publish_article", fake_publish)
        monkeypatch.setattr(publisher, "mark_articles_completed", fake_mark)

        processed = await publish_due_articles(600)
        assert processed == 1
        assert events == ["get_due:600", "publish:3", "mark:[3]"]

    async def test_no_due_articles_touches_nothing(self, monkeypatch):
        async def fake_get_due(interval):
            return []

        async def fail_publish(article):
            raise AssertionError("should not publish")

        async def fail_mark(ids):
            raise AssertionError("should not mark")

        monkeypatch.setattr(publisher, "get_due_articles", fake_get_due)
        monkeypatch.setattr(publisher, "publish_article", fail_publish)
        monkeypatch.setattr(publisher, "mark_articles_completed", fail_mark)
        assert await publish_due_articles(600) == 0

    async def test_publish_failure_still_completes(self, wp_env, monkeypatch):
        """Fail-soft policy: a failed publish never blocks completion."""

        async def fake_get_due(interval):
            return [make_article(id=9)]

        async def fake_publish(article):
            return None  # publishing failed

        marked: list[list[int]] = []

        async def fake_mark(ids):
            marked.append(ids)
            return len(ids)

        monkeypatch.setattr(publisher, "get_due_articles", fake_get_due)
        monkeypatch.setattr(publisher, "publish_article", fake_publish)
        monkeypatch.setattr(publisher, "mark_articles_completed", fake_mark)

        assert await publish_due_articles(600) == 1
        assert marked == [[9]]


# ---------------------------------------------------------------------------
# DB split: get_due_articles + mark_articles_completed
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session(monkeypatch, tmp_path):
    """Bind app.db.articles to a throwaway SQLite database."""
    from app.db import articles as articles_mod

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(articles_mod, "SessionLocal", session_factory)
    return session_factory


def _insert_article(session, **overrides) -> Article:
    past = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat(
        timespec="seconds"
    )
    defaults = dict(
        title="First Article",
        url="https://example.com/a",
        image_url=None,
        description="desc",
        published_at="2026-09-19",
        source="kaumudi",
        status=STATUS_ACCEPTED,
        accepted_at=past,
        accepted_order=1,
    )
    defaults.update(overrides)
    article = Article(**defaults)
    session.add(article)
    session.commit()
    return article


class TestDueArticlesDb:
    async def test_returns_elapsed_article(self, db_session):
        with db_session() as session:
            article = _insert_article(session)
        due = await articles_mod_get_due(600)
        assert [item.id for item in due] == [article.id]
        assert due[0].status == STATUS_ACCEPTED  # not yet mutated

    async def test_timer_not_elapsed_not_returned(self, db_session):
        with db_session() as session:
            _insert_article(
                session,
                accepted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        assert await articles_mod_get_due(600) == []

    async def test_mark_articles_completed(self, db_session):
        with db_session() as session:
            article = _insert_article(session)
        count = await articles_mod_mark([article.id])
        assert count == 1
        with db_session() as session:
            row = session.get(Article, article.id)
            assert row.status == STATUS_COMPLETED
            assert row.accepted_order is None

    async def test_mark_skips_missing_or_non_accepted(self, db_session):
        with db_session() as session:
            article = _insert_article(session, status=STATUS_COMPLETED)
        # The completed row must be skipped, and unknown IDs ignored.
        assert await articles_mod_mark([article.id, 999]) == 0


async def articles_mod_get_due(interval: int):
    from app.db.articles import get_due_articles

    return await get_due_articles(interval)


async def articles_mod_mark(ids: list[int]) -> int:
    from app.db.articles import mark_articles_completed

    return await mark_articles_completed(ids)
