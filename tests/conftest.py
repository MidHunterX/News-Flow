"""Pytest fixtures for scraper integration tests.

Resets the singleton HttpClient between tests so each async test
gets a fresh client bound to its own event loop, and binds every
``app.db.*`` module to a throwaway SQLite database so the real
``newsflow.db`` is never touched by the test suite.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.client import HttpClient


@pytest.fixture(autouse=True)
def _reset_http_client():
    """Ensure every test starts with a fresh HTTP client."""
    HttpClient._instance = None
    yield
    HttpClient._instance = None


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Bind every app.db module's SessionLocal to a throwaway SQLite DB.

    Each ``app.db.*`` module holds its own ``from app.db.engine import
    SessionLocal`` reference, so patching a single module (as per-test
    fixtures do) leaves the others — notably ``app.db.notifications`` —
    writing to the real ``newsflow.db``. This autouse net rebinds them all
    first; per-test fixtures can still override individual modules after
    (later monkeypatch setattr wins, and teardown restores in reverse).
    """
    import sys

    import app.db as db_pkg
    import app.db.articles as articles_mod
    import app.db.notifications as notifications_mod
    import app.db.schema as schema_mod
    import app.db.settings as settings_mod
    import app.db.wp_terms as wp_terms_mod
    from app.db.models import Base

    # NB: app/db/__init__.py does `from app.db.engine import engine`, so the
    # package attribute `app.db.engine` is the Engine object, shadowing the
    # submodule. sys.modules still has the real module.
    engine_mod = sys.modules["app.db.engine"]

    engine = create_engine(
        f"sqlite:///{tmp_path / 'conftest.db'}",
        # Same as the production engine: DB helpers run blocking calls in
        # worker threads via asyncio.to_thread.
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    for mod in (engine_mod, db_pkg, articles_mod, notifications_mod,
                schema_mod, settings_mod, wp_terms_mod):
        monkeypatch.setattr(mod, "SessionLocal", session_factory)
        # Only schema and the package __init__ import `engine` itself.
        monkeypatch.setattr(mod, "engine", engine, raising=False)
