"""Data access for WordPress terms (categories + tags) synced from the site.

The publisher feeds these terms to Gemini so the model can pick matching
categories; ``set_article_category_ids`` stores the chosen IDs on the article.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.db.constants import WP_TERMS_SYNCED_KEY
from app.db.engine import SessionLocal, run_in_thread
from app.db.models import Article, WpCategory
from app.db.settings import get_setting, set_setting

# Taxonomies we sync from the WordPress REST API.
TAXONOMY_CATEGORY = "category"
TAXONOMY_TAG = "post_tag"


def _upsert_terms_sync(
    taxonomy: str, terms: list[dict]
) -> None:
    """Insert/update synced terms for *taxonomy*; prune terms gone from WP."""
    with SessionLocal() as session:
        existing = {
            term.wp_id: term
            for term in session.scalars(
                select(WpCategory).where(WpCategory.taxonomy == taxonomy)
            ).all()
        }
        seen: set[int] = set()
        for term in terms:
            wp_id = int(term["id"])
            seen.add(wp_id)
            row = existing.get(wp_id)
            if row is None:
                row = WpCategory(wp_id=wp_id, taxonomy=taxonomy)
                session.add(row)
            row.name = str(term.get("name", ""))
            row.slug = str(term.get("slug", ""))
            row.synced_at = datetime.now(UTC).isoformat(timespec="seconds")
        # Drop terms deleted on the WordPress side.
        for wp_id, row in existing.items():
            if wp_id not in seen:
                session.delete(row)
        session.commit()


async def upsert_terms(taxonomy: str, terms: list[dict]) -> None:
    """Persist a page of WordPress terms fetched from the REST API."""
    await run_in_thread(_upsert_terms_sync, taxonomy, terms)


def _get_terms_sync(taxonomy: str) -> list[WpCategory]:
    with SessionLocal() as session:
        return (
            session.scalars(
                select(WpCategory)
                .where(WpCategory.taxonomy == taxonomy)
                .order_by(WpCategory.name.collate("NOCASE"))
            )
            .unique()
            .all()
        )


async def get_categories() -> list[WpCategory]:
    """Return all synced WordPress categories, ordered by name."""
    return await run_in_thread(_get_terms_sync, TAXONOMY_CATEGORY)


async def get_tags() -> list[WpCategory]:
    """Return all synced WordPress tags, ordered by name."""
    return await run_in_thread(_get_terms_sync, TAXONOMY_TAG)


def _set_article_category_ids_sync(article_id: int, ids: list[int]) -> bool:
    with SessionLocal() as session:
        article = session.get(Article, article_id)
        if article is None:
            return False
        article.wp_category_ids = ids
        session.commit()
    return True


async def set_article_category_ids(article_id: int, ids: list[int]) -> bool:
    """Store the Gemini-chosen WordPress category IDs on an article."""
    return await run_in_thread(_set_article_category_ids_sync, article_id, ids)


async def get_last_terms_sync() -> str | None:
    """ISO-8601 UTC timestamp of the last successful terms sync, or None."""
    return await get_setting(WP_TERMS_SYNCED_KEY, "") or None


async def set_last_terms_sync() -> None:
    """Record a successful terms sync as of now."""
    await set_setting(
        WP_TERMS_SYNCED_KEY,
        datetime.now(UTC).isoformat(timespec="seconds"),
    )
