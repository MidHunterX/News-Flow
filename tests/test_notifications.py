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
from app.db.constants import (MAX_NOTIFICATIONS, NOTIF_ERROR, NOTIF_INFO,
                              NOTIF_WARNING)
from app.db.models import Base, Notification

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
        import app.gemini as gemini

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
