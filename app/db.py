import asyncio
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema to existing databases."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    if "status" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN status TEXT")
    if "accepted_at" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN accepted_at TEXT")


def init_db() -> None:
    """Create the tables/indexes (and migrate) if they don't exist."""
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT,
                image_url TEXT,
                description TEXT NOT NULL,
                published_at TEXT NOT NULL,
                source TEXT NOT NULL,
                scraped_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """)
        _migrate(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


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


def _save_items_sync(items: list[NewsItem]) -> None:
    by_source: dict[str, list[NewsItem]] = defaultdict(list)
    for item in _dedupe(items):
        by_source[item.source].append(item)

    with _connect() as conn:
        for source, source_items in by_source.items():
            existing = conn.execute(
                "SELECT url, title FROM articles WHERE source = ?", (source,)
            ).fetchall()
            existing_keys = {row["url"] or row["title"] for row in existing}
            new_items = [
                item
                for item in source_items
                if (item.url or item.title) not in existing_keys
            ]
            if not new_items:
                continue
            conn.executemany(
                """
                INSERT INTO articles (title, url, image_url, description, published_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.title,
                        item.url,
                        item.image_url,
                        item.description,
                        item.published_at,
                        item.source,
                    )
                    for item in new_items
                ],
            )


async def save_items(items: list[NewsItem]) -> None:
    """Insert cached articles not already stored, keeping previously seen ones."""
    await asyncio.to_thread(_save_items_sync, items)


def _row_to_item(row: sqlite3.Row) -> NewsItem:
    return NewsItem(
        id=row["id"],
        title=row["title"],
        url=row["url"],
        image_url=row["image_url"],
        description=row["description"],
        published_at=row["published_at"],
        source=row["source"],
        status=row["status"],
        accepted_at=row["accepted_at"],
    )


def _get_items_sync(
    source: str | None = None, status_filter: str | None = None
) -> list[NewsItem]:
    """status_filter: 'pending', 'accepted' (accepted + completed), 'rejected', or None for all."""
    query = (
        "SELECT id, title, url, image_url, description, published_at, source, status, accepted_at "
        "FROM articles"
    )
    clauses: list[str] = []
    params: list[object] = []
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if status_filter == "pending":
        clauses.append("status IS NULL")
    elif status_filter == "accepted":
        clauses.append("status IN (?, ?)")
        params.extend([STATUS_ACCEPTED, STATUS_COMPLETED])
    elif status_filter == "rejected":
        clauses.append("status = ?")
        params.append(STATUS_REJECTED)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_item(row) for row in rows]


async def get_items(source: str | None = None) -> list[NewsItem]:
    """Return all cached articles, optionally filtered by source."""
    return await asyncio.to_thread(_get_items_sync, source, None)


async def get_pending_items(source: str | None = None) -> list[NewsItem]:
    """Return articles that have not been accepted, rejected, or completed."""
    return await asyncio.to_thread(_get_items_sync, source, "pending")


async def get_accepted_items(source: str | None = None) -> list[NewsItem]:
    """Return accepted articles, including ones already marked completed."""
    return await asyncio.to_thread(_get_items_sync, source, "accepted")


async def get_rejected_items(source: str | None = None) -> list[NewsItem]:
    """Return articles the user has rejected."""
    return await asyncio.to_thread(_get_items_sync, source, "rejected")


def _set_article_status_sync(article_id: int, status: str | None) -> bool:
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if exists is None:
            return False
        if status == STATUS_ACCEPTED:
            conn.execute(
                "UPDATE articles SET status = ?, accepted_at = ? WHERE id = ?",
                (STATUS_ACCEPTED, _now_iso(), article_id),
            )
        elif status is None:
            conn.execute(
                "UPDATE articles SET status = NULL, accepted_at = NULL WHERE id = ?",
                (article_id,),
            )
        else:
            conn.execute(
                "UPDATE articles SET status = ? WHERE id = ?", (status, article_id)
            )
    return True


async def set_article_status(article_id: int, status: str | None) -> bool:
    """Set or clear the status flag on an article. Returns False if not found."""
    return await asyncio.to_thread(_set_article_status_sync, article_id, status)


def _complete_due_articles_sync(interval_seconds: int) -> int:
    """Mark accepted articles whose completion deadline has passed as completed."""
    now = datetime.now(timezone.utc).timestamp()
    due_ids: list[int] = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, accepted_at FROM articles WHERE status = ?",
            (STATUS_ACCEPTED,),
        ).fetchall()
        for row in rows:
            try:
                accepted_ts = datetime.fromisoformat(row["accepted_at"]).timestamp()
            except TypeError, ValueError:
                continue
            if now - accepted_ts >= interval_seconds:
                due_ids.append(row["id"])
        if due_ids:
            conn.executemany(
                "UPDATE articles SET status = ? WHERE id = ?",
                [(STATUS_COMPLETED, article_id) for article_id in due_ids],
            )
    return len(due_ids)


async def complete_due_articles(interval_seconds: int) -> int:
    """Mark accepted articles as completed once their interval elapses."""
    return await asyncio.to_thread(_complete_due_articles_sync, interval_seconds)


def _get_setting_sync(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


async def get_setting(key: str, default: str) -> str:
    return await asyncio.to_thread(_get_setting_sync, key) or default


def _set_setting_sync(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


async def set_setting(key: str, value: str) -> None:
    await asyncio.to_thread(_set_setting_sync, key, value)


def _get_all_settings_sync() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


async def get_all_settings() -> dict[str, str]:
    return await asyncio.to_thread(_get_all_settings_sync)


async def get_completion_interval() -> int:
    """Return the completion interval in seconds, falling back to the default."""
    raw = await get_setting(
        "completion_interval", DEFAULT_SETTINGS["completion_interval"]
    )
    try:
        return max(1, int(raw))
    except TypeError, ValueError:
        return int(DEFAULT_SETTINGS["completion_interval"])


def _get_cached_sources_sync() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT source FROM articles").fetchall()
    return {row["source"] for row in rows}


async def get_cached_sources() -> set[str]:
    """Return the set of sources that currently have cached articles."""
    return await asyncio.to_thread(_get_cached_sources_sync)
