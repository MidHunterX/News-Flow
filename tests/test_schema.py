"""Offline tests for the daily workspace reset (app/db/schema.py).

Binds app.db.schema to a throwaway SQLite database and temp workspace dirs,
so the real newsflow.db / public/ files are never touched (no network).
Covers both halves of the rollover: the publication workspace that receives
the archive, and the curation workspace that is wiped clean.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import schema
from app.db.constants import LAST_RUN_DATE_KEY, STATUS_ACCEPTED, STATUS_PUBLISHING
from app.db.models import Article, Base, PublicationArticle, Setting

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

    # Publication workspace: deliberately left absent so the reset's
    # create-if-missing behaviour is exercised.
    monkeypatch.setattr(
        schema, "PUBLICATION_ARTICLES_DIR", tmp_path / "publication" / "articles"
    )
    monkeypatch.setattr(
        schema, "PUBLICATION_COVERS_DIR", tmp_path / "publication" / "covers"
    )
    return session_factory, articles_dir, covers_dir


@pytest.fixture
def pub_dirs(reset_env):
    """Temp publication-workspace dirs, read after ``reset_env`` patched them.

    Declared as a dependent so the patches are in place; tests that don't
    inspect the archive never request it.
    """
    return schema.PUBLICATION_ARTICLES_DIR, schema.PUBLICATION_COVERS_DIR


def _set_last_run(session, value: str) -> None:
    session.add(Setting(key=LAST_RUN_DATE_KEY, value=value))
    session.commit()


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _insert_article(session, **overrides) -> Article:
    defaults = {
        "title": "First Article",
        "url": "https://example.com/a",
        "image_url": None,
        "description": "desc",
        "published_at": "2026-09-19",
        "source": "kaumudi",
    }
    defaults.update(overrides)
    article = Article(**defaults)
    session.add(article)
    session.commit()
    return article


def _insert_publication_article(session, **overrides) -> PublicationArticle:
    defaults = {
        "title": "Archived Article",
        "url": "https://example.com/archived",
        "image_url": None,
        "description": "desc",
        "published_at": "2026-09-18",
        "source": "kaumudi",
    }
    defaults.update(overrides)
    article = PublicationArticle(**defaults)
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
# Publication workspace (the archive the reset mirrors into)
# ---------------------------------------------------------------------------


class TestPublicationArchive:
    """A new day moves the curation workspace over instead of losing it."""

    def test_new_day_moves_rows_to_publication_table(self, reset_env):
        """Rows land in publication_articles verbatim — ids included — while
        the curation table goes back to empty."""
        session_factory, _articles_dir, _covers_dir = reset_env
        with session_factory() as session:
            _set_last_run(session, "2000-01-01")
            first = _insert_article(session)
            second = _insert_article(session, title="Second")
            expected_ids = {first.id, second.id}

        schema._reset_daily_workspace()

        with session_factory() as session:
            archived = session.query(PublicationArticle).all()
            assert {row.id for row in archived} == expected_ids
            assert {row.title for row in archived} == {"First Article", "Second"}
            assert session.query(Article).count() == 0

    def test_new_day_copies_files_to_publication_workspace(
        self, reset_env, pub_dirs
    ):
        """Content and cover files are mirrored across, then wiped locally;
        a stale file from an older archive does not survive."""
        session_factory, articles_dir, covers_dir = reset_env
        pub_articles_dir, pub_covers_dir = pub_dirs
        with session_factory() as session:
            _set_last_run(session, "2000-01-01")
            article = _insert_article(session, cover_file="/covers/one.png")
        (articles_dir / f"{article.id}.txt").write_text("H\n\nbody words here",
                                                        encoding="utf-8")
        (covers_dir / "one.png").write_bytes(b"png")
        pub_covers_dir.mkdir(parents=True)
        (pub_covers_dir / "stale.png").write_bytes(b"old")

        schema._reset_daily_workspace()

        assert (
            (pub_articles_dir / f"{article.id}.txt").read_text(encoding="utf-8")
            == "H\n\nbody words here"
        )
        assert (pub_covers_dir / "one.png").read_bytes() == b"png"
        # The archive starts fresh each day: last time's leftovers go.
        assert not (pub_covers_dir / "stale.png").exists()
        # The curation side is still wiped clean, exactly as before.
        assert not (articles_dir / f"{article.id}.txt").exists()
        assert not (covers_dir / "one.png").exists()

    def test_same_day_leaves_publication_workspace_alone(self, reset_env, pub_dirs):
        """A same-day restart must not replace (or empty) the archive."""
        session_factory, _articles_dir, _covers_dir = reset_env
        pub_articles_dir, pub_covers_dir = pub_dirs
        with session_factory() as session:
            _set_last_run(session, _today())
            _insert_publication_article(session)
        pub_articles_dir.mkdir(parents=True)
        (pub_articles_dir / "7.txt").write_text("H\n\nB", encoding="utf-8")
        pub_covers_dir.mkdir(parents=True)
        (pub_covers_dir / "keep.png").write_bytes(b"png")

        schema._reset_daily_workspace()

        with session_factory() as session:
            assert session.query(PublicationArticle).count() == 1
        assert (pub_articles_dir / "7.txt").exists()
        assert (pub_covers_dir / "keep.png").exists()

    def test_new_day_replaces_a_stale_archive(self, reset_env):
        """Yesterday's archive is swapped out, never appended to."""
        session_factory, _articles_dir, _covers_dir = reset_env
        with session_factory() as session:
            _set_last_run(session, "2000-01-01")
            _insert_publication_article(session, title="Oldest day")
            _insert_article(session, title="Today's article")

        schema._reset_daily_workspace()

        with session_factory() as session:
            archived = session.query(PublicationArticle).all()
            assert [row.title for row in archived] == ["Today's article"]

    def test_missing_curation_dirs_are_created(self, reset_env, monkeypatch, tmp_path):
        """A pristine checkout (no workspace dirs at all) still resets cleanly."""
        session_factory, _articles_dir, _covers_dir = reset_env
        monkeypatch.setattr(schema, "ARTICLES_DIR", tmp_path / "absent" / "articles")
        monkeypatch.setattr(schema, "COVERS_DIR", tmp_path / "absent" / "covers")
        with session_factory() as session:
            _set_last_run(session, "2000-01-01")

        schema._reset_daily_workspace()  # must not raise

        assert (tmp_path / "absent" / "articles").is_dir()
        assert (tmp_path / "absent" / "covers").is_dir()


