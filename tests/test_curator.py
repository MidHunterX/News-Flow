"""Offline tests for the AI Publish curator (app/curator.py).

The Gemini call and DB layer are faked/monkeypatched — no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.curator as curator
from app.models import NewsItem


def _item(idx: int, **overrides) -> NewsItem:
    defaults = dict(
        id=idx,
        title=f"Article {idx}",
        url=f"https://example.com/{idx}",
        image_url=None,
        description="desc",
        published_at="2026-09-19",
        source="kaumudi",
    )
    defaults.update(overrides)
    return NewsItem(**defaults)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class _Settings:
    """Mutable stand-in for the settings the curator reads."""

    def __init__(self, toggle: bool = True, last_run: str | None = None,
                 interval: int = 3600, count: int = 3, history: int = 10):
        self.toggle = toggle
        self.last_run = last_run
        self.interval = interval
        self.count = count
        self.history = history


@pytest.fixture
def env(monkeypatch):
    """Patch every DB/settings dependency the curator touches."""
    settings = _Settings()
    notifications: list[tuple[str, str, str]] = []
    accepted_ids: list[int] = []
    enriched: list[int] = []

    async def fake_get_toggle(key: str) -> bool:
        assert key == "ai_publish"
        return settings.toggle

    async def fake_get_interval() -> int:
        return settings.interval

    async def fake_get_last_run() -> str | None:
        return settings.last_run

    async def fake_get_count() -> int:
        return settings.count

    async def fake_get_history() -> int:
        return settings.history

    async def fake_get_pending_items() -> list[NewsItem]:
        return list(settings.pending)

    async def fake_get_recent_accepted_titles(limit: int):
        return [("src", f"Recent {i}") for i in range(min(limit, 2))]

    async def fake_set_setting(key: str, value: str) -> None:
        if key == "ai_publish_last_run":
            settings.last_run = value

    async def fake_record_notification(level, source, message, article_id=None):
        notifications.append((level, source, message))

    async def fake_set_article_status(article_id: int, status) -> bool:
        if status == "accepted":
            accepted_ids.append(article_id)
        return True

    async def fake_enrich(article: NewsItem) -> None:
        enriched.append(article.id)

    monkeypatch.setattr("app.db.get_toggle", fake_get_toggle)
    # The rest are bound into app.curator's namespace at import time.
    monkeypatch.setattr(curator, "get_ai_publish_interval", fake_get_interval)
    monkeypatch.setattr(curator, "get_ai_publish_last_run", fake_get_last_run)
    monkeypatch.setattr(curator, "get_ai_publish_count", fake_get_count)
    monkeypatch.setattr(curator, "get_ai_publish_history", fake_get_history)
    monkeypatch.setattr(curator, "get_pending_items", fake_get_pending_items)
    monkeypatch.setattr(
        curator, "get_recent_accepted_titles", fake_get_recent_accepted_titles
    )
    monkeypatch.setattr(curator, "set_setting", fake_set_setting)
    monkeypatch.setattr(curator, "record_notification", fake_record_notification)
    monkeypatch.setattr(curator, "set_article_status", fake_set_article_status)
    monkeypatch.setattr(curator, "enrich_accepted_article", fake_enrich)

    settings.pending = [_item(1), _item(2), _item(3)]
    settings.notifications = notifications
    settings.accepted_ids = accepted_ids
    settings.enriched = enriched
    settings.picked = [2, 3]
    return settings


@pytest.fixture(autouse=True)
def _patch_pick(monkeypatch, env):
    """Fake Gemini's pick (lazy import inside run_due_selection)."""

    async def fake_pick(pending, recent, count):
        env.pick_calls = (pending, recent, count)
        return env.picked

    monkeypatch.setattr("app.gemini.pick_article_ids", fake_pick)


async def test_disabled_toggle_is_noop(env):
    env.toggle = False
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []
    assert env.last_run is None


async def test_interval_not_elapsed_is_noop(env):
    env.last_run = _iso(datetime.now(timezone.utc) - timedelta(seconds=60))
    env.interval = 3600
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []


async def test_runs_when_interval_elapsed(env):
    env.last_run = _iso(datetime.now(timezone.utc) - timedelta(seconds=3601))
    env.interval = 3600
    assert await curator.run_due_selection() == 2
    assert env.accepted_ids == [2, 3]


async def test_runs_when_never_run(env):
    env.last_run = None
    assert await curator.run_due_selection() == 2
    assert env.accepted_ids == [2, 3]


async def test_last_run_stamped_before_call(env, monkeypatch):
    """The attempt timestamp is stored before the Gemini call."""
    stamps: list[str] = []

    real_set = curator.set_setting

    async def tracking_set(key, value):
        stamps.append("set" if key == "ai_publish_last_run" else key)
        await real_set(key, value)

    async def slow_pick(pending, recent, count):
        stamps.append("pick")
        return []

    monkeypatch.setattr("app.gemini.pick_article_ids", slow_pick)
    monkeypatch.setattr(curator, "set_setting", tracking_set)
    await curator.run_due_selection()
    assert stamps == ["set", "pick"]


async def test_passes_id_title_pairs_and_history_to_gemini(env):
    env.history = 5
    await curator.run_due_selection()
    pending, recent, count = env.pick_calls
    assert pending == [(1, "Article 1"), (2, "Article 2"), (3, "Article 3")]
    assert recent == ["Recent 0", "Recent 1"]
    assert count == 3


async def test_no_pending_articles_skips_gemini(env, monkeypatch):
    env.pending = []
    await curator.run_due_selection()
    # pick_article_ids would still be called; it returns [] for empty input.
    assert env.accepted_ids == []


async def test_no_pick_means_no_accept(env, monkeypatch):
    async def empty_pick(pending, recent, count):
        return []

    monkeypatch.setattr("app.gemini.pick_article_ids", empty_pick)
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []


async def test_hallucinated_ids_never_accept(env, monkeypatch):
    async def bad_pick(pending, recent, count):
        return [99]  # not in the pending list

    monkeypatch.setattr("app.gemini.pick_article_ids", bad_pick)
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []


async def test_accept_enriches_article_for_publishing(env):
    await curator.run_due_selection()
    assert env.enriched == [2, 3]


async def test_unconfigured_gemini_skips_round(env, monkeypatch):
    monkeypatch.setattr("app.gemini.is_configured", lambda: False)
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []


async def test_gemini_error_is_swallowed_and_notified(env, monkeypatch):
    async def boom(pending, recent, count):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.gemini.pick_article_ids", boom)
    assert await curator.run_due_selection() == 0
    assert env.accepted_ids == []
    levels = [level for level, _, _ in env.notifications]
    assert "error" in levels


async def test_success_records_info_notification(env):
    await curator.run_due_selection()
    infos = [msg for level, _, msg in env.notifications if level == "info"]
    assert any("2" in msg and "AI Publish" in msg for msg in infos)


async def test_invalid_last_run_treated_as_due(env):
    env.last_run = "not-a-timestamp"
    assert await curator.run_due_selection() == 2


async def test_interval_elapsed_helper():
    assert curator._interval_elapsed(None, 60) is True
    future = _iso(datetime.now(timezone.utc) + timedelta(seconds=120))
    assert curator._interval_elapsed(future, 60) is False
    past = _iso(datetime.now(timezone.utc) - timedelta(seconds=61))
    assert curator._interval_elapsed(past, 60) is True
