import asyncio
import sqlite3
from collections import defaultdict
from pathlib import Path

from app.models import NewsItem

DB_PATH = Path(__file__).resolve().parent.parent / "newsflow.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the articles table and indexes if they don't exist."""
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source)"
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


def _get_items_sync(source: str | None = None) -> list[NewsItem]:
    with _connect() as conn:
        if source is None:
            rows = conn.execute(
                "SELECT title, url, image_url, description, published_at, source "
                "FROM articles ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT title, url, image_url, description, published_at, source "
                "FROM articles WHERE source = ? ORDER BY id DESC",
                (source,),
            ).fetchall()
    return [
        NewsItem(
            title=row["title"],
            url=row["url"],
            image_url=row["image_url"],
            description=row["description"],
            published_at=row["published_at"],
            source=row["source"],
        )
        for row in rows
    ]


async def get_items(source: str | None = None) -> list[NewsItem]:
    """Return cached articles, optionally filtered by source."""
    return await asyncio.to_thread(_get_items_sync, source)


def _get_cached_sources_sync() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT source FROM articles").fetchall()
    return {row["source"] for row in rows}


async def get_cached_sources() -> set[str]:
    """Return the set of sources that currently have cached articles."""
    return await asyncio.to_thread(_get_cached_sources_sync)
