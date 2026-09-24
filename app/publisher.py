"""Publish completed articles to a WordPress site via the REST API.

Authentication uses a WordPress application password over HTTP Basic auth
(configured via .env — see .env.example). For each due article:

* the downloaded cover (``public/covers/``) is uploaded to
  ``/wp-json/wp/v2/media`` and attached as the post's featured image;
* the post is created at ``/wp-json/wp/v2/posts`` with status ``publish``,
  using the scraped content saved by the accept endpoint
  (``public/articles/<id>.txt``) when available.

Publishing is fail-soft: a failed request is logged and skipped, and the
article is still marked completed (mirroring how the accept endpoint never
fails because of scraping errors).
"""

from __future__ import annotations

import base64
import html
import logging
import mimetypes
from pathlib import Path

import httpx

from app.config import (WORDPRESS_APP_PASSWORD, WORDPRESS_CATEGORY_ID,
                        WORDPRESS_URL, WORDPRESS_USERNAME)
from app.db import (get_due_articles, get_toggle, mark_articles_completed)
from app.db.constants import NOTIF_ERROR, NOTIF_WARNING, STATUS_PUBLISHING
from app.db.notifications import record_notification
from app.db.wp_terms import set_article_category_ids
from app.models import NewsItem
from app.utils import ARTICLES_DIR, COVERS_DIR

logger = logging.getLogger(__name__)

API_BASE = "/wp-json/wp/v2"


def is_configured() -> bool:
    """True when all WordPress publishing credentials are present."""
    return bool(WORDPRESS_URL and WORDPRESS_USERNAME and WORDPRESS_APP_PASSWORD)


def _auth_header() -> dict[str, str]:
    """HTTP Basic auth header built from the username + application password."""
    credentials = base64.b64encode(
        f"{WORDPRESS_USERNAME}:{WORDPRESS_APP_PASSWORD}".encode()
    ).decode()
    return {"Authorization": f"Basic {credentials}"}


def _api_url(endpoint: str) -> str:
    return f"{WORDPRESS_URL}{API_BASE}{endpoint}"


def read_article_content(article_id: int | None) -> tuple[str, str]:
    """Return (heading, body) from public/articles/<id>.txt.

    Returns ("", "") when no scraped content was saved for the article.
    """
    if article_id is None:
        return "", ""
    content_file = ARTICLES_DIR / f"{article_id}.txt"
    if not content_file.exists():
        return "", ""
    heading, _, body = content_file.read_text(encoding="utf-8").partition("\n\n")
    return heading.strip(), body.strip()


def body_to_html(body: str) -> str:
    """Convert scraped plain-text lines into escaped <p> paragraphs."""
    return "\n".join(
        f"<p>{html.escape(line)}</p>" for line in body.splitlines() if line.strip()
    )


def _cover_local_path(cover_file: str | None) -> Path | None:
    """Map a stored /covers/<name> web path to its local file, if it exists."""
    if not cover_file:
        return None
    path = COVERS_DIR / Path(cover_file).name
    return path if path.is_file() else None


def _post_fields(
    article: NewsItem, heading: str, body: str, featured_media: int | None
) -> dict:
    """Build the JSON payload for POST /wp/v2/posts."""
    fields: dict = {
        "title": heading or article.title,
        "content": body_to_html(body) or f"<p>{html.escape(article.description)}</p>",
        "status": "publish",
    }
    if featured_media is not None:
        fields["featured_media"] = featured_media
    if article.wp_category_ids:
        # Chosen by Gemini for this article; overrides the site default.
        fields["categories"] = list(article.wp_category_ids)
    elif WORDPRESS_CATEGORY_ID:
        try:
            fields["categories"] = [int(WORDPRESS_CATEGORY_ID)]
        except ValueError:
            logger.warning(
                "Ignoring invalid WORDPRESS_CATEGORY_ID=%r", WORDPRESS_CATEGORY_ID
            )
    return fields


