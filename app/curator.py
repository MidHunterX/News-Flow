"""AI Publish: Gemini periodically picks pending articles to accept.

The background loop in ``main.py`` calls :func:`run_due_selection` on every
tick. When the ``ai_publish`` toggle is on and ``ai_publish_interval`` seconds
have elapsed since the last prompt:

1. pending articles are offered to Gemini as ``<id>. <heading>`` lines;
2. the ``ai_publish_history`` most recently accepted headings are included as
   duplicate-avoidance context (the same story often appears on several
   sources);
3. Gemini responds with up to ``ai_publish_count`` article IDs (JSON schema),
   which are matched back against the offered list so a hallucinated ID can
   never accept a real article;
4. the matched articles are marked accepted — exactly like the UI's accept
   button — so they flow through the same scrape/cover/publish pipeline.

Fail-soft everywhere: a 429 rate limit, a transport error, or a malformed
response merely skips the round; the next prompt goes out one interval after
the attempt. The attempt timestamp is stored before the Gemini call so a
crashed call can't put the loop into a hot retry spin.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from app.db import (get_ai_publish_count, get_ai_publish_history,
                    get_ai_publish_interval, get_ai_publish_last_run,
                    get_article_by_id, get_pending_items,
                    get_recent_accepted_titles, record_notification,
                    set_article_status)
from app.db.constants import (AI_PUBLISH_LAST_RUN_KEY, NOTIF_ERROR,
                              NOTIF_INFO, STATUS_ACCEPTED, now_iso)
from app.db.settings import set_setting
from app.models import NewsItem

logger = logging.getLogger(__name__)


async def enrich_accepted_article(article: NewsItem) -> None:
    """Scrape an accepted article's page for cover + content.

    Shared by the UI's accept endpoint and the AI Publish curator so both
    paths produce identical workspaces: the cover is downloaded to
    ``public/covers/`` (falling back to the listing thumbnail) and the
    scraped heading + body are saved to ``public/articles/<id>.txt`` for the
    article view and the WordPress publisher. Every failure is swallowed —
    acceptance must never fail because scraping did.
    """
    from app.browser import fetch_rendered_html
    from app.scrapers.init import SCRAPERS
    from app.utils import ARTICLES_DIR, download_image, fetch_html

    article_id = article.id
    if not article_id or not article.url or article.source not in SCRAPERS:
        return
    scraper = SCRAPERS[article.source]
    try:
        if scraper.needs_browser:
            html = await fetch_rendered_html(
                article.url,
                wait_selector="div.single-news-content h1",
            )
        else:
            html = await fetch_html(article.url)
        scraped = await scraper.scrape_article_page(html)
        if scraped:
            local_path = None
            if scraped.cover_path:
                local_path = await download_image(scraped.cover_path)
                if local_path is None and article.image_url:
                    # Cover download failed; fall back to the thumbnail
                    # image from the listing page.
                    local_path = await download_image(article.image_url)
            if local_path:
                # Store the web-accessible path relative to covers dir.
                cover_name = Path(local_path).name
                cover_web = f"/covers/{cover_name}"
                from app.db import update_article_cover_file

                await update_article_cover_file(article_id, cover_web)
            # Persist scraped content for the article view page.
            ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
            content_file = ARTICLES_DIR / f"{article_id}.txt"
            content_file.write_text(
                f"{scraped.heading}\n\n{scraped.content}", encoding="utf-8"
            )
    except Exception:
        pass  # Don't fail the accept if scraping fails


async def run_due_selection() -> int:
    """Run one AI Publish round when the interval has elapsed.

    Returns the number of articles accepted this round (0 most ticks). Never
    raises: an unexpected error is logged, recorded in the notification log,
    and the round is skipped.
    """
    from app.gemini import is_configured, pick_article_ids  # lazy: tests patch

    try:
        if not await get_toggle_safe():
            return 0
        interval = await get_ai_publish_interval()
        if not _interval_elapsed(await get_ai_publish_last_run(), interval):
            return 0

        # Stamp the attempt before the network call so a crashed/slow call
        # can't wedge the loop into a tight retry against the rate limit.
        await set_setting(AI_PUBLISH_LAST_RUN_KEY, now_iso())

        if not is_configured():
            # Toggle on but no API key (env changed since enabling): skip.
            return 0

        pending = await get_pending_items()
        if not pending:
            return 0

        count = await get_ai_publish_count()
        history_limit = await get_ai_publish_history()
        recent = await get_recent_accepted_titles(history_limit)

        picked = await pick_article_ids(
            [(item.id, item.title) for item in pending if item.id is not None],
            [title for _, title in recent],
            count,
        )
        if not picked:
            # Rate limit, malformed response, or the model picked nothing —
            # all handled the same: wait for the next interval.
            return 0

        accepted = await _accept_picked(pending, picked)
        if accepted:
            logger.info("AI Publish accepted articles %s", accepted)
            await record_notification(
                NOTIF_INFO,
                "ai_publish",
                f"AI Publish accepted {len(accepted)} article(s): "
                + ", ".join(str(aid) for aid in accepted),
            )
        return len(accepted)
    except Exception as exc:
        # Never let a curation error kill the caller's loop.
        logger.warning("AI Publish round failed: %s: %s",
                       type(exc).__name__, exc)
        await record_notification(
            NOTIF_ERROR,
            "ai_publish",
            f"AI Publish round failed ({type(exc).__name__}): "
            f"{exc or 'no details'}",
        )
        return 0


async def get_toggle_safe() -> bool:
    """Return the ai_publish toggle state (False when anything goes wrong)."""
    from app.db import get_toggle  # lazy: tests patch app.db.get_toggle

    try:
        return await get_toggle("ai_publish")
    except Exception:
        logger.warning("Could not read ai_publish toggle", exc_info=True)
        return False


def _interval_elapsed(last_run: str | None, interval_seconds: int) -> bool:
    """True when *interval_seconds* have passed since the last prompt (or
    there never was one)."""
    if last_run is None:
        return True
    try:
        last_ts = datetime.fromisoformat(last_run).timestamp()
    except (TypeError, ValueError):
        return True
    now_ts = datetime.now(timezone.utc).timestamp()
    return now_ts - last_ts >= interval_seconds


async def _accept_picked(pending: list[NewsItem], picked_ids: list[int]) -> list[int]:
    """Mark each picked pending article accepted; return the IDs that took.

    Articles already transitioned (accepted/rejected in the meantime) are
    skipped by the status guard inside set_article_status's flow — a picked ID
    that is no longer pending simply doesn't get accepted again.
    """
    accepted: list[int] = []
    pending_by_id = {item.id: item for item in pending if item.id is not None}
    # Preserve the pending list's (oldest-first) order in accepted_order.
    for article_id in picked_ids:
        item = pending_by_id.get(article_id)
        if item is None:
            continue
        if await set_article_status(article_id, STATUS_ACCEPTED):
            accepted.append(article_id)
            # Give the AI-picked article the same treatment as one accepted
            # from the UI: cover downloaded + content saved for publishing.
            await enrich_accepted_article(item)
    return accepted
