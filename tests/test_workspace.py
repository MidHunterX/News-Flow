"""Offline tests for the database-scoped workspace paths (app.utils).

Every scraped-content/cover/publication dir lives under ``public/<db name>/``
where ``<db name>`` is the stem of the active database file. That scoping is
what keeps a temporary ``NEWSFLOW_DB_PATH`` from triggering the daily
workspace cleanup against the main database's files: each database only ever
resets its own tree.
"""

from __future__ import annotations

from app.config import DEFAULT_DB_PATH, database_path
from app.utils import (
    ARTICLES_DIR,
    COVERS_DIR,
    DB_NAME,
    PUBLIC_DIR,
    PUBLICATION_ARTICLES_DIR,
    PUBLICATION_COVERS_DIR,
    WORKSPACE_DIR,
)


def test_workspace_dirs_are_named_after_the_active_database():
    """All four workspace dirs hang off public/<db name>/, not public/."""
    assert DB_NAME == database_path().stem
    assert WORKSPACE_DIR == PUBLIC_DIR / DB_NAME
    assert ARTICLES_DIR == PUBLIC_DIR / DB_NAME / "articles"
    assert COVERS_DIR == PUBLIC_DIR / DB_NAME / "covers"
    assert PUBLICATION_ARTICLES_DIR == (
        PUBLIC_DIR / DB_NAME / "publication" / "articles"
    )
    assert PUBLICATION_COVERS_DIR == (
        PUBLIC_DIR / DB_NAME / "publication" / "covers"
    )


def test_database_path_honors_the_env_override(monkeypatch, tmp_path):
    """NEWSFLOW_DB_PATH wins, and its file name is what scopes the workspace."""
    monkeypatch.setenv("NEWSFLOW_DB_PATH", str(tmp_path / "scratch.db"))

    assert database_path() == tmp_path / "scratch.db"
    # The stem is the workspace directory name: a scratch DB would write to
    # public/scratch/…, never to the default database's public/newsflow/….
    assert database_path().stem == "scratch"


def test_default_database_path_is_the_project_db(monkeypatch):
    """Without an override the default <project root>/newsflow.db is used."""
    monkeypatch.delenv("NEWSFLOW_DB_PATH", raising=False)

    assert database_path() == DEFAULT_DB_PATH
    assert database_path().name == "newsflow.db"
