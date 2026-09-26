"""Table creation, schema migrations, and startup initialization."""

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, inspect, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.constants import (
    DEFAULT_SETTINGS,
    LAST_RUN_DATE_KEY,
    NOTIF_INFO,
    STATUS_ACCEPTED,
    STATUS_PUBLISHING,
)
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


def _clear_dir(path: Path) -> None:
    """Remove everything inside *path*, recreating it as an empty directory.

    Used for the daily clean slate: the whole workspace (``public/articles/``,
    ``public/covers/``) is wiped, not just files tied to known articles — this
    also clears orphans whose article rows are already gone. Missing
    directories are created; deletions that fail are logged and skipped.
    """
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete workspace file %s", child,
                           exc_info=True)
    path.mkdir(parents=True, exist_ok=True)


def _reset_daily_workspace() -> None:
    """Clear the articles table and the whole workspace on a new UTC day.

    Fresh workspace per day: when the last run was on a previous day, every
    article row is dropped and the workspace directories (scraped-content
    ``.txt`` files and downloaded covers) are emptied entirely. The wipe
    happens *after* the row delete is committed — losing a row but keeping
    its files is the safe failure mode.
    """
    with SessionLocal() as session:
        today = datetime.now(UTC).date().isoformat()
        last_run = session.get(Setting, LAST_RUN_DATE_KEY)
        if last_run is not None and last_run.value == today:
            return  # Same day: nothing to reset.

        session.execute(delete(Article))
        if last_run is None:
            session.add(Setting(key=LAST_RUN_DATE_KEY, value=today))
        else:
            last_run.value = today
        session.commit()

    for workspace_dir in (ARTICLES_DIR, COVERS_DIR):
        _clear_dir(workspace_dir)

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
