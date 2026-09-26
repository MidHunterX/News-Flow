"""Offline tests for the notification log (app/db/notifications.py).

Binds the notifications data layer to a throwaway SQLite database so the
real newsflow.db is never touched (no network).
"""

from __future__ import annotations

import logging

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.notifications as notifications_mod
import app.publisher as publisher_mod
import app.wordpress as wordpress_mod
from app.db.models import Base
from app.models import NewsItem


async def _async_none() -> None:
    return None


class FakeResponse:
    def __init__(self, json_data=None, status_code: int = 200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json
from app.db.constants import MAX_NOTIFICATIONS, NOTIF_ERROR, NOTIF_INFO, NOTIF_WARNING

# gemini's record_notification resolves lazily via the module attribute on
# app.db.notifications, so patching that module's SessionLocal covers it.


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Bind app.db.notifications to a throwaway SQLite database."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(notifications_mod, "SessionLocal", session_factory)
    return session_factory


async def _record(level, source, message, article_id=None):
    from app.db.notifications import record_notification

    await record_notification(level, source, message, article_id)


async def _get(limit=50):
    from app.db.notifications import get_notifications

    return await get_notifications(limit)


class TestRecordAndGet:
    async def test_roundtrip_fields(self, db):
        await _record(NOTIF_WARNING, "gemini", "Categorization failed: HTTP 429",
                      article_id=12)
        rows = await _get()
        assert len(rows) == 1
        row = rows[0]
        assert row.level == NOTIF_WARNING
        assert row.source == "gemini"
        assert row.message == "Categorization failed: HTTP 429"
        assert row.article_id == 12
        assert row.created_at  # timestamped

    async def test_newest_first_ordering(self, db):
        await _record(NOTIF_INFO, "a", "first")
        await _record(NOTIF_INFO, "a", "second")
        rows = await _get()
        assert [r.message for r in rows] == ["second", "first"]

    async def test_message_truncated(self, db):
        await _record(NOTIF_INFO, "a", "x" * 5000)
        (row,) = await _get()
        assert len(row.message) <= 500
        assert row.message.endswith("…")

    async def test_long_message_stored_untuncated_tail_marker(self, db):
        """The truncation keeps the beginning, not the tail, of the message."""
        await _record(NOTIF_INFO, "a", "START" + "x" * 5000)
        (row,) = await _get()
        assert row.message.startswith("START")

    async def test_whitespace_stripped(self, db):
        await _record(NOTIF_INFO, "a", "  padded  \n")
        (row,) = await _get()
        assert row.message == "padded"


class TestTrim:
    async def test_caps_stored_rows(self, db):
        for i in range(MAX_NOTIFICATIONS + 20):
            await _record(NOTIF_INFO, "src", f"n{i}")
        rows = await _get(limit=MAX_NOTIFICATIONS)
        assert len(rows) == MAX_NOTIFICATIONS
        # Newest kept, oldest dropped.
        assert rows[0].message == f"n{MAX_NOTIFICATIONS + 19}"
        assert f"n{MAX_NOTIFICATIONS - 1}" in [r.message for r in rows]
        assert "n0" not in [r.message for r in rows]

    async def test_get_limit_is_capped(self, db):
        for i in range(10):
            await _record(NOTIF_INFO, "src", f"n{i}")
        assert len(await _get(limit=100000)) == 10  # capped to MAX_NOTIFICATIONS


class TestClear:
    async def test_clears_all_and_returns_count(self, db):
        await _record(NOTIF_ERROR, "gemini", "boom")
        await _record(NOTIF_ERROR, "gemini", "bang")
        from app.db.notifications import clear_notifications

        assert await clear_notifications() == 2
        assert await _get() == []

    async def test_clear_on_empty_is_zero(self, db):
        from app.db.notifications import clear_notifications

        assert await clear_notifications() == 0


class TestFailSoft:
    async def test_db_failure_never_raises(self, db, monkeypatch, caplog):
        """Recording a notification on a broken DB must not raise."""
        def broken(*args, **kwargs):
            raise RuntimeError("db gone")

        monkeypatch.setattr(notifications_mod, "SessionLocal", broken)
        with caplog.at_level(logging.WARNING):
            await _record(NOTIF_ERROR, "gemini", "boom")  # must not raise
        assert "Could not store notification" in caplog.text


class TestGeminiIntegration:
    """Gemini failures surface as warning notifications."""

    async def test_http_failure_records_notification(self, db, monkeypatch):
        from app import gemini

        monkeypatch.setattr(gemini, "GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(gemini, "GEMINI_MODEL", "test-model")

        class FakeClient:
            async def post(self, url, **kwargs):
                # Not a TransportError, so the retry loop lets it through.
                raise httpx.HTTPError("HTTP 500")

        async def fake_get_client():
            return FakeClient()

        monkeypatch.setattr("app.client.get_client", fake_get_client)

        # A synced category is required before the request fires.
        import app.db.wp_terms as wp_terms_mod

        class Term:
            wp_id, name, slug = 1, "Sports", "sports"

        async def fake_get_categories():
            return [Term()]

        async def fake_get_tags():
            return []

        monkeypatch.setattr(wp_terms_mod, "get_categories", fake_get_categories)
        monkeypatch.setattr(wp_terms_mod, "get_tags", fake_get_tags)

        article = gemini.NewsItem(
            id=7,
            title="T",
            url=None,
            image_url=None,
            description="d",
            published_at="2026-09-19",
            source="kaumudi",
        )
        assert await gemini.suggest_categories(article, "H", "B") == []

        rows = await _get()
        assert len(rows) == 1
        assert rows[0].level == NOTIF_WARNING
        assert rows[0].source == "gemini"
        assert rows[0].article_id == 7
        assert "Categorization failed" in rows[0].message


class TestWordPressIntegration:
    """WordPress publish/sync failures surface as notifications."""

    @pytest.fixture
    def wp_env(self, monkeypatch):
        """Pretend WordPress publishing is configured (Gemini disabled)."""
        import app.gemini as gemini_mod

        monkeypatch.setattr(publisher_mod, "WORDPRESS_URL", "https://wp.example.com")
        monkeypatch.setattr(publisher_mod, "WORDPRESS_USERNAME", "editor")
        monkeypatch.setattr(publisher_mod, "WORDPRESS_APP_PASSWORD", "pass")
        monkeypatch.setattr(publisher_mod, "WORDPRESS_CATEGORY_ID", "")
        # Isolate WordPress: categorization would otherwise add its own
        # notification when the fake response lacks Gemini-specific fields.
        monkeypatch.setattr(gemini_mod, "GEMINI_API_KEY", "")
        return monkeypatch

    def _article(self) -> NewsItem:
        return NewsItem(
            id=3,
            title="T",
            url=None,
            image_url=None,
            description="d",
            published_at="2026-09-19",
            source="kaumudi",
        )

    async def _publish_with_client(self, monkeypatch, handler):
        class FakeClient:
            async def post(self, url, **kwargs):
                return handler(url, kwargs)

        async def fake_get_client():
            return FakeClient()

        monkeypatch.setattr("app.client.get_client", fake_get_client)

    async def test_publish_failure_records_error_notification(
        self, db, wp_env, monkeypatch
    ):
        await self._publish_with_client(
            monkeypatch, lambda url, kwargs: FakeResponse(status_code=500)
        )
        assert await publisher_mod.publish_article(self._article()) is None

        rows = await _get()
        assert len(rows) == 1
        assert rows[0].level == NOTIF_ERROR
        assert rows[0].source == "wordpress"
        assert rows[0].article_id == 3
        assert "Publish failed" in rows[0].message

    async def test_media_upload_failure_records_warning(
        self, db, wp_env, monkeypatch, tmp_path
    ):
        cover = tmp_path / "cover.png"
        cover.write_bytes(b"png")
        posts_url = "https://wp.example.com/wp-json/wp/v2/posts"

        def handler(url, kwargs):
            if url.endswith("/media"):
                return FakeResponse(status_code=500)
            return FakeResponse({"id": 7, "link": "https://wp.example.com/?p=7"})

        await self._publish_with_client(monkeypatch, handler)
        article = self._article()
        article.cover_file = f"/covers/{cover.name}"
        monkeypatch.setattr(publisher_mod, "COVERS_DIR", tmp_path)
        assert await publisher_mod.publish_article(article)

        levels = {r.level for r in await _get()}
        assert levels == {NOTIF_WARNING}  # upload failed; post still went out

    async def test_terms_sync_failure_records_warning(self, db, monkeypatch):
        async def boom():
            raise httpx.ConnectError("down")

        monkeypatch.setattr(wordpress_mod, "sync_terms", boom)
        monkeypatch.setattr(
            wordpress_mod, "get_last_terms_sync", lambda: _async_none()
        )
        assert await wordpress_mod.sync_terms_if_stale() is True

        rows = await _get()
        assert len(rows) == 1
        assert rows[0].level == NOTIF_WARNING
        assert rows[0].source == "wordpress"
        assert "Terms sync failed" in rows[0].message
