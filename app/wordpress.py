"""WordPress REST API client for site terms (categories + tags).

``sync_terms`` runs at startup and every ``WP_CATEGORIES_TTL`` seconds: it
fetches all pages of ``/wp-json/wp/v2/categories`` and ``/wp-json/wp/v2/tags``
and upserts them into the ``wp_terms`` table, so the publisher always has a
fresh term list to feed Gemini. Fail-soft like the rest of the publisher: a
failed sync is logged and retried on the next cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.config import WORDPRESS_URL
from app.db import (get_categories, get_last_terms_sync, get_tags,
                    set_last_terms_sync, upsert_terms)
from app.db.constants import NOTIF_WARNING, WP_CATEGORIES_TTL
from app.db.notifications import record_notification
from app.db.wp_terms import TAXONOMY_CATEGORY, TAXONOMY_TAG

logger = logging.getLogger(__name__)

API_BASE = "/wp-json/wp/v2"
PER_PAGE = 100  # REST API maximum


def _terms_url(taxonomy: str, page: int) -> str:
    # REST resource is plural while the taxonomy name is singular
    # ("category" -> "categories", "post_tag" -> "tags").
    resource = "categories" if taxonomy == TAXONOMY_CATEGORY else "tags"
    return f"{WORDPRESS_URL}{API_BASE}/{resource}?per_page={PER_PAGE}&page={page}"


async def fetch_terms(client: httpx.AsyncClient, taxonomy: str) -> list[dict]:
    """Fetch every page of a terms taxonomy; returns raw JSON term dicts.

    Stops when a page comes back short or a 400 "beyond the final page"
    response arrives. Raises on unexpected HTTP errors.
    """
    terms: list[dict] = []
    page = 1
    while True:
        resp = await client.get(
            _terms_url(taxonomy, page), headers=_auth_headers()
        )
        if resp.status_code == 400 and page > 1:
            # WordPress returns rest_post_invalid_page_number past the end.
            break
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        terms.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
    return terms


def _auth_headers() -> dict[str, str]:
    # Deferred import keeps the circular import (publisher ← wordpress ←
    # publisher) harmless and lets tests patch app.publisher.is_configured.
    from app.publisher import _auth_header

    return _auth_header()


async def sync_terms() -> None:
    """Fetch and store all WordPress categories + tags (idempotent upsert)."""
    from app.client import get_client  # lazy: tests patch app.client.get_client

    client = await get_client()
    for taxonomy, upsert in (
        (TAXONOMY_CATEGORY, upsert_categories),
        (TAXONOMY_TAG, upsert_tags),
    ):
        terms = await fetch_terms(client, taxonomy)
        await upsert(terms)
        logger.info(
            "Synced %d WordPress %s terms", len(terms), taxonomy
        )
    await set_last_terms_sync()


# Small indirections so sync_terms stays monkeypatch-friendly.
async def upsert_categories(terms: list[dict]) -> None:
    await upsert_terms(TAXONOMY_CATEGORY, terms)


async def upsert_tags(terms: list[dict]) -> None:
    await upsert_terms(TAXONOMY_TAG, terms)


def _terms_stale_from(last_synced: str | None) -> bool:
    if not last_synced:
        return True
    try:
        last = datetime.fromisoformat(last_synced)
    except (TypeError, ValueError):
        return True
    age = datetime.now(timezone.utc).timestamp() - last.timestamp()
    return age >= WP_CATEGORIES_TTL


async def sync_terms_if_stale() -> bool:
    """Sync when the stored terms are missing or older than the TTL.

    No-op when WordPress publishing is not configured. Returns True when a
    sync ran (successfully or not); False when skipped.
    """
    from app.publisher import is_configured  # lazy: avoid import cycle

    if not is_configured():
        return False
    if not _terms_stale_from(await get_last_terms_sync()):
        return False
    try:
        await sync_terms()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        logger.warning("WordPress terms sync failed: %s", exc)
        # Categorization depends on a fresh term list; surface the gap.
        await record_notification(
            NOTIF_WARNING,
            "wordpress",
            f"Terms sync failed: {exc or 'no details'}",
        )
    return True


async def ensure_terms() -> bool:
    """Best-effort sync at startup; always safe to call.

    Returns True when terms are available in the DB afterwards.
    """
    await sync_terms_if_stale()
    categories, tags = await get_categories(), await get_tags()
    if not categories:
        logger.warning("No WordPress categories available for categorization")
        return False
    logger.info(
        "WordPress terms ready: %d categories, %d tags", len(categories), len(tags)
    )
    return True


def format_terms_for_prompt(
    categories: list, tags: list
) -> tuple[list[tuple[int, str, str]], list[tuple[int, str, str]]]:
    """Convert synced WpCategory rows into (id, name, slug) triples."""
    return (
        [(c.wp_id, c.name, c.slug) for c in categories],
        [(t.wp_id, t.name, t.slug) for t in tags],
    )
