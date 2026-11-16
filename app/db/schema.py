"""Table creation, schema migrations, and startup initialization."""

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, insert, inspect, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.constants import (
    DEFAULT_SETTINGS,
    LAST_RUN_DATE_KEY,
    NOTIF_INFO,
    STATUS_ACCEPTED,
    STATUS_PUBLISHING,
)
from app.db.engine import Base, SessionLocal, engine
from app.db.layout import PUBLICATION_LAYOUT_DEFAULTS
from app.db.models import (
    Article,
    LayoutGeneration,
    Notification,
    PublicationArticle,
    Setting,
)
from app.utils import (
    ARTICLES_DIR,
    COVERS_DIR,
    PUBLICATION_ARTICLES_DIR,
    PUBLICATION_COVERS_DIR,
)

# The four workspace dirs above are scoped to the active database
# (public/<db name>/… — see app.utils), so every path this module clears or
# mirrors belongs to the database the process is running against.

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
            if "is_print_published" not in columns:
                conn.execute(
                    text(
                        f"ALTER TABLE {table} ADD COLUMN is_print_published "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "page_number" not in columns:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN page_number INTEGER")
                )

    # Layout generations table (AI-generated page assignments history)
    if "layout_generations" not in existing_tables:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE layout_generations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "page_assignments JSON NOT NULL,"
                "day TEXT NOT NULL,"
                "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
                ")"
            ))

    # The notification log gained its unread flag with the bell badge; rows
    # that predate it land as unread (DEFAULT 0), so they show up once.
    if "notifications" in existing_tables:
        columns = {col["name"] for col in inspector.get_columns("notifications")}
        if "is_read" not in columns:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE notifications ADD COLUMN "
                        "is_read BOOLEAN NOT NULL DEFAULT 0"
                    )
                )


def _clear_dir(path: Path) -> None:
    """Remove everything inside *path*, recreating it as an empty directory.

    Used for the daily clean slate: the whole workspace (the active
    database's ``articles``/``covers`` dirs under ``public/<db name>/``) is
    wiped, not just files tied to known articles — this
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
    ``public/<db name>/publication/``) is cleared, everything currently in the
    curation workspace is copied over there, and only then is the curation
    workspace cleared. Curation keeps its clean-slate philosophy — nothing
    from the previous day leaks into today's queue — while the previous
    day's data stays available to the print-publication pipeline behind
    ``/publication``. The AI layout generation history is dropped along with
    it: those page assignments belong to a workspace that no longer exists.

    Because every dir here is scoped to the active database (see
    :mod:`app.utils`), a freshly provisioned database — no ``last_run_date``
    stamp yet, e.g. a temporary DB_PATH pointed at for testing — resets only
    its own, empty workspace instead of wiping the main database's files.

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
        # The AI layout history describes a workspace that is now empty, so
        # yesterday's generations are meaningless: each new day starts with
        # no generation history at all (in both the curation and publication
        # tabs). Cleared in the same transaction as the row move so a crash
        # can never leave history pointing at articles that no longer exist.
        session.execute(delete(LayoutGeneration))
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


def _dedupe_existing_urls_sync(session) -> int:
    """Remove duplicate ``url`` rows from today's curation workspace.

    A race between scrapers after a clean-slate reset can insert the same url
    more than once before the unique constraint was in place, or on a DB that
    predates it. This is the one-shot repair that runs on the next startup:
    for every url that appears more than once in ``articles``, keep exactly one
    row (the newest, highest-id one) and delete the rest. Rows without a url
    are left alone — they are still dedeuped by title in
    :func:`app.db.articles._save_items_sync`, which is good enough for the
    rare title-only case and avoids touching rows the constraint does not
    cover.

    The caller holds an open write transaction (the session ``init_db`` opens
    for seeding settings), so this mutates that same transaction rather than
    starting a nested one.

    Returns the number of rows removed.
    """
    removed = 0
    rows = session.execute(
        select(Article.url, func.max(Article.id).label("kept_id"))
        .where(Article.url.isnot(None))
        .group_by(Article.url)
        .having(func.count(Article.id) > 1)
    ).all()
    for (url, kept_id) in rows:
        deleted = session.execute(
            delete(Article).where(
                Article.url == url,
                Article.id != kept_id,
            )
        )
        removed += deleted.rowcount
    return removed


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
        # The print-layout knobs live in their own module (app.db.layout)
        # but are seeded here with the rest, into the same settings table.
        for key, value in PUBLICATION_LAYOUT_DEFAULTS.items():
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

        # Repair any duplicate ``url`` rows the race at clean launch could
        # have inserted before the unique constraint took effect (or on a DB
        # that predates it). Doing it after the reset keeps the repair inside
        # today's fresh workspace, and it is safe to be idempotent — a clean
        # DB touches nothing.
        deduped = _dedupe_existing_urls_sync(session)
        if deduped:
            logger.info(
                "Deduplicated %d duplicate article url(s) in today's workspace",
                deduped,
            )

        session.commit()
