"""Data access for cached news articles.

``get_due_articles`` atomically claims each due article by flipping its status
to :data:`STATUS_PUBLISHING` in the same transaction that detects it, so
concurrent triggers (the background completion loop and UI reloads) can never
publish the same article twice. """

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from app.db.constants import (
    STATUS_ACCEPTED,
    STATUS_COMPLETED,
    STATUS_PUBLISHING,
    STATUS_REJECTED,
    now_iso,
)
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
        cover_file=article.cover_file,
        wp_category_ids=article.wp_category_ids,
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


def _trim_articles_sync(max_per_source: int) -> None:
    """Delete oldest pending articles so each source keeps at most *max_per_source* rows.

    Articles that have been accepted, completed, or rejected are never removed
    by the trim — only unprocessed (status=None) rows beyond the cap are pruned,
    keeping the newest ones.
    """
    with SessionLocal() as session:
        sources = session.scalars(select(Article.source).distinct()).all()
        for source in sources:
            # IDs of the newest *max_per_source* pending articles for this source.
            keep_ids = session.scalars(
                select(Article.id)
                .where(Article.source == source, Article.status.is_(None))
                .order_by(Article.id.desc())
                .limit(max_per_source)
            ).all()
            if keep_ids:
                # Delete every pending row for this source that isn't in the keep set.
                session.query(Article).filter(
                    Article.source == source,
                    Article.status.is_(None),
                    ~Article.id.in_(keep_ids),
                ).delete(synchronize_session="fetch")
        session.commit()


async def trim_articles(max_per_source: int) -> None:
    """Trim each source to at most *max_per_source* pending articles."""
    await run_in_thread(_trim_articles_sync, max_per_source)


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


def _get_article_by_id_sync(article_id: int) -> NewsItem | None:
    with SessionLocal() as session:
        article = session.get(Article, article_id)
        if article is None:
            return None
        return _article_to_item(article)


async def get_article_by_id(article_id: int) -> NewsItem | None:
    """Return a single article by its ID, or None if not found."""
    return await run_in_thread(_get_article_by_id_sync, article_id)


def _update_article_cover_file_sync(article_id: int, cover_file: str | None) -> bool:
    with SessionLocal() as session:
        article = session.get(Article, article_id)
        if article is None:
            return False
        article.cover_file = cover_file
        session.commit()
    return True


async def update_article_cover_file(article_id: int, cover_file: str | None) -> bool:
    """Store the local cover file path for an article."""
    return await run_in_thread(_update_article_cover_file_sync, article_id, cover_file)


def _get_due_articles_sync(interval_seconds: int) -> list[NewsItem]:
    """Return accepted articles whose completion timer has elapsed.

    Only the first accepted (non-completed) article runs its completion timer;
    the rest wait in the queue. A queued article's timer starts only once it
    becomes the active one.

    Each due article is atomically claimed for publishing by setting its
    status to :data:`STATUS_PUBLISHING` in the same transaction, so a
    concurrent trigger (background loop racing a UI reload) cannot return the
    same article again — this is what prevents duplicate WordPress posts.
    """
    now = datetime.now(UTC).timestamp()
    current = now_iso()
    due: list[NewsItem] = []
    with SessionLocal() as session:
        # Take the write lock before reading: two triggers otherwise both
        # SELECT (pysqlite defers BEGIN to writes), both see the article as
        # accepted, and both publish it. BEGIN IMMEDIATE + the engine's
        # busy_timeout serializes them — the loser reads committed state.
        session.execute(text("BEGIN IMMEDIATE"))
        articles = session.scalars(
            select(Article)
            .where(Article.status == STATUS_ACCEPTED)
            .order_by(Article.accepted_order.asc(), Article.id.asc())
        ).all()
        claimed_head = False
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
                except (TypeError, ValueError):
                    continue
                if now - accepted_ts >= interval_seconds:
                    # Claim the article for publishing in this same
                    # transaction: a second trigger running concurrently sees
                    # status=publishing and won't return it again.
                    article.status = STATUS_PUBLISHING
                    due.append(_article_to_item(article))
                    claimed_head = True
            elif index == 1 and claimed_head:
                # Hand the active timer slot to the next queued article; its
                # own publish happens once its interval elapses.
                article.accepted_at = current
            elif article.accepted_at is not None:
                # Queued article: its timer must not have started yet.
                article.accepted_at = None
        session.commit()
    return due


async def get_due_articles(interval_seconds: int) -> list[NewsItem]:
    """Return accepted articles whose completion interval has elapsed."""
    return await run_in_thread(_get_due_articles_sync, interval_seconds)


def _mark_articles_completed_sync(
    article_ids: list[int], expected_status: str
) -> int:
    """Mark the given claimed articles completed (skips other rows).

    Only articles currently in *expected_status* are valid targets — the
    publisher passes its STATUS_PUBLISHING claim. A row already completed by
    another path, or one that lost its claim, is skipped.
    """
    completed = 0
    with SessionLocal() as session:
        for article_id in article_ids:
            article = session.get(Article, article_id)
            if article is None or article.status != expected_status:
                continue
            article.status = STATUS_COMPLETED
            article.accepted_order = None
            completed += 1
        session.commit()
    return completed


async def mark_articles_completed(
    article_ids: list[int], expected_status: str = STATUS_ACCEPTED
) -> int:
    """Mark articles as completed by ID. Returns how many changed.

    ``expected_status`` guards the transition: only rows currently in that
    state are completed. The publisher passes STATUS_PUBLISHING (its claim);
    the default keeps the plain accepted → completed transition.
    """
    if not article_ids:
        return 0
    return await run_in_thread(
        _mark_articles_completed_sync, article_ids, expected_status
    )


def _get_recent_accepted_titles_sync(limit: int) -> list[tuple[str, str]]:
    """Return the *limit* most recently accepted (source, title) pairs.

    Feeds the AI Publish prompt as duplicate-avoidance context: headings
    already accepted (any accepted → completed stage) are things the site has
    already covered, so Gemini should not pick the same story again. Oldest
    first so the prompt reads chronologically; ties broken by newer ID first.
    """
    with SessionLocal() as session:
        rows = session.execute(
            select(Article.source, Article.title)
            .where(Article.status.in_([STATUS_ACCEPTED, STATUS_COMPLETED,
                                       STATUS_PUBLISHING]))
            .order_by(Article.id.desc())
            .limit(limit)
        ).all()
    return list(reversed(rows))


async def get_recent_accepted_titles(limit: int) -> list[tuple[str, str]]:
    """Most recently accepted article headings, oldest first."""
    return await run_in_thread(_get_recent_accepted_titles_sync, limit)


def _get_cached_sources_sync() -> set[str]:
    with SessionLocal() as session:
        return set(session.scalars(select(Article.source).distinct()).all())


async def get_cached_sources() -> set[str]:
    """Return the set of sources that currently have cached articles."""
    return await run_in_thread(_get_cached_sources_sync)