async def _upload_media(
    client: httpx.AsyncClient, local_path: Path, article_id: int | None = None
) -> int | None:
    """Upload a local image as a WordPress media attachment; return its ID."""
    mime = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    try:
        resp = await client.post(
            _api_url("/media"),
            headers=_auth_header(),
            files={"file": (local_path.name, local_path.read_bytes(), mime)},
        )
        resp.raise_for_status()
        return int(resp.json()["id"])
    except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError) as exc:
        logger.warning("WordPress media upload failed (%s): %s", local_path.name, exc)
        # The post can still go out without a featured image.
        await record_notification(
            NOTIF_WARNING,
            "wordpress",
            f"Media upload failed ({local_path.name}): {exc or 'no details'}",
            article_id=article_id,
        )
        return None


async def _mark_categories(article: NewsItem, heading: str, body: str) -> None:
    """Ask Gemini for related categories and store the IDs on the article.

    Skipped when the ``ai_auto_categorization`` setting is off. Fail-soft:
    any failure leaves the article uncategorized and logged; the post still
    goes out under the site default category.
    """
    if not await get_toggle("ai_auto_categorization"):
        return
    from app.gemini import suggest_categories  # lazy: tests patch app.gemini

    try:
        ids = await suggest_categories(article, heading, body)
    except Exception as exc:
        logger.warning("Category suggestion failed for %s: %s", article.id, exc)
        return
    if ids:
        await set_article_category_ids(article.id, ids)
        # Reflect on the in-memory item so _post_fields picks it up.
        article.wp_category_ids = ids


async def publish_article(article: NewsItem) -> str | None:
    """Create a WordPress post for *article*. Returns the post URL, or None.

    Publishing can be disabled (no credentials configured, or the
    ``auto_publish`` setting is off) or fail (HTTP error); both return None
    without raising.
    """
    if not is_configured() or not await get_toggle("auto_publish"):
        return None

    from app.client import get_client  # lazy: tests patch app.client.get_client

    client = await get_client()
    cover_path = _cover_local_path(article.cover_file)
    featured_media = (
        await _upload_media(client, cover_path, article.id) if cover_path else None
    )

    heading, body = read_article_content(article.id)
    await _mark_categories(article, heading, body)
    try:
        resp = await client.post(
            _api_url("/posts"),
            headers=_auth_header(),
            json=_post_fields(article, heading, body, featured_media),
        )
        resp.raise_for_status()
        link = resp.json().get("link")
    except Exception as exc:
        # Fail-soft: log and skip (status stays "publishing"; init_db requeues
        # it on the next startup, and the post was never created so nothing is
        # orphaned on WordPress).
        logger.warning("WordPress publish failed for article %s: %s", article.id, exc)
        await record_notification(
            NOTIF_ERROR,
            "wordpress",
            f"Publish failed: {exc or 'no details'}",
            article_id=article.id,
        )
        return None
    return str(link) if link else None


async def publish_due_articles(interval_seconds: int) -> int:
    """Publish every accepted article whose completion timer elapsed.

    Due articles are claimed in the DB (status=publishing) by
    get_due_articles, so concurrent triggers can't double-publish. Each
    article is published then marked completed individually — one broken
    article must not skip or wedge the rest of the batch. Publishing failures
    never prevent completion; returns the number of articles claimed.
    """
    due = await get_due_articles(interval_seconds)
    for article in due:
        try:
            await publish_article(article)
        except Exception as exc:
            # Fail-soft per article: log and still complete it.
            logger.exception("Unexpected error publishing article %s", article.id)
            await record_notification(
                NOTIF_ERROR,
                "wordpress",
                f"Unexpected publish error: {exc or 'no details'}",
                article_id=article.id,
            )
        if article.id is None:
            continue
        await mark_articles_completed([article.id], STATUS_PUBLISHING)
    return len(due)
