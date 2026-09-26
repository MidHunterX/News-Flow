"""Offline tests for WordPress terms sync (app/wordpress.py).

HTTP is faked with the FakeClient pattern (no network); DB-backed tests bind
``app.db`` session factory to a throwaway SQLite database.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.wordpress as wp
from app import publisher
from app.db.constants import WP_TERMS_SYNCED_KEY
from app.db.models import Base, Setting, WpCategory

WP_BASE = "https://wp.example.com"


class FakeResponse:
    def __init__(self, json_data=None, status_code: int = 200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeClient:
    """Serves canned per-URL responses; records every GET."""

    def __init__(self, responses: dict[str, list[FakeResponse]]):
        self.responses = responses
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs) -> FakeResponse:
        self.urls.append(url)
        queue = self.responses.get(url)
        if not queue:
            raise AssertionError(f"Unexpected GET {url}")
        return queue.pop(0)


@pytest.fixture
def wp_url(monkeypatch):
    """Point the wordpress module's site URL at the fake (never the real .env)."""
    monkeypatch.setattr(wp, "WORDPRESS_URL", WP_BASE)
    return monkeypatch


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Bind the whole app.db package to a throwaway SQLite database."""
    # NOTE: app.db.__init__ shadows the 'engine' submodule name with the
    # engine *instance*, so resolve the module via importlib.
    engine_mod = importlib.import_module("app.db.engine")

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(engine_mod, "SessionLocal", session_factory)
    # wp_terms/settings/articles hold `from ... import SessionLocal` refs.
    import app.db.settings as settings_mod
    import app.db.wp_terms as wp_terms_mod

    monkeypatch.setattr(settings_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(wp_terms_mod, "SessionLocal", session_factory)
    return session_factory


def _terms(n: int, prefix: str = "cat") -> list[dict]:
    return [
        {"id": i, "name": f"{prefix.title()} {i}", "slug": f"{prefix}-{i}"}
        for i in range(1, n + 1)
    ]


def _client_for(categories: list[dict], tags: list[dict]) -> FakeClient:
    return FakeClient({
        f"{WP_BASE}/wp-json/wp/v2/categories?per_page=100&page=1":
            [FakeResponse(categories)],
        f"{WP_BASE}/wp-json/wp/v2/tags?per_page=100&page=1":
            [FakeResponse(tags)],
    })


# ---------------------------------------------------------------------------
# fetch_terms pagination
# ---------------------------------------------------------------------------


class TestFetchTerms:
    async def test_single_page(self, wp_url, monkeypatch):
        client = FakeClient({
            f"{WP_BASE}/wp-json/wp/v2/categories?per_page=100&page=1":
                [FakeResponse(_terms(3))],
        })
        terms = await wp.fetch_terms(client, "category")
        assert [t["id"] for t in terms] == [1, 2, 3]

    async def test_paginates_until_short_page(self, wp_url, monkeypatch):
        client = FakeClient({
            f"{WP_BASE}/wp-json/wp/v2/categories?per_page=100&page=1":
                [FakeResponse([{"id": i} for i in range(1, 101)])],
            f"{WP_BASE}/wp-json/wp/v2/categories?per_page=100&page=2":
                [FakeResponse([{"id": i} for i in range(101, 201)])],
            f"{WP_BASE}/wp-json/wp/v2/categories?per_page=100&page=3":
                [FakeResponse([{"id": 201}])],
        })
        # Pages 1 and 2 are full (100 each), page 3 short -> stop.
        terms = await wp.fetch_terms(client, "category")
        assert len(terms) == 201

    async def test_stops_on_invalid_page_error(self, wp_url, monkeypatch):
        client = FakeClient({
            f"{WP_BASE}/wp-json/wp/v2/categories?per_page=100&page=1":
                [FakeResponse([{"id": i} for i in range(1, 101)])],
            f"{WP_BASE}/wp-json/wp/v2/categories?per_page=100&page=2":
                [FakeResponse({"code": "rest_post_invalid_page_number"},
                              status_code=400)],
        })
        terms = await wp.fetch_terms(client, "category")
        assert len(terms) == 100

    async def test_http_error_propagates(self, wp_url):
        client = FakeClient({
            f"{WP_BASE}/wp-json/wp/v2/categories?per_page=100&page=1":
                [FakeResponse(status_code=500)],
        })
        with pytest.raises(httpx.HTTPError):
            await wp.fetch_terms(client, "category")


# ---------------------------------------------------------------------------
# sync_terms + TTL staleness
# ---------------------------------------------------------------------------


class TestSyncTerms:
    async def test_upserts_both_taxonomies_and_stamps_sync(
        self, db, wp_url, monkeypatch
    ):
        client = _client_for(_terms(3), _terms(2, prefix="tag"))

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        await wp.sync_terms()

        with db() as session:
            cats = session.scalars(
                select(WpCategory).where(WpCategory.taxonomy == "category")
            ).all()
            tags = session.scalars(
                select(WpCategory).where(WpCategory.taxonomy == "post_tag")
            ).all()
            stamp = session.get(Setting, WP_TERMS_SYNCED_KEY)
        assert sorted(c.wp_id for c in cats) == [1, 2, 3]
        assert sorted(t.wp_id for t in tags) == [1, 2]
        assert cats[0].name == "Cat 1" and cats[0].slug == "cat-1"
        assert stamp is not None and stamp.value

    async def test_updates_and_prunes_stale_terms(
        self, db, wp_url, monkeypatch
    ):
        with db() as session:
            session.add(WpCategory(wp_id=1, taxonomy="category",
                                   name="Old Name", slug="old"))
            session.add(WpCategory(wp_id=99, taxonomy="category",
                                   name="Deleted On Site", slug="gone"))
            session.commit()

        client = _client_for([{"id": 1, "name": "New Name", "slug": "new"}], [])

        async def fake_get_client():
            return client

        monkeypatch.setattr("app.client.get_client", fake_get_client)
        await wp.sync_terms()

        with db() as session:
            cats = session.scalars(
                select(WpCategory).where(WpCategory.taxonomy == "category")
            ).all()
        assert [(c.wp_id, c.name) for c in cats] == [(1, "New Name")]

    async def test_sync_skipped_when_fresh(self, db, monkeypatch):
        with db() as session:
            session.add(Setting(
                key=WP_TERMS_SYNCED_KEY,
                value=datetime.now(UTC).isoformat(timespec="seconds"),
            ))
            session.commit()

        async def fail_sync():
            raise AssertionError("should not sync")

        monkeypatch.setattr(wp, "sync_terms", fail_sync)
        assert await wp.sync_terms_if_stale() is False

    async def test_sync_runs_when_stale_or_missing(self, db, monkeypatch):
        ran = []

        async def fake_sync():
            ran.append(True)

        monkeypatch.setattr(wp, "sync_terms", fake_sync)
        assert await wp.sync_terms_if_stale() is True
        assert ran == [True]

    async def test_sync_failure_is_swallowed(self, db, monkeypatch):
        async def fake_sync():
            raise httpx.ConnectError("down")

        monkeypatch.setattr(wp, "sync_terms", fake_sync)
        assert await wp.sync_terms_if_stale() is True  # ran, but fail-soft

    async def test_skips_entirely_when_wp_unconfigured(self, db, monkeypatch):
        monkeypatch.setattr(publisher, "WORDPRESS_URL", "")
        monkeypatch.setattr(publisher, "WORDPRESS_USERNAME", "")
        monkeypatch.setattr(publisher, "WORDPRESS_APP_PASSWORD", "")

        async def fail_sync():
            raise AssertionError("should not sync")

        monkeypatch.setattr(wp, "sync_terms", fail_sync)
        assert await wp.sync_terms_if_stale() is False


# ---------------------------------------------------------------------------
# ensure_terms
# ---------------------------------------------------------------------------


class TestEnsureTerms:
    async def test_true_when_categories_exist(self, db, monkeypatch):
        with db() as session:
            session.add(WpCategory(wp_id=5, taxonomy="category",
                                   name="News", slug="news"))
            session.commit()

        async def fail_sync():
            raise AssertionError("fresh terms must not re-sync")

        monkeypatch.setattr(wp, "sync_terms", fail_sync)
        monkeypatch.setattr(wp, "_terms_stale_from", lambda _: False)
        assert await wp.ensure_terms() is True

    async def test_false_when_no_categories(self, db, monkeypatch):
        monkeypatch.setattr(wp, "_terms_stale_from", lambda _: False)
        assert await wp.ensure_terms() is False
