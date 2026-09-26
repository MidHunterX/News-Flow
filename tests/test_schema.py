"""Offline tests for the daily workspace reset (app/db/schema.py).

Binds app.db.schema to a throwaway SQLite database and temp workspace dirs,
so the real newsflow.db / public/ files are never touched (no network).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import schema
from app.db.constants import LAST_RUN_DATE_KEY, STATUS_ACCEPTED, STATUS_PUBLISHING
from app.db.models import Article, Base, Setting

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_env(tmp_path, monkeypatch):
    """Bind app.db.schema to a throwaway DB and temp workspace dirs."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(schema, "engine", engine)
    monkeypatch.setattr(schema, "SessionLocal", session_factory)

    articles_dir = tmp_path / "articles"
    covers_dir = tmp_path / "covers"
    articles_dir.mkdir()
    covers_dir.mkdir()
    monkeypatch.setattr(schema, "ARTICLES_DIR", articles_dir)
    monkeypatch.setattr(schema, "COVERS_DIR", covers_dir)
    return session_factory, articles_dir, covers_dir


def _set_last_run(session, value: str) -> None:
    session.add(Setting(key=LAST_RUN_DATE_KEY, value=value))
    session.commit()


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _insert_article(session, **overrides) -> Article:
    defaults = dict(
        title="First Article",
        url="https://example.com/a",
        image_url=None,
        description="desc",
        published_at="2026-09-19",
        source="kaumudi",
    )
    defaults.update(overrides)
    article = Article(**defaults)
    session.add(article)
    session.commit()
    return article


# ---------------------------------------------------------------------------
# _reset_daily_workspace
# ---------------------------------------------------------------------------


class TestResetDailyWorkspace:
    def test_deletes_rows_and_attached_files(self, reset_env):
        """A new day wipes the articles table and its covers/content files."""
        session_factory, articles_dir, covers_dir = reset_env
        with session_factory() as session:
            _set_last_run(session, "2000-01-01")  # any previous day
            first = _insert_article(session, cover_file="/covers/one.png")
            second = _insert_article(
                session, title="Second", cover_file="/covers/two.png"
            )
        for article_id in (first.id, second.id):
            (articles_dir / f"{article_id}.txt").write_text("H\n\nB",
                                                            encoding="utf-8")
        (covers_dir / "one.png").write_bytes(b"png")
        (covers_dir / "two.png").write_bytes(b"png")

        schema._reset_daily_workspace()

        with session_factory() as session:
            assert session.query(Article).count() == 0
            last_run = session.get(Setting, LAST_RUN_DATE_KEY)
            assert last_run is not None and last_run.value == _today()
        for article_id in (first.id, second.id):
            assert not (articles_dir / f"{article_id}.txt").exists()
        assert not (covers_dir / "one.png").exists()
        assert not (covers_dir / "two.png").exists()

    def test_same_day_keeps_rows_and_files(self, reset_env):
        """Restarting on the same day must not touch anything."""
        session_factory, articles_dir, covers_dir = reset_env
        with session_factory() as session:
            _set_last_run(session, _today())
            article = _insert_article(session, cover_file="/covers/keep.png")
        (articles_dir / f"{article.id}.txt").write_text("H\n\nB",
                                                        encoding="utf-8")
        (covers_dir / "keep.png").write_bytes(b"png")

        schema._reset_daily_workspace()

        with session_factory() as session:
            assert session.query(Article).count() == 1
            last_run = session.get(Setting, LAST_RUN_DATE_KEY)
            assert last_run.value == _today()
        assert (articles_dir / f"{article.id}.txt").exists()
        assert (covers_dir / "keep.png").exists()

    def test_first_run_resets_and_seeds_last_run(self, reset_env):
        """No last_run setting (fresh DB) still clears stray workspace files."""
        session_factory, articles_dir, covers_dir = reset_env
        with session_factory() as session:
            article = _insert_article(session, cover_file="/covers/x.png")
        (articles_dir / f"{article.id}.txt").write_text("H\n\nB",
                                                        encoding="utf-8")
        (covers_dir / "x.png").write_bytes(b"png")

        schema._reset_daily_workspace()

        with session_factory() as session:
            assert session.query(Article).count() == 0
            last_run = session.get(Setting, LAST_RUN_DATE_KEY)
            assert last_run is not None and last_run.value == _today()
        assert not (articles_dir / f"{article.id}.txt").exists()
        assert not (covers_dir / "x.png").exists()

    def test_wipes_orphans_and_whole_workspace(self, reset_env):
        """The whole workspace is emptied, including files with no article row."""
        session_factory, articles_dir, covers_dir = reset_env
        with session_factory() as session:
            _set_last_run(session, "2000-01-01")
            article = _insert_article(session, cover_file="/covers/gone.png")
        (articles_dir / f"{article.id}.txt").write_text("H\n\nB",
                                                        encoding="utf-8")
        (covers_dir / "unrelated.png").write_bytes(b"png")

        schema._reset_daily_workspace()  # must not raise

        assert not (articles_dir / f"{article.id}.txt").exists()
        assert not (covers_dir / "gone.png").exists()  # never existed anyway
        # Orphaned covers are wiped too: the reset guarantees a clean slate.
        assert not (covers_dir / "unrelated.png").exists()
        assert covers_dir.is_dir()  # recreated empty, not removed
        assert articles_dir.is_dir()


# ---------------------------------------------------------------------------
# init_db (public startup path)
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_new_day_wipes_articles_and_files(self, reset_env):
        session_factory, articles_dir, covers_dir = reset_env
        with session_factory() as session:
            _set_last_run(session, "2000-01-01")
            article = _insert_article(
                session, status=STATUS_PUBLISHING, cover_file="/covers/x.png"
            )
        (articles_dir / f"{article.id}.txt").write_text("H\n\nB",
                                                        encoding="utf-8")
        (covers_dir / "x.png").write_bytes(b"png")

        schema.init_db()

        with session_factory() as session:
            assert session.query(Article).count() == 0
        assert not (articles_dir / f"{article.id}.txt").exists()
        assert not (covers_dir / "x.png").exists()

    def test_same_day_requeues_stale_publishing_claims(self, reset_env):
        """Same-day restart: rows stay, a crashed publish claim is requeued."""
        session_factory, articles_dir, covers_dir = reset_env
        with session_factory() as session:
            _set_last_run(session, _today())
            stale = _insert_article(
                session, status=STATUS_PUBLISHING, cover_file="/covers/y.png"
            )
            _insert_article(session, status=STATUS_ACCEPTED, title="Queued")
        (covers_dir / "y.png").write_bytes(b"png")

        schema.init_db()

        with session_factory() as session:
            row = session.get(Article, stale.id)
            assert row.status == STATUS_ACCEPTED
            assert session.query(Article).count() == 2
        # Same day: no cleanup, the cover survives for the requeued publish.
        assert (covers_dir / "y.png").exists()
