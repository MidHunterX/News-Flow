"""Offline tests for the background auto-scrape loop (app/autoscraper.py).

The enrich step and the DB layer are monkeypatched — no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import autoscraper
from app.models import NewsItem


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _item(idx: int) -> NewsItem:
    return NewsItem(
        id=idx,
        title=f"Article {idx}",
        url=f"https://example.com/{idx}",
        image_url=None,
        description="desc",
        published_at="2026-10-29",
        source="kaumudi",
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Patch every dependency the auto-scrape worker touches."""
    settings = {"toggle": True, "interval": 60, "last_run": None}
    state = {
        # get_items returns newest-first; id 1 is therefore the oldest.
        "items": [_item(3), _item(2), _item(1)],
        "fail_scrape": False,
        "error": None,
    }
    scraped: list[int] = []
    notifications: list[tuple[str, str, str]] = []
    stamps: list[tuple[str, str]] = []
    events: list[str] = []

    async def fake_get_toggle(key: str) -> bool:
        assert key == "auto_scrape"
        return settings["toggle"]

    async def fake_get_interval() -> int:
        return settings["interval"]

    async def fake_get_items() -> list[NewsItem]:
        if state["error"] is not None:
            raise state["error"]
        return list(state["items"])

    async def fake_get_setting(key: str, default: str) -> str:
        return settings["last_run"] or default

    async def fake_set_setting(key: str, value: str) -> None:
        stamps.append((key, value))
        if key == "auto_scrape_last_run":
            settings["last_run"] = value
            events.append("stamp")

    async def fake_record(level, source, message, article_id=None):
        notifications.append((level, source, message))

    async def fake_enrich(article: NewsItem) -> bool:
        events.append("scrape")
        if state["fail_scrape"]:
            return False
        scraped.append(article.id)
        # The real enrich writes the content file the scan looks for.
        (tmp_path / f"{article.id}.txt").write_text("heading\n\nbody")
        return True

    monkeypatch.setattr(autoscraper, "get_toggle", fake_get_toggle)
    monkeypatch.setattr(autoscraper, "get_auto_scrape_interval", fake_get_interval)
    monkeypatch.setattr(autoscraper, "get_items", fake_get_items)
    monkeypatch.setattr("app.db.get_setting", fake_get_setting)
    monkeypatch.setattr(autoscraper, "set_setting", fake_set_setting)
    monkeypatch.setattr(autoscraper, "record_notification", fake_record)
    monkeypatch.setattr(autoscraper, "enrich_accepted_article", fake_enrich)
    monkeypatch.setattr(autoscraper, "ARTICLES_DIR", tmp_path)
    autoscraper._FAILED.clear()

    settings["scraped"] = scraped
    settings["notifications"] = notifications
    settings["stamps"] = stamps
    settings["events"] = events
    settings["state"] = state
    settings["dir"] = tmp_path

    def _write_content(idx: int) -> None:
        (tmp_path / f"{idx}.txt").write_text("heading\n\nbody")

    settings["write_content"] = _write_content
    return settings


# ---------------------------------------------------------------------
# run_due_auto_scrape (the background loop tick)
# ---------------------------------------------------------------------


async def test_disabled_toggle_is_noop(env):
    env["toggle"] = False
    assert await autoscraper.run_due_auto_scrape() == 0
    assert env["scraped"] == []
    assert env["stamps"] == []


async def test_interval_not_elapsed_is_noop(env):
    env["last_run"] = _iso(datetime.now(UTC) - timedelta(seconds=30))
    assert await autoscraper.run_due_auto_scrape() == 0
    assert env["scraped"] == []


async def test_scrapes_one_article_and_reports_one(env):
    assert await autoscraper.run_due_auto_scrape() == 1
    assert env["scraped"] == [1]  # oldest first


async def test_scrape_is_stamped_before_the_fetch(env):
    """The attempt timestamp is written before the page is scraped."""
    await autoscraper.run_due_auto_scrape()
    assert env["events"][0] == "stamp"
    assert [key for key, _ in env["stamps"]] == ["auto_scrape_last_run"]


async def test_one_article_per_gap(env):
    """The second tick inside the gap does nothing — spacing between scrapes."""
    assert await autoscraper.run_due_auto_scrape() == 1
    assert await autoscraper.run_due_auto_scrape() == 0
    assert env["scraped"] == [1]


async def test_already_scraped_articles_are_skipped(env):
    env["write_content"](1)
    env["write_content"](2)
    assert await autoscraper.run_due_auto_scrape() == 1
    assert env["scraped"] == [3]


async def test_queue_drains_oldest_first(env):
    for expected in (1, 2, 3):
        env["last_run"] = None  # open the gap again
        assert await autoscraper.run_due_auto_scrape() == 1
        assert env["scraped"][-1] == expected
    env["last_run"] = None
    assert await autoscraper.run_due_auto_scrape() == 0  # everything scraped


async def test_nothing_left_never_stamps(env):
    for idx in (1, 2, 3):
        env["write_content"](idx)
    assert await autoscraper.run_due_auto_scrape() == 0
    assert env["stamps"] == []


async def test_failed_scrape_is_notified_and_skipped(env):
    """An unusable page is recorded once, then the queue moves on."""
    env["state"]["fail_scrape"] = True
    assert await autoscraper.run_due_auto_scrape() == 0
    assert 1 in autoscraper._FAILED
    levels = [level for level, _, _ in env["notifications"]]
    assert "warning" in levels

    # Next round (gap reopened): article 1 is skipped, article 2 is tried.
    env["state"]["fail_scrape"] = False
    env["last_run"] = None
    assert await autoscraper.run_due_auto_scrape() == 1
    assert env["scraped"] == [2]


async def test_unexpected_error_is_swallowed_and_notified(env):
    env["state"]["error"] = RuntimeError("db locked")
    assert await autoscraper.run_due_auto_scrape() == 0
    levels = [level for level, _, _ in env["notifications"]]
    assert "error" in levels


async def test_success_records_no_notification(env):
    await autoscraper.run_due_auto_scrape()
    assert env["notifications"] == []


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def test_interval_elapsed_helper():
    assert autoscraper._interval_elapsed(None, 60) is True
    future = _iso(datetime.now(UTC) + timedelta(seconds=120))
    assert autoscraper._interval_elapsed(future, 60) is False
    past = _iso(datetime.now(UTC) - timedelta(seconds=61))
    assert autoscraper._interval_elapsed(past, 60) is True
    assert autoscraper._interval_elapsed("not-a-timestamp", 60) is True


def test_has_local_content(env):
    assert autoscraper.has_local_content(None) is True  # nothing to scrape
    assert autoscraper.has_local_content(7) is False
    env["write_content"](7)
    assert autoscraper.has_local_content(7) is True
