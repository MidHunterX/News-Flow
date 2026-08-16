"""Data access for cached news articles."""

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db.constants import (STATUS_ACCEPTED, STATUS_COMPLETED,
                              STATUS_REJECTED, now_iso)
from app.db.engine import SessionLocal, run_in_thread
from app.db.models import Article
from app.models import NewsItem


def _dedupe(items: list[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    result: list[NewsItem] = []
    for item in items:
        key = item.url or item.title
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _article_to_item(article: Article) -> NewsItem:
    return NewsItem(
        id=article.id,
        title=article.title,
        url=article.url,
        image_url=article.image_url,
        description=article.description,
        published_at=article.published_at,
        source=article.source,
        status=article.status,
        accepted_at=article.accepted_at,
    )


def _save_items_sync(items: list[NewsItem]) -> None:
    by_source: dict[str, list[NewsItem]] = defaultdict(list)
    for item in _dedupe(items):
        by_source[item.source].append(item)

    with SessionLocal() as session:
        for source, source_items in by_source.items():
            existing_keys = {
                key
                for (url, title) in session.execute(
                    select(Article.url, Article.title).where(Article.source == source)
                )
                if (key := url or title)
            }
            new_items = [
                item
                for item in source_items
                if (item.url or item.title) not in existing_keys
            ]
            if not new_items:
                continue
            session.add_all(
                Article(
                    title=item.title,
                    url=item.url,
                    image_url=item.image_url,
                    description=item.description,
                    published_at=item.published_at,
                    source=item.source,
                )
                for item in new_items
            )
        session.commit()


async def save_items(items: list[NewsItem]) -> None:
    """Insert cached articles not already stored, keeping previously seen ones."""
    await run_in_thread(_save_items_sync, items)


def _get_items_sync(
    source: str | None = None, status_filter: str | None = None
) -> list[NewsItem]:
    """status_filter: 'pending', 'accepted', 'completed', 'rejected', or None for all."""
    stmt = select(Article).order_by(Article.id.desc())
    if source is not None:
        stmt = stmt.where(Article.source == source)
    if status_filter == "pending":
        stmt = stmt.where(Article.status.is_(None))
    elif status_filter == "accepted":
        stmt = stmt.where(Article.status == STATUS_ACCEPTED)
    elif status_filter == "completed":
        stmt = stmt.where(Article.status == STATUS_COMPLETED)
    elif status_filter == "rejected":
        stmt = stmt.where(Article.status == STATUS_REJECTED)

    with SessionLocal() as session:
        articles = session.scalars(stmt).all()
    return [_article_to_item(article) for article in articles]


async def get_items(source: str | None = None) -> list[NewsItem]:
    """Return all cached articles, optionally filtered by source."""
    return await run_in_thread(_get_items_sync, source, None)


async def get_pending_items(source: str | None = None) -> list[NewsItem]:
    """Return articles that have not been accepted, rejected, or completed."""
    return await run_in_thread(_get_items_sync, source, "pending")


async def get_accepted_items(source: str | None = None) -> list[NewsItem]:
    """Return articles the user has accepted but not yet completed."""
    return await run_in_thread(_get_items_sync, source, "accepted")


async def get_completed_items(source: str | None = None) -> list[NewsItem]:
    """Return articles whose acceptance interval has elapsed."""
    return await run_in_thread(_get_items_sync, source, "completed")


async def get_rejected_items(source: str | None = None) -> list[NewsItem]:
    """Return articles the user has rejected."""
    return await run_in_thread(_get_items_sync, source, "rejected")


def _set_article_status_sync(article_id: int, status: str | None) -> bool:
    with SessionLocal() as session:
        article = session.get(Article, article_id)
        if article is None:
            return False
        if status == STATUS_ACCEPTED:
            # Queue the article behind every previously accepted one.
            max_order = session.scalar(select(func.max(Article.accepted_order))) or 0
            article.accepted_order = max_order + 1
            # Only one article runs its completion timer at a time: if this is
            # the only accepted article its timer starts now, otherwise it
            # waits in the queue until its turn.
            others = session.scalar(
                select(Article.id)
                .where(Article.status == STATUS_ACCEPTED, Article.id != article_id)
                .limit(1)
            )
            article.status = STATUS_ACCEPTED
            article.accepted_at = now_iso() if others is None else None
        elif status is None:
            article.status = None
            article.accepted_at = None
            article.accepted_order = None
        else:
            article.status = status
            article.accepted_order = None
        session.commit()
    return True


async def set_article_status(article_id: int, status: str | None) -> bool:
    """Set or clear the status flag on an article. Returns False if not found."""
    return await run_in_thread(_set_article_status_sync, article_id, status)


def _complete_due_articles_sync(interval_seconds: int) -> int:
    """Complete accepted articles one at a time, in acceptance order.

    Only the first accepted (non-completed) article runs its completion timer;
    the rest wait in the queue. A queued article's timer starts only once it
    becomes the active one.
    """
    now = datetime.now(timezone.utc).timestamp()
    current = now_iso()
    completed = 0
    with SessionLocal() as session:
        articles = session.scalars(
            select(Article)
            .where(Article.status == STATUS_ACCEPTED)
            .order_by(Article.accepted_order.asc(), Article.id.asc())
        ).all()
        for index, article in enumerate(articles):
            if index == 0:
                # Active article: start its timer if it hasn't begun yet.
                if article.accepted_at is None:
                    article.accepted_at = current
                    continue
                try:
                    accepted_ts = datetime.fromisoformat(
                        article.accepted_at
                    ).timestamp()
                except TypeError, ValueError:
                    continue
                if now - accepted_ts >= interval_seconds:
                    article.status = STATUS_COMPLETED
                    article.accepted_order = None
                    completed += 1
            elif article.accepted_at is not None:
                # Queued article: its timer must not have started yet.
                article.accepted_at = None
        session.commit()
    return completed


async def complete_due_articles(interval_seconds: int) -> int:
    """Mark accepted articles as completed once their interval elapses."""
    return await run_in_thread(_complete_due_articles_sync, interval_seconds)


def _get_cached_sources_sync() -> set[str]:
    with SessionLocal() as session:
        return set(session.scalars(select(Article.source).distinct()).all())


async def get_cached_sources() -> set[str]:
    """Return the set of sources that currently have cached articles."""
    return await run_in_thread(_get_cached_sources_sync)
