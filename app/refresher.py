"""Background source refresh: re-scrapes every source every ``refresh_interval``.

Replaces the old frontend timer (``templates/base.html`` used to POST
``/api/refresh`` every 15 minutes from the browser): the refresh now runs in
the background loop in ``main.py`` via :func:`run_due_refresh`, so scraping
continues while no browser is open.

The manual trigger (``POST /api/refresh``) and the scheduled background run
share :func:`refresh_sources` so both paths trim and cache identically.

Scheduling mirrors the AI Publish curator: the attempt timestamp is stored
*before* scraping so a crashed/slow run can't wedge the loop into a tight
retry, and any failure is swallowed (recorded in the notification log) so the
caller's loop never dies.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.config import MAX_ARTICLES_PER_SOURCE, SOURCES
from app.db import (
    get_refresh_interval,
    get_setting,
    record_notification,
    save_items,
    set_setting,
    trim_articles,
)
from app.db.constants import NOTIF_ERROR, NOTIF_INFO, REFRESH_LAST_RUN_KEY
from app.models import NewsItem

logger = logging.getLogger(__name__)


async def refresh_sources(sources: list[str] | None = None) -> list[NewsItem]:
    """Re-scrape the given sources (default: all), replace their cached rows.

    Returns the freshly scraped articles. Raises on scrape failure — callers
    decide whether that is fatal (API) or logged-and-skipped (loop).
    """
    from app.scrapers.init import scrape_source  # lazy: tests patch this

    names = sources if sources is not None else list(SOURCES)
    results = [await scrape_source(name) for name in names]
    items = [item for sublist in results for item in sublist]
    await save_items(items)
    await trim_articles(MAX_ARTICLES_PER_SOURCE)
    await set_setting(REFRESH_LAST_RUN_KEY, now_iso())
    return items


async def run_due_refresh() -> int:
    """Run one background refresh round when the interval has elapsed.

    Returns the number of articles scraped (0 most ticks). Never raises: an
    unexpected error is logged, recorded in the notification log, and the
    round is skipped.
    """
    try:
        interval = await get_refresh_interval()
        if not _interval_elapsed(await get_last_refresh(), interval):
            return 0

        # Stamp the attempt before scraping so a crashed/slow run can't wedge
        # the loop into retrying back-to-back against the news sites.
        await set_setting(REFRESH_LAST_RUN_KEY, now_iso())

        count = len(await refresh_sources())
        logger.info("Background refresh scraped %d articles", count)
        await record_notification(
            NOTIF_INFO,
            "refresh",
            f"Scheduled refresh scraped {count} article(s).",
        )
        return count
    except Exception as exc:
        # Never let a refresh error kill the caller's loop.
        logger.warning("Background refresh failed: %s: %s",
                       type(exc).__name__, exc)
        await record_notification(
            NOTIF_ERROR,
            "refresh",
            f"Scheduled refresh failed ({type(exc).__name__}): "
            f"{exc or 'no details'}",
        )
        return 0


async def get_last_refresh() -> str | None:
    """UTC ISO timestamp of the last refresh attempt, or None when never run."""
    raw = await get_setting(REFRESH_LAST_RUN_KEY, "")
    return raw or None


def _interval_elapsed(last_run: str | None, interval_seconds: int) -> bool:
    """True when *interval_seconds* have passed since the last refresh (or
    there never was one)."""
    if last_run is None:
        return True
    try:
        last_ts = datetime.fromisoformat(last_run).timestamp()
    except (TypeError, ValueError):
        return True
    now_ts = datetime.now(UTC).timestamp()
    return now_ts - last_ts >= interval_seconds


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")
