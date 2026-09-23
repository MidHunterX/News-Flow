"""Table creation, schema migrations, and startup initialization."""

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, inspect, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.constants import (DEFAULT_SETTINGS, LAST_RUN_DATE_KEY,
                              STATUS_PUBLISHING, STATUS_ACCEPTED,
                              NOTIF_INFO)
from app.db.engine import Base, SessionLocal, engine
from app.db.models import Article, Notification, Setting
from app.utils import ARTICLES_DIR, COVERS_DIR

logger = logging.getLogger(__name__)


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
        if "cover_file" not in columns:
            conn.execute(text("ALTER TABLE articles ADD COLUMN cover_file TEXT"))
        if "wp_category_ids" not in columns:
            conn.execute(
                text("ALTER TABLE articles ADD COLUMN wp_category_ids JSON")
            )


def _collect_workspace_files(articles: list[Article]) -> list[Path]:
    """Return the local files attached to the given articles, for deletion.

    These are the scraped-content files (``public/articles/<id>.txt``) and any
    downloaded cover referenced by ``cover_file`` (a ``/covers/<name>`` web
    path mapped back into ``public/covers/``). Missing paths are kept so the
    caller can still try unlinking them — ``unlink`` is only attempted when
    the file actually exists.
    """
    files: list[Path] = []
    for article in articles:
        files.append(ARTICLES_DIR / f"{article.id}.txt")
        if article.cover_file:
            files.append(COVERS_DIR / Path(article.cover_file).name)
    return files


def _delete_files(paths: list[Path]) -> None:
    """Unlink the given files, ignoring missing ones and logging failures."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete workspace file %s", path, exc_info=True)


def _reset_daily_workspace() -> None:
    """Clear the articles table and its attached files on a new UTC day.

    Fresh workspace per day: when the last run was on a previous day, every
    article row is dropped together with the files created for it (covers and
    scraped-content .txt). The file deletion happens *after* the row delete is
    committed — losing a row but keeping its files is the safe failure mode,
    and orphaned files are harmless.
    """
    with SessionLocal() as session:
        today = datetime.now(timezone.utc).date().isoformat()
        last_run = session.get(Setting, LAST_RUN_DATE_KEY)
        if last_run is not None and last_run.value == today:
            return  # Same day: nothing to reset.

        # Files are collected before the delete; the rows are gone after
        # commit, so this must happen inside the transaction.
        files = _collect_workspace_files(session.query(Article).all())
        session.execute(delete(Article))
        if last_run is None:
            session.add(Setting(key=LAST_RUN_DATE_KEY, value=today))
        else:
            last_run.value = today
        session.commit()

    _delete_files(files)

    # A fresh day is an event worth surfacing in the UI's notification bell.
    try:
        with SessionLocal() as session:
            session.add(
                Notification(
                    level=NOTIF_INFO,
                    source="system",
                    message="Daily workspace reset: articles and files cleared.",
                )
            )
            session.commit()
    except Exception:
        logger.warning("Could not store daily-reset notification", exc_info=True)


def init_db() -> None:
    """Create the tables/indexes (and migrate) if they don't exist."""
    Base.metadata.create_all(engine)
    _migrate()

    # Runs first and in its own committed transaction: the session below
    # starts writing (settings seeding) immediately after, and SQLite could
    # not serve a second write transaction from here while that one holds an
    # uncommitted one ("database is locked").
    _reset_daily_workspace()

    with SessionLocal() as session:
        for key, value in DEFAULT_SETTINGS.items():
            session.execute(
                sqlite_insert(Setting)
                .values(key=key, value=value)
                .on_conflict_do_nothing()
            )

        # Recover articles left mid-publish by a previous crash/shutdown: no
        # publisher is running yet, so any claim is stale. Requeue them so
        # they get published exactly once instead of being orphaned.
        for article in session.query(Article).filter(
            Article.status == STATUS_PUBLISHING
        ).all():
            article.status = STATUS_ACCEPTED

        session.commit()
