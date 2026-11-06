"""Offline tests for the cached-article writer (app/db/articles.py).

Runs against the throwaway SQLite database conftest provisions — never the
real ``newsflow.db`` and never the network. The focus is the cross-day rule:
the daily reset hands yesterday's workspace to ``publication_articles`` and
empties ``articles``, so ``save_items`` must refuse to re-insert a story the
archive already holds while still caching genuinely new ones.
"""

from __future__ import annotations

from sqlalchemy import select

import app.db as db_pkg
from app.db.articles import save_items
from app.db.models import Article, PublicationArticle
from app.models import NewsItem


def _item(**overrides) -> NewsItem:
    defaults = {
        "title": "Today's story",
        "url": "https://example.com/story",
        "image_url": None,
        "description": "desc",
        "published_at": "2026-11-06",
        "source": "kaumudi",
    }
    defaults.update(overrides)
    return NewsItem(**defaults)


def _archive(**overrides) -> None:
    """Store a row in yesterday's workspace (publication_articles)."""
    defaults = {
        "title": "Yesterday's story",
        "url": "https://example.com/yesterday",
        "image_url": None,
        "description": "desc",
        "published_at": "2026-11-05",
        "source": "kaumudi",
    }
    defaults.update(overrides)
    with db_pkg.SessionLocal() as session:
        session.add(PublicationArticle(**defaults))
        session.commit()


def _stored() -> list[tuple[str | None, str]]:
    """(url, title) pairs currently in today's curation workspace."""
    with db_pkg.SessionLocal() as session:
        return list(
            session.execute(
                select(Article.url, Article.title).order_by(Article.id)
            )
        )


async def test_skips_story_already_in_yesterday_archive():
    _archive(url="https://example.com/story")

    await save_items([_item()])

    assert _stored() == []


async def test_skips_archived_story_matched_by_title():
    """A row archived without a url is matched on its heading instead."""
    _archive(url=None, title="Today's story")

    await save_items([_item(url=None)])

    assert _stored() == []


async def test_archived_story_only_blocks_its_own_source():
    """Same key under another source: yesterday's row was not this source's."""
    _archive(source="mangalam", url="https://example.com/story")

    await save_items([_item()])

    assert _stored() == [("https://example.com/story", "Today's story")]


async def test_new_story_is_still_cached_alongside_the_archive():
    _archive()  # a different story from yesterday

    await save_items([_item()])

    assert _stored() == [("https://example.com/story", "Today's story")]


async def test_story_already_in_todays_workspace_is_not_duplicated():
    """The pre-existing same-day dedupe keeps working after the refactor."""
    await save_items([_item()])

    await save_items([_item()])  # same url, e.g. a second refresh round

    assert _stored() == [("https://example.com/story", "Today's story")]


async def test_batch_dedupes_within_itself_and_keeps_unseen_items():
    await save_items(
        [
            _item(),
            _item(),  # duplicate inside one batch
            _item(title="Second story", url="https://example.com/second"),
        ]
    )

    assert _stored() == [
        ("https://example.com/story", "Today's story"),
        ("https://example.com/second", "Second story"),
    ]


async def test_save_items_returns_only_the_newly_stored_articles():
    """The return value drives refresh reporting, so it must exclude every
    duplicate (today's workspace and yesterday's archive)."""
    _archive(url="https://example.com/yesterday")

    stored = await save_items(
        [
            _item(url="https://example.com/yesterday"),  # from the archive
            _item(),  # genuinely new
            _item(),  # duplicate of the new one inside the batch
        ]
    )

    assert [item.url for item in stored] == ["https://example.com/story"]


async def test_save_items_returns_empty_when_everything_was_known():
    _archive(url="https://example.com/story")

    assert await save_items([_item()]) == []
