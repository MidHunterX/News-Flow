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

Sources are scraped independently: one site being down (404 during
maintenance, a timeout) only costs that source's stories for the round. The
healthy sources are stored as usual, the down one is flagged in the UI via
:mod:`app.source_health`, and it is retried on the next round.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import SOURCES
from app.db import (
    get_refresh_interval,
    get_setting,
    record_notification,
    save_items,
    set_setting,
)
from app.db.constants import NOTIF_ERROR, REFRESH_LAST_RUN_KEY
from app.models import NewsItem
from app.source_health import is_down

logger = logging.getLogger(__name__)


async def refresh_sources(sources: list[str] | None = None) -> list[NewsItem]:
    """Re-scrape the given sources (default: all), replace their cached rows.

    Returns the articles actually stored — freshly scraped ones minus
    duplicates already in today's workspace or yesterday's archive — so
    callers report new arrivals, not scrape totals.

    Sources are scraped concurrently and independently: a source that fails
    is logged (and notified the first time it goes down), then skipped — it
    never stops the other sources' stories from being stored, and it is
    retried on the next round.
    """
    from app.scrapers.init import scrape_source  # lazy: tests patch this

    names = sources if sources is not None else list(SOURCES)
    # Snapshot before scraping: a source already known to be down has
    # already told the user, so a repeated failure stays quiet in the log.
    was_down = {name: is_down(name) for name in names}

    results = await asyncio.gather(
        *(scrape_source(name) for name in names), return_exceptions=True
    )

    items: list[NewsItem] = []
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            logger.warning(
                "Scrape failed for %s: %s: %s",
                name,
                type(result).__name__,
                result,
            )
            if not was_down[name]:
                await record_notification(
                    NOTIF_ERROR,
                    name,
                    f"Source unavailable ({type(result).__name__}): "
                    f"{result or 'no details'}. Other sources keep "
                    "scraping; this one is retried automatically.",
                )
            continue
        items.extend(result)

    stored = await save_items(items) if items else []
    await set_setting(REFRESH_LAST_RUN_KEY, now_iso())
    return stored


async def run_due_refresh() -> int:
    """Run one background refresh round when the interval has elapsed.

    Returns the number of articles newly stored by the round (0 most ticks,
    and 0 when every scraped story was already known). Never raises: an
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
