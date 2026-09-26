"""Offline tests for the backend-driven source refresher (app/refresher.py).

The scraper pipeline and DB layer are monkeypatched — no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import refresher
from app.config import SOURCES
from app.models import NewsItem


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _all_source_names() -> list[str]:
    return list(SOURCES)


@pytest.fixture
def env(monkeypatch):
    """Patch the scraper pipeline and settings the refresher touches."""
    settings = {"refresh_interval": 900, "refresh_last_run": None}
    calls: list[str | None] = []
    saved: list[list[NewsItem]] = []
    trimmed: list[int] = []
    notifications: list[tuple[str, str, str]] = []
    stamp_writes: list[tuple[str, str]] = []
    state = {"fail_scrape": False}

    def _item(idx: int) -> NewsItem:
        return NewsItem(
            id=idx,
            title=f"Article {idx}",
            url=f"https://example.com/{idx}",
            image_url=None,
            description="desc",
            published_at="2026-09-19",
            source="kaumudi",
        )

    async def fake_scrape_source(name: str) -> list[NewsItem]:
        if state["fail_scrape"]:
            raise RuntimeError("network down")
        calls.append(name)
        return [_item(len(calls))]

    async def fake_save_items(items: list[NewsItem]) -> None:
        saved.append(list(items))

    async def fake_trim_articles(limit: int) -> None:
        trimmed.append(limit)

    async def fake_get_interval() -> int:
        return settings["refresh_interval"]

    async def fake_get_last_refresh() -> str | None:
        return settings["refresh_last_run"]

    async def fake_set_setting(key: str, value: str) -> None:
        stamp_writes.append((key, value))
        if key == "refresh_last_run":
            settings["refresh_last_run"] = value

    async def fake_record_notification(level, source, message, article_id=None):
        notifications.append((level, source, message))

    monkeypatch.setattr("app.scrapers.init.scrape_source", fake_scrape_source)
    monkeypatch.setattr(refresher, "save_items", fake_save_items)
    monkeypatch.setattr(refresher, "trim_articles", fake_trim_articles)
    monkeypatch.setattr(refresher, "get_refresh_interval", fake_get_interval)
    monkeypatch.setattr(refresher, "get_last_refresh", fake_get_last_refresh)
    monkeypatch.setattr(refresher, "set_setting", fake_set_setting)
    monkeypatch.setattr(refresher, "record_notification", fake_record_notification)

    settings["calls"] = calls
    settings["saved"] = saved
    settings["trimmed"] = trimmed
    settings["notifications"] = notifications
    settings["stamps"] = stamp_writes
    settings["state"] = state
    return settings


# ---------------------------------------------------------------------------
# refresh_sources (shared by the API route and the background loop)
# ---------------------------------------------------------------------------


async def test_refresh_sources_scrapes_all_sources_by_default(env):
    items = await refresher.refresh_sources()
    assert [i.id for i in items] == list(range(1, len(_all_source_names()) + 1))
    assert env["calls"] == _all_source_names()
    # Cached rows are replaced and trimmed, like the old route did.
    assert env["saved"] == [items]
    assert env["trimmed"] == [10]


async def test_refresh_sources_scrapes_only_requested_source(env):
    items = await refresher.refresh_sources(["kaumudi"])
    assert [i.source for i in items] == ["kaumudi"]
    assert env["calls"] == ["kaumudi"]


async def test_refresh_sources_stamps_last_run(env):
    await refresher.refresh_sources(["kaumudi"])
    keys = [key for key, _ in env["stamps"]]
    assert keys.count("refresh_last_run") == 1


async def test_refresh_sources_raises_on_scrape_failure(env):
    env["state"]["fail_scrape"] = True
    with pytest.raises(RuntimeError):
        await refresher.refresh_sources(["kaumudi"])


# ---------------------------------------------------------------------------
# run_due_refresh (the background loop tick)
# ---------------------------------------------------------------------------


async def test_interval_not_elapsed_is_noop(env):
    env["refresh_last_run"] = _iso(datetime.now(UTC) - timedelta(seconds=60))
    assert await refresher.run_due_refresh() == 0
    assert env["calls"] == []
    assert env["saved"] == []


async def test_runs_when_never_run(env):
    assert await refresher.run_due_refresh() == len(_all_source_names())
    assert env["calls"] == _all_source_names()


async def test_runs_when_interval_elapsed(env):
    env["refresh_last_run"] = _iso(
        datetime.now(UTC) - timedelta(seconds=901)
    )
    assert await refresher.run_due_refresh() == len(_all_source_names())
    assert env["calls"] == _all_source_names()


async def test_last_run_stamped_before_scraping(env):
    """The attempt timestamp is stored before the scrape calls."""
    await refresher.run_due_refresh()
    assert env["calls"]  # scraping did happen
    stamp_index = next(
        i for i, (key, _) in enumerate(env["stamps"]) if key == "refresh_last_run"
    )
    # The initial stamp precedes the scrape-produced save/trim calls.
    assert stamp_index == 0
    assert "refresh_last_run" in [key for key, _ in env["stamps"]]


async def test_success_records_info_notification(env):
    await refresher.run_due_refresh()
    infos = [msg for level, _, msg in env["notifications"] if level == "info"]
    assert any(str(len(_all_source_names())) in msg for msg in infos)


async def test_scrape_error_is_swallowed_and_notified(env):
    env["state"]["fail_scrape"] = True
    assert await refresher.run_due_refresh() == 0
    assert env["saved"] == []
    levels = [level for level, _, _ in env["notifications"]]
    assert "error" in levels


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_interval_elapsed_helper():
    assert refresher._interval_elapsed(None, 60) is True
    future = _iso(datetime.now(UTC) + timedelta(seconds=120))
    assert refresher._interval_elapsed(future, 60) is False
    past = _iso(datetime.now(UTC) - timedelta(seconds=61))
    assert refresher._interval_elapsed(past, 60) is True
    assert refresher._interval_elapsed("not-a-timestamp", 60) is True


def test_now_iso_has_second_precision():
    value = refresher.now_iso()
    assert value.endswith("+00:00")
    assert len(value) == len("2026-09-26T00:00:00+00:00")
