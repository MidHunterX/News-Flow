"""Persistence layer for News Flow (SQLAlchemy 2.0 + SQLite).

Public data-access functions are async and run each blocking call in a worker
thread via :func:`app.db.engine.run_in_thread` so the event loop never blocks.

Modules:
    engine      — engine, session factory, declarative base
    models      — SQLAlchemy ORM models (Article, Setting)
    constants   — shared defaults and status constants
    schema      — table creation / migrations / init_db
    articles    — data access for cached articles
    settings    — data access for app settings
"""

from app.db.articles import (get_accepted_items, get_article_by_id,
                             get_cached_sources, get_completed_items,
                             get_due_articles, get_items, get_pending_items,
                             get_rejected_items, mark_articles_completed,
                             save_items, set_article_status, trim_articles,
                             update_article_cover_file)
from app.db.constants import (DEFAULT_SETTINGS, LAST_RUN_DATE_KEY,
                              STATUS_ACCEPTED, STATUS_COMPLETED,
                              STATUS_REJECTED)
from app.db.engine import DB_PATH, Base, SessionLocal, engine
from app.db.models import Article, Setting
from app.db.schema import init_db
from app.db.settings import (get_all_settings, get_completion_interval,
                             get_setting, set_setting)

__all__ = [
    "DEFAULT_SETTINGS",
    "LAST_RUN_DATE_KEY",
    "STATUS_ACCEPTED",
    "STATUS_COMPLETED",
    "STATUS_REJECTED",
    # Engine / session / models (useful for tests and ad-hoc queries).
    "DB_PATH",
    "Base",
    "SessionLocal",
    "engine",
    "Article",
    "Setting",
    # Schema.
    "init_db",
    # Articles.
    "save_items",
    "trim_articles",
    "get_items",
    "get_pending_items",
    "get_accepted_items",
    "get_completed_items",
    "get_rejected_items",
    "set_article_status",
    "get_article_by_id",
    "get_due_articles",
    "mark_articles_completed",
    "get_cached_sources",
    "update_article_cover_file",
    # Settings.
    "get_setting",
    "set_setting",
    "get_all_settings",
    "get_completion_interval",
]
