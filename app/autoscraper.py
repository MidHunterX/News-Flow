"""Background auto-scrape: fill the workspace for articles never scraped.

When the ``auto_scrape`` toggle is on, the background loop in ``main.py``
calls :func:`run_due_auto_scrape` on every tick. Once
``auto_scrape_interval`` seconds have passed since the previous scrape, the
*oldest* article in the curation workspace that has no local content file
(``public/articles/<id>.txt``) has its page scraped through the very same
:func:`app.curator.enrich_accepted_article` the accept flow uses — cover
downloaded, heading + body saved — without touching the article's status.

One article per gap: the request to the source site is spaced out from the
next one by the interval, so a fresh backlog is drained gently instead of in
a burst. Every article counts as a target regardless of status (pending,
accepted, completed or rejected) — "no local copy" is the only test.

Fail-soft everywhere, mirroring ``app/refresher``: a page that cannot be
fetched or parsed is recorded once in the notification log and remembered
for the rest of the process (its content file never appears, so the
oldest-first scan would otherwise keep picking it and block every newer
article), and any unexpected error is logged and notified rather than
killing the caller's loop. The attempt timestamp is stored *before* the
scrape so a slow page can't put the loop into a hot retry.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.curator import enrich_accepted_article
from app.db import (
    get_auto_scrape_interval,
    get_items,
    get_toggle,
    record_notification,
    set_setting,
)
from app.db.constants import (
    AUTO_SCRAPE_LAST_RUN_KEY,
    NOTIF_ERROR,
    NOTIF_WARNING,
    now_iso,
)
from app.models import NewsItem
from app.utils import ARTICLES_DIR

logger = logging.getLogger(__name__)

# Article ids whose page failed to scrape during this process. Such a page
# has no content file, so the oldest-first scan would return it again on
# every tick and block the queue — remember the failure instead of retrying
# it forever (a restart gives it one fresh chance).
_FAILED: set[int] = set()


def has_local_content(article_id: int | None) -> bool:
    """True when a scraped body for this article already sits on disk."""
    if article_id is None:
        return True  # Nothing to scrape for an id-less row.
    return (ARTICLES_DIR / f"{article_id}.txt").exists()


async def _next_unscraped() -> NewsItem | None:
    """Oldest curation article without a local content file (None when done).

    ``get_items`` returns newest-first, so walking it backwards yields the
    oldest article first — the earliest stories get their content first.
    Articles already known to fail this run are skipped.
    """
    items = await get_items()
    for item in reversed(items):
        if item.id in _FAILED or has_local_content(item.id):
            continue
        return item
    return None


async def get_last_scrape() -> str | None:
    """UTC ISO timestamp of the last auto-scrape attempt, or None if never."""
    from app.db import get_setting  # lazy: tests patch this

    raw = await get_setting(AUTO_SCRAPE_LAST_RUN_KEY, "")
    return raw or None


def _interval_elapsed(last_run: str | None, interval_seconds: int) -> bool:
    """True when *interval_seconds* have passed since the last scrape (or
    there never was one)."""
    if last_run is None:
        return True
    try:
        last_ts = datetime.fromisoformat(last_run).timestamp()
    except (TypeError, ValueError):
        return True
    now_ts = datetime.now(UTC).timestamp()
    return now_ts - last_ts >= interval_seconds


async def run_due_auto_scrape() -> int:
    """Scrape one not-yet-scraped article when the gap has elapsed.

    Returns 1 when an article was scraped, 0 otherwise (toggle off, gap not
    elapsed, nothing left to scrape, or a scrape failure). Never raises: an
    unexpected error is logged, recorded in the notification log, and the
    tick is skipped so the caller's loop survives.
    """
    try:
        if not await get_toggle("auto_scrape"):
            return 0
        interval = await get_auto_scrape_interval()
        if not _interval_elapsed(await get_last_scrape(), interval):
            return 0

        target = await _next_unscraped()
        if target is None:
            # Everything has its content: no stamp, so a newly arrived
            # article is picked up on the next tick.
            return 0

        # Stamp the attempt before scraping so a slow or hanging page can't
        # wedge the loop into fetching back-to-back.
        await set_setting(AUTO_SCRAPE_LAST_RUN_KEY, now_iso())

        if await enrich_accepted_article(target):
            return 1

        _FAILED.add(target.id)  # _next_unscraped never returns a None id
        await record_notification(
            NOTIF_WARNING,
            "auto_scrape",
            f"Auto scrape could not fetch article {target.id}; "
            "skipping it for now",
            article_id=target.id,
        )
        return 0
    except Exception as exc:
        # Never let an auto-scrape error kill the caller's loop.
        logger.warning("Auto-scrape tick failed: %s: %s",
                       type(exc).__name__, exc)
        await record_notification(
            NOTIF_ERROR,
            "auto_scrape",
            f"Auto scrape failed ({type(exc).__name__}): "
            f"{exc or 'no details'}",
        )
        return 0
