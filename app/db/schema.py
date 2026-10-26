"""Table creation, schema migrations, and startup initialization."""

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, insert, inspect, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.constants import (
    DEFAULT_SETTINGS,
    LAST_RUN_DATE_KEY,
    NOTIF_INFO,
    STATUS_ACCEPTED,
    STATUS_PUBLISHING,
)
from app.db.engine import Base, SessionLocal, engine
from app.db.models import Article, Notification, PublicationArticle, Setting
from app.utils import (
    ARTICLES_DIR,
    COVERS_DIR,
    PUBLICATION_ARTICLES_DIR,
    PUBLICATION_COVERS_DIR,
)

logger = logging.getLogger(__name__)


def _reset_now_date(tz_mode: str) -> str:
    """Today's date per the configured reset clock ("utc" or "local")."""
    now = datetime.now(UTC if tz_mode != "local" else None)
    return now.date().isoformat()


def _migrate() -> None:
    """Add columns introduced after the initial schema to existing databases.

    Runs against both article tables: the publication one is a mirror of the
    curation one, and ``create_all`` never alters an existing table.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in ("articles", "publication_articles"):
        if table not in existing_tables:
            continue
        columns = {col["name"] for col in inspector.get_columns(table)}
        with engine.begin() as conn:
            if "status" not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN status TEXT"))
            if "accepted_at" not in columns:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN accepted_at TEXT")
                )
            if "accepted_order" not in columns:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN accepted_order INTEGER")
                )
            if "cover_file" not in columns:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN cover_file TEXT")
                )
            if "wp_category_ids" not in columns:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN wp_category_ids JSON")
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


def _copy_dir(src: Path, dst: Path) -> None:
    """Copy every entry under *src* into *dst* (created when missing).

    Mirrors the curation workspace into the publication workspace at the
    daily reset: content ``.txt`` files and cover images are duplicated, not
    moved, so the curation workspace can be wiped right afterwards. A
    missing *src* is a no-op; per-entry failures are logged and skipped so
    one bad file can't abort the reset.
    """
    dst.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return
    for child in src.iterdir():
        target = dst / child.name
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.copytree(child, target, dirs_exist_ok=True)
            else:
                shutil.copy2(child, target)
        except OSError:
            logger.warning("Could not copy workspace file %s to %s", child,
                           target, exc_info=True)


def _reset_daily_workspace() -> None:
    """Archive the curation workspace to publication, then start a fresh one.

    The daily rollover runs in three steps: the publication workspace (the
    ``publication_articles`` rows plus the files under
    ``public/publication/``) is cleared, everything currently in the
    curation workspace is copied over there, and only then is the curation
    workspace cleared. Curation keeps its clean-slate philosophy — nothing
    from the previous day leaks into today's queue — while the previous
    day's data stays available to the print-publication pipeline behind
    ``/publication``.

    Order matters for crash safety: the files are copied before any row is
    touched (worst case is duplicated files, never an archived row without
    its content), the row move shares a transaction with the
    ``last_run_date`` stamp, and the curation wipe happens after that
    commit — losing a row but keeping its files is the safe failure mode.
    The "new day" boundary follows the ``reset_timezone`` setting — UTC by
    default, or the server's local time zone.
    """
    with SessionLocal() as session:
        tz_setting = session.get(Setting, "reset_timezone")
        tz_mode = (
            tz_setting.value
            if tz_setting is not None else DEFAULT_SETTINGS["reset_timezone"]
        )
        today = _reset_now_date(tz_mode)
        last_run = session.get(Setting, LAST_RUN_DATE_KEY)
        if last_run is not None and last_run.value == today:
            return  # Same day: nothing to reset.

        # 1. Fresh publication workspace: the last day's archive is replaced.
        _clear_dir(PUBLICATION_ARTICLES_DIR)
        _clear_dir(PUBLICATION_COVERS_DIR)

        # 2. Mirror the curation workspace across before deleting anything.
        _copy_dir(ARTICLES_DIR, PUBLICATION_ARTICLES_DIR)
        _copy_dir(COVERS_DIR, PUBLICATION_COVERS_DIR)

        # 3. Move the rows across (verbatim ids) and open the new day in one
        #    transaction, so the archive can never hold half the workspace.
        session.execute(delete(PublicationArticle))
        columns = [column.name for column in Article.__table__.columns]
        session.execute(
            insert(PublicationArticle).from_select(
                columns,
                select(*(Article.__table__.c[name] for name in columns)),
            )
        )
        session.execute(delete(Article))
        if last_run is None:
            session.add(Setting(key=LAST_RUN_DATE_KEY, value=today))
        else:
            last_run.value = today
        session.commit()

    # 4. The curation workspace gets its clean slate.
    for workspace_dir in (ARTICLES_DIR, COVERS_DIR):
        _clear_dir(workspace_dir)

    # A fresh day is an event worth surfacing in the UI's notification bell.
    try:
        with SessionLocal() as session:
            session.add(
                Notification(
                    level=NOTIF_INFO,
                    source="system",
                    message=(
                        "Daily workspace reset: previous day archived to the "
                        "publication workspace, curation workspace cleared."
                    ),
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