# ---------------------------------------------------------------------------
# init_db (public startup path)
# ---------------------------------------------------------------------------


class TestResetTimezone:
    """The reset clock honors the reset_timezone setting (utc vs local)."""

    @staticmethod
    def _seed(session, **settings) -> None:
        for key, value in settings.items():
            session.add(Setting(key=key, value=value))
        session.commit()

    @staticmethod
    def _fake_clock(monkeypatch, utc_today: str, local_tomorrow: str) -> None:
        """Stub _reset_now_date: utc date stays fixed, local rolls over."""
        dates = {"utc": utc_today, "local": local_tomorrow}
        monkeypatch.setattr(
            schema, "_reset_now_date", lambda mode: dates.get(mode, utc_today)
        )

    def test_utc_mode_uses_utc_clock(self, reset_env, monkeypatch):
        """utc mode (the default): a same-UTC-day last run is not reset even
        when the local date has already rolled over."""
        session_factory, _articles_dir, _covers_dir = reset_env
        with session_factory() as session:
            self._seed(session, reset_timezone="utc")
            _set_last_run(session, "2026-10-09")
            _insert_article(session)

        self._fake_clock(monkeypatch, "2026-10-09", "2026-10-10")
        schema._reset_daily_workspace()

        with session_factory() as session:
            assert session.query(Article).count() == 1
            assert session.get(Setting, LAST_RUN_DATE_KEY).value == "2026-10-09"

    def test_local_mode_uses_local_clock(self, reset_env, monkeypatch):
        """local mode: a last run stamped with the UTC date still resets once
        the local date has rolled over, and the stamp switches to local."""
        session_factory, _articles_dir, _covers_dir = reset_env
        with session_factory() as session:
            self._seed(session, reset_timezone="local")
            _set_last_run(session, "2026-10-09")
            _insert_article(session)

        self._fake_clock(monkeypatch, "2026-10-09", "2026-10-10")
        schema._reset_daily_workspace()

        with session_factory() as session:
            assert session.query(Article).count() == 0
            assert session.get(Setting, LAST_RUN_DATE_KEY).value == "2026-10-10"

    def test_unknown_stored_mode_falls_back_to_utc(self, reset_env, monkeypatch):
        """A bogus stored value behaves like utc, not like local."""
        session_factory, _articles_dir, _covers_dir = reset_env
        with session_factory() as session:
            self._seed(session, reset_timezone="bogus")
            _set_last_run(session, "2026-10-09")
            _insert_article(session)

        self._fake_clock(monkeypatch, "2026-10-09", "2026-10-10")
        schema._reset_daily_workspace()

        with session_factory() as session:
            assert session.query(Article).count() == 1
            assert session.get(Setting, LAST_RUN_DATE_KEY).value == "2026-10-09"

    def test_helper_unknown_mode_behaves_as_utc(self):
        """_reset_now_date maps anything unknown to the UTC clock."""
        assert schema._reset_now_date("bogus") == schema._reset_now_date("utc")

    def test_helper_local_mode_uses_local_now(self):
        """local mode compares against the server's local calendar date."""
        local_now = datetime.now().astimezone()  # local tz, per DTZ005's rule
        assert schema._reset_now_date("local") == local_now.date().isoformat()


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
        session_factory, _articles_dir, covers_dir = reset_env
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
