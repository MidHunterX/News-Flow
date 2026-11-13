"""Offline tests for the cached-article writer (app/db/articles.py).

Runs against the throwaway SQLite database conftest provisions — never the
real ``newsflow.db`` and never the network. The focus is the cross-day rule:
the daily reset hands yesterday's workspace to ``publication_articles`` and
empties ``articles``, so ``save_items`` must refuse to re-insert a story the
archive already holds while still caching genuinely new ones.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

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


# ---------------------------------------------------------------------------
# DB-level unique constraint on ``articles.url`` (race-proof dedupe)
# ---------------------------------------------------------------------------


def _raw_url_rows() -> set[str]:
    """Every non-null ``url`` currently in today's curation workspace."""
    with db_pkg.SessionLocal() as session:
        return {
            row[0]
            for row in session.execute(
                select(Article.url).where(Article.url.isnot(None))
            ).all()
        }


async def test_url_is_unique_at_the_db_level():
    """Two scrapers racing to insert the same story can only ever create one
    row — the unique index on ``articles.url`` enforces it, not application
    code. The application dedupe still avoids the pointless second INSERT for
    the common path, but the DB is the authority when they do race.
    """

    await save_items([_item()])

    # A second writer inserting the same url must hit the unique constraint.
    with db_pkg.SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(Article(
            title="Same story again",
            url="https://example.com/story",
            image_url=None,
            description="desc",
            published_at="2026-11-06",
            source="kaumudi",
        ))
        session.commit()

    # The winner is still the single row we stored first.
    assert _raw_url_rows() == {"https://example.com/story"}


async def test_url_constraint_is_enforced_even_when_save_items_is_bypassed():
    """The constraint is the authority, so even a writer that skips
    ``save_items`` (raw session INSERT, another process, a migration script)
    cannot create a second row for the same url. The loser's commit raises
    ``IntegrityError``.
    """

    await save_items([_item(url="https://example.com/race")])

    with db_pkg.SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(Article(
            title="Second writer",
            url="https://example.com/race",
            image_url=None,
            description="desc",
            published_at="2026-11-06",
            source="kaumudi",
        ))
        session.commit()

    # The workspace still has exactly one row for that url.
    assert _raw_url_rows() == {"https://example.com/race"}


async def test_startup_deduplicates_an_already_duplicated_workspace():
    """If the workspace already accumulated duplicate ``url`` rows (e.g. from
    a race before the constraint existed, or before this repair landed), the
    next ``init_db`` call trims them down to one row per url. Rows without a
    url are left alone — they are still handled by the title-based application
    dedupe.

    The duplicates are planted with raw SQL while the unique index is dropped,
    then the index is recreated *after* the repair so we can assert the
    workspace is clean under a real constraint.
    """
    from app.db.schema import _dedupe_existing_urls_sync

    with db_pkg.SessionLocal() as session:
        # Drop the unique index so we can intentionally create duplicate urls.
        session.execute(text("DROP INDEX IF EXISTS idx_articles_url"))
        session.commit()

        session.add_all([
            Article(
                title="Dup A",
                url="https://example.com/dup",
                image_url=None,
                description="desc",
                published_at="2026-11-06",
                source="kaumudi",
            ),
            Article(
                title="Dup B",
                url="https://example.com/dup",
                image_url=None,
                description="desc",
                published_at="2026-11-06",
                source="kaumudi",
            ),
            Article(
                title="Same title",
                url=None,
                image_url=None,
                description="desc",
                published_at="2026-11-06",
                source="kaumudi",
            ),
            Article(
                title="Same title",
                url=None,
                image_url=None,
                description="desc",
                published_at="2026-11-06",
                source="kaumudi",
            ),
            Article(
                title="Unique",
                url="https://example.com/unique",
                image_url=None,
                description="desc",
                published_at="2026-11-06",
                source="kaumudi",
            ),
        ])
        session.commit()

        # Run the actual repair path directly (the same code init_db uses).
        removed = _dedupe_existing_urls_sync(session)
        assert removed == 1  # one duplicate url, one row removed
        session.commit()

    # Now recreate the unique index: it must succeed because the workspace is
    # clean (one row per url).
    with db_pkg.SessionLocal() as session:
        session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url "
                "ON articles (url)"
            )
        )
        session.commit()

    with db_pkg.SessionLocal() as session:
        urls = {
            row[0]
            for row in session.execute(
                select(Article.url).where(Article.url.isnot(None))
            ).all()
        }
        titles = {
            row[0]
            for row in session.execute(
                select(Article.title).where(Article.url.is_(None))
            ).all()
        }

    # Duplicate urls collapsed to one each; unique row untouched.
    assert urls == {"https://example.com/dup", "https://example.com/unique"}
    # Title-only duplicates are NOT removed by the url repair (they are the
    # application dedupe's job on the next save_items call).
    assert titles == {"Same title"}


async def test_init_db_repairs_duplicate_urls_created_after_the_reset():
    """``init_db`` clears ``articles`` for a fresh day, so duplicates that
    existed *before* startup are wiped by the reset, not by the dedupe.
    The dedupe path still runs after the reset, and it matters when a race
    inserts duplicates *after* the reset but *before* the next startup — e.g.
    when a throwaway test DB already has the index and a later
    ``init_db`` call encounters duplicates that were created in its fresh
    workspace.

    This test plants duplicates **after** an ``init_db`` reset (so they land
    in today's fresh workspace under the live unique index... which would
    reject them). To get duplicates into a workspace that already has the
    index, we drop the index, plant them, repair with the same function
    ``init_db`` uses, then re-create the index to prove the workspace is
    clean.
    """
    from app.db.schema import _dedupe_existing_urls_sync

    db_pkg.init_db()  # fresh day: clear curation workspace

    with db_pkg.SessionLocal() as session:
        session.execute(text("DROP INDEX IF EXISTS idx_articles_url"))
        session.commit()

        session.add_all([
            Article(
                title="Post-reset dup",
                url="https://example.com/post",
                image_url=None,
                description="desc",
                published_at="2026-11-06",
                source="kaumudi",
            ),
            Article(
                title="Post-reset dup 2",
                url="https://example.com/post",
                image_url=None,
                description="desc",
                published_at="2026-11-06",
                source="kaumudi",
            ),
        ])
        session.commit()

        removed = _dedupe_existing_urls_sync(session)
        assert removed == 1
        session.commit()

        session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url "
                "ON articles (url)"
            )
        )
        session.commit()

    with db_pkg.SessionLocal() as session:
        urls = {
            row[0]
            for row in session.execute(
                select(Article.url).where(Article.url.isnot(None))
            ).all()
        }

    assert urls == {"https://example.com/post"}

async def test_save_items_still_reports_only_genuinely_new_rows_after_a_race():
    """Even when a url is inserted by another session between the pre-check
    and the commit (the exact race the constraint prevents from duplicating),
    the return value stays honest about what this call stored.

    Here we fake that race by pre-seeding the url, so save_items skips it.
    """
    with db_pkg.SessionLocal() as session:
        session.add(Article(
            title="Already there",
            url="https://example.com/race-win",
            image_url=None,
            description="desc",
            published_at="2026-11-06",
            source="kaumudi",
        ))
        session.commit()

    stored = await save_items([_item(url="https://example.com/race-win")])

    assert stored == []
    assert _raw_url_rows() == {"https://example.com/race-win"}
