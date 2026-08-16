import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (Index, Integer, String, create_engine, delete, event,
                        func, inspect, select, text)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.models import NewsItem

DB_PATH = Path(__file__).resolve().parent.parent / "newsflow.db"

# User-overridable settings and their default values (seeded into the DB).
DEFAULT_SETTINGS = {
    # Seconds between an article being accepted and it being marked completed.
    "completion_interval": "600",  # 10 minutes
}

# Article status flags. NULL (default) means the article is untouched.
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_COMPLETED = "completed"

# Setting key storing the UTC date of the last app run. When the app starts on
# a new day, the articles table is cleared for a fresh workspace.
LAST_RUN_DATE_KEY = "last_run_date"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Engine & session --------------------------------------------------------

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
    """Apply per-connection pragmas (WAL + a write busy timeout)."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# --- ORM models --------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        Index("idx_articles_source", "source"),
        Index("idx_articles_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str | None] = mapped_column(String)
    image_url: Mapped[str | None] = mapped_column(String)
    description: Mapped[str] = mapped_column(String, nullable=False)
    published_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    scraped_at: Mapped[str] = mapped_column(
        String, nullable=False, server_default=func.datetime("now")
    )
    status: Mapped[str | None] = mapped_column(String)
    accepted_at: Mapped[str | None] = mapped_column(String)
    accepted_order: Mapped[int | None] = mapped_column(Integer)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


# --- Helpers -----------------------------------------------------------------


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


async def _run(func, *args, **kwargs):
    """Run a blocking DB call in a worker thread."""
    return await asyncio.to_thread(func, *args, **kwargs)


# --- Schema setup ------------------------------------------------------------


def _migrate() -> None:
    """Add columns introduced after the initial schema to existing databases."""
    columns = {col["name"] for col in inspect(engine).get_columns("articles")}
    with engine.begin() as conn:
        if "status" not in columns:
            conn.execute(text("ALTER TABLE articles ADD COLUMN status TEXT"))
        if "accepted_at" not in columns:
            conn.execute(text("ALTER TABLE articles ADD COLUMN accepted_at TEXT"))
        if "accepted_order" not in columns:
            conn.execute(text("ALTER TABLE articles ADD COLUMN accepted_order INTEGER"))


def init_db() -> None:
    """Create the tables/indexes (and migrate) if they don't exist."""
    Base.metadata.create_all(engine)
    _migrate()

    with SessionLocal() as session:
        for key, value in DEFAULT_SETTINGS.items():
            session.execute(
                sqlite_insert(Setting)
                .values(key=key, value=value)
                .on_conflict_do_nothing()
            )
        # Fresh workspace per day: clear the articles table when the last run
        # was on a previous day.
        today = datetime.now(timezone.utc).date().isoformat()
        last_run = session.get(Setting, LAST_RUN_DATE_KEY)
        if last_run is None or last_run.value != today:
            session.execute(delete(Article))
            if last_run is None:
                session.add(Setting(key=LAST_RUN_DATE_KEY, value=today))
            else:
                last_run.value = today
        session.commit()


# --- Articles ----------------------------------------------------------------


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
    await _run(_save_items_sync, items)


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
    return await _run(_get_items_sync, source, None)


async def get_pending_items(source: str | None = None) -> list[NewsItem]:
    """Return articles that have not been accepted, rejected, or completed."""
    return await _run(_get_items_sync, source, "pending")


async def get_accepted_items(source: str | None = None) -> list[NewsItem]:
    """Return articles the user has accepted but not yet completed."""
    return await _run(_get_items_sync, source, "accepted")


async def get_completed_items(source: str | None = None) -> list[NewsItem]:
    """Return articles whose acceptance interval has elapsed."""
    return await _run(_get_items_sync, source, "completed")


async def get_rejected_items(source: str | None = None) -> list[NewsItem]:
    """Return articles the user has rejected."""
    return await _run(_get_items_sync, source, "rejected")


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
            article.accepted_at = _now_iso() if others is None else None
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
    return await _run(_set_article_status_sync, article_id, status)


def _complete_due_articles_sync(interval_seconds: int) -> int:
    """Complete accepted articles one at a time, in acceptance order.

    Only the first accepted (non-completed) article runs its completion timer;
    the rest wait in the queue. A queued article's timer starts only once it
    becomes the active one.
    """
    now = datetime.now(timezone.utc).timestamp()
    now_iso = _now_iso()
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
                    article.accepted_at = now_iso
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
    return await _run(_complete_due_articles_sync, interval_seconds)


def _get_cached_sources_sync() -> set[str]:
    with SessionLocal() as session:
        return set(session.scalars(select(Article.source).distinct()).all())


async def get_cached_sources() -> set[str]:
    """Return the set of sources that currently have cached articles."""
    return await _run(_get_cached_sources_sync)


# --- Settings ----------------------------------------------------------------


def _get_setting_sync(key: str) -> str | None:
    with SessionLocal() as session:
        setting = session.get(Setting, key)
    return setting.value if setting else None


async def get_setting(key: str, default: str) -> str:
    return await _run(_get_setting_sync, key) or default


def _set_setting_sync(key: str, value: str) -> None:
    with SessionLocal() as session:
        setting = session.get(Setting, key)
        if setting is None:
            session.add(Setting(key=key, value=value))
        else:
            setting.value = value
        session.commit()


async def set_setting(key: str, value: str) -> None:
    await _run(_set_setting_sync, key, value)


def _get_all_settings_sync() -> dict[str, str]:
    with SessionLocal() as session:
        return {
            key: value
            for key, value in session.execute(select(Setting.key, Setting.value))
        }


async def get_all_settings() -> dict[str, str]:
    return await _run(_get_all_settings_sync)


async def get_completion_interval() -> int:
    """Return the completion interval in seconds, falling back to the default."""
    raw = await get_setting(
        "completion_interval", DEFAULT_SETTINGS["completion_interval"]
    )
    try:
        return max(1, int(raw))
    except TypeError, ValueError:
        return int(DEFAULT_SETTINGS["completion_interval"])
