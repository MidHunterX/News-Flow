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
    notifications — data access for the UI notification log
"""

from app.db.articles import (get_accepted_items, get_article_by_id,
                             get_cached_sources, get_completed_items,
                             get_due_articles, get_items, get_pending_items,
                             get_rejected_items, mark_articles_completed,
                             save_items, set_article_status, trim_articles,
                             update_article_cover_file)
from app.db.constants import (ARTICLE_LAYOUTS, DEFAULT_SETTINGS, LAST_RUN_DATE_KEY,
                              STATUS_ACCEPTED, STATUS_COMPLETED,
                              STATUS_PUBLISHING, STATUS_REJECTED)
from app.db.engine import DB_PATH, Base, SessionLocal, engine
from app.db.models import Article, Notification, Setting
from app.db.schema import init_db
from app.db.notifications import (clear_notifications, get_notifications,
                                  record_notification)
from app.db.settings import (get_all_settings, get_article_layout,
                             get_completion_interval, get_setting, set_setting)
from app.db.wp_terms import (get_categories, get_last_terms_sync, get_tags,
                             set_article_category_ids, set_last_terms_sync,
                             upsert_terms)

__all__ = [
    "DEFAULT_SETTINGS",
    "ARTICLE_LAYOUTS",
    "LAST_RUN_DATE_KEY",
    "STATUS_ACCEPTED",
    "STATUS_PUBLISHING",
    "STATUS_COMPLETED",
    "STATUS_REJECTED",
    # Engine / session / models (useful for tests and ad-hoc queries).
    "DB_PATH",
    "Base",
    "SessionLocal",
    "engine",
    "Article",
    "Setting",
    "Notification",
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
    "get_article_layout",
    # WordPress terms (categories + tags).
    "get_categories",
    "get_tags",
    "upsert_terms",
    "set_article_category_ids",
    "get_last_terms_sync",
    "set_last_terms_sync",
    # Notification log (surfaced in the UI).
    "record_notification",
    "get_notifications",
    "clear_notifications",
]
