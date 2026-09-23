"""Offline tests for the article-layout setting.

Covers the validated settings getter (bound to a throwaway SQLite DB), the
settings API validation, and the template branch each layout renders.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.settings as settings_mod
import app.routes.scrape as scrape_routes
from app.db.constants import DEFAULT_SETTINGS
from app.db.models import Base, Setting

LAYOUTS = ("grid", "rows", "compact")


# ---------------------------------------------------------------------------
# get_article_layout (DB-backed)
# ---------------------------------------------------------------------------


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Bind app.db.settings to a throwaway SQLite database."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(settings_mod, "SessionLocal", session_factory)
    return session_factory


def _seed_layout(session, value: str) -> None:
    session.add(Setting(key="article_layout", value=value))
    session.commit()


class TestGetArticleLayout:
    async def test_returns_stored_layout(self, db):
        with db() as session:
            _seed_layout(session, "rows")
        assert await settings_mod.get_article_layout() == "rows"

    async def test_defaults_when_unseeded(self, db):
        assert await settings_mod.get_article_layout() == DEFAULT_SETTINGS[
            "article_layout"
        ]

    async def test_invalid_stored_value_falls_back(self, db):
        with db() as session:
            _seed_layout(session, "bogus")
        assert await settings_mod.get_article_layout() == DEFAULT_SETTINGS[
            "article_layout"
        ]


# ---------------------------------------------------------------------------
# Settings API validation
# ---------------------------------------------------------------------------


class TestSettingsApi:
    @pytest.fixture
    def recorded(self, monkeypatch):
        """Stub the settings data layer used by the routes; record writes."""
        writes: dict[str, str] = {}

        async def fake_set_setting(key: str, value: str) -> None:
            writes[key] = value

        async def fake_get_all_settings() -> dict[str, str]:
            return dict(writes)

        monkeypatch.setattr(scrape_routes, "set_setting", fake_set_setting)
        monkeypatch.setattr(
            scrape_routes, "get_all_settings", fake_get_all_settings
        )
        return writes

    async def test_valid_layout_is_accepted(self, recorded):
        result = await scrape_routes.update_settings({"article_layout": "compact"})
        assert recorded == {"article_layout": "compact"}
        assert result == {"article_layout": "compact"}

    async def test_invalid_layout_rejected(self, recorded):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"article_layout": "spiral"})
        assert excinfo.value.status_code == 400
        assert "article_layout must be one of" in excinfo.value.detail
        assert recorded == {}  # nothing was written

    async def test_unknown_setting_still_rejected(self, recorded):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"nope": "1"})
        assert excinfo.value.status_code == 400

    async def test_interval_validation_untouched(self, recorded):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"completion_interval": "0"})
        assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# Template layout branches
# ---------------------------------------------------------------------------


def _item(idx: int, **overrides) -> SimpleNamespace:
    defaults = dict(
        id=idx,
        title=f"Article {idx}",
        url=f"https://example.com/{idx}",
        image_url=f"https://example.com/{idx}.jpg",
        description="desc",
        published_at="2026-09-19",
        source="kaumudi",
        status=None,
        accepted_at=None,
        cover_file=None,
        wp_category_ids=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _render(layout: str, items: list) -> str:
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("index.html")
    return template.render(
        items=items,
        accepted_items=[],
        completed_items=[],
        rejected_items=[],
        sources=["kaumudi"],
        selected_source=None,
        count=len(items),
        completion_interval=600,
        article_layout=layout,
    )


class TestTemplateLayouts:
    @pytest.mark.parametrize("layout", LAYOUTS)
    def test_each_layout_renders_its_branch(self, layout):
        html = _render(layout, [_item(1), _item(2)])
        assert f"{layout.upper()} layout" in html
        assert "/api/articles/1/accept" in html

    def test_unknown_layout_renders_grid_branch(self):
        html = _render("bogus", [_item(1)])
        assert "GRID layout" in html

    def test_rows_layout_keeps_sidebar_panel(self):
        html = _render("rows", [_item(1)])
        assert "Review &amp; Status" in html
