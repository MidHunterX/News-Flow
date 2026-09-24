"""Offline tests for the article-layout setting and feature toggles.

Covers the validated settings getters (bound to a throwaway SQLite DB), the
settings API validation (including the env-gated toggles), and the template
branch each layout renders.
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
from app.db.constants import (DEFAULT_SETTINGS, TOGGLE_ENV_KEYS,
                              TOGGLE_SETTINGS, toggle_is_available)
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
# Feature toggles (ai_auto_categorization / auto_publish)
# ---------------------------------------------------------------------------


class TestToggleHelpers:
    def test_every_toggle_has_env_keys(self):
        assert set(TOGGLE_SETTINGS) == {
            "ai_auto_categorization", "auto_publish"
        }
        assert set(TOGGLE_ENV_KEYS) == set(TOGGLE_SETTINGS)

    def test_availability_tracks_env(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        assert toggle_is_available("ai_auto_categorization") is True
        monkeypatch.delenv("GEMINI_API_KEY")
        assert toggle_is_available("ai_auto_categorization") is False
        # All of auto_publish's vars must be present.
        for name in TOGGLE_ENV_KEYS["auto_publish"]:
            monkeypatch.setenv(name, "v")
        assert toggle_is_available("auto_publish") is True
        monkeypatch.setenv("WORDPRESS_APP_PASSWORD", "   ")  # blank = absent
        assert toggle_is_available("auto_publish") is False

    async def test_get_toggle_defaults_on(self, db):
        assert await settings_mod.get_toggle("auto_publish") is True

    async def test_get_toggle_roundtrip(self, db):
        with db() as session:
            session.add(Setting(key="auto_publish", value="0"))
            session.commit()
        assert await settings_mod.get_toggle("auto_publish") is False

    async def test_get_toggle_invalid_value_falls_back_on(self, db):
        with db() as session:
            session.add(Setting(key="auto_publish", value="maybe"))
            session.commit()
        assert await settings_mod.get_toggle("auto_publish") is True

    async def test_get_all_toggle_states(self, db):
        with db() as session:
            session.add(Setting(key="ai_auto_categorization", value="0"))
            session.commit()
        states = await settings_mod.get_all_toggle_states()
        assert states == {"ai_auto_categorization": False, "auto_publish": True}


class TestToggleApi:
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

    async def test_turning_off_needs_no_env(self, recorded):
        result = await scrape_routes.update_settings({"auto_publish": "0"})
        assert recorded == {"auto_publish": "0"}
        assert result == {"auto_publish": "0"}

    async def test_unknown_toggle_value_rejected(self, recorded):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"auto_publish": "true"})
        assert excinfo.value.status_code == 400
        assert recorded == {}

    async def test_enabling_without_env_rejected(self, recorded, monkeypatch):
        for name in TOGGLE_ENV_KEYS["ai_auto_categorization"]:
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings(
                {"ai_auto_categorization": "1"}
            )
        assert excinfo.value.status_code == 400
        assert "GEMINI_API_KEY" in excinfo.value.detail
        assert recorded == {}

    async def test_enabling_with_env_accepted(self, recorded, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        result = await scrape_routes.update_settings(
            {"ai_auto_categorization": "1"}
        )
        assert recorded == {"ai_auto_categorization": "1"}
        assert result["ai_auto_categorization"] == "1"

    async def test_disabling_missing_toggle_is_accepted(self, recorded):
        """Toggling off never touches env availability — safe with no keys."""
        result = await scrape_routes.update_settings(
            {"ai_auto_categorization": "0"}
        )
        assert recorded == {"ai_auto_categorization": "0"}
        assert result["ai_auto_categorization"] == "0"

    async def test_settings_payload_reports_toggle_state(self, monkeypatch):
        async def fake_states() -> dict[str, bool]:
            return {"ai_auto_categorization": True, "auto_publish": False}

        async def fake_all_settings() -> dict[str, str]:
            return {"completion_interval": "600"}

        monkeypatch.setattr(
            scrape_routes, "get_all_toggle_states", fake_states
        )
        monkeypatch.setattr(
            scrape_routes, "get_all_settings", fake_all_settings
        )
        # Availability mirrors the deployment env; fake it deterministically.
        monkeypatch.setattr(
            scrape_routes,
            "toggle_is_available",
            lambda key: key == "ai_auto_categorization",
        )
        payload = await scrape_routes.get_settings()
        assert payload["completion_interval"] == "600"
        assert payload["toggles"]["ai_auto_categorization"]["enabled"] is True
        assert payload["toggles"]["auto_publish"]["enabled"] is False
        assert payload["toggles"]["auto_publish"]["available"] is False
        assert payload["toggles"]["auto_publish"]["required_env"] == [
            "WORDPRESS_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD"
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
        toggles={},
    )


class TestSettingsModalToggles:
    """The settings modal renders toggle switches with env messaging."""

    def _render_base(self, toggles: dict) -> str:
        env = Environment(
            loader=FileSystemLoader("templates"),
            autoescape=select_autoescape(["html"]),
        )
        template = env.get_template("base.html")
        return template.render(toggles=toggles)

    def test_available_toggle_renders_switch(self):
        html = self._render_base({
            "auto_publish": {
                "label": "Auto Publishing",
                "description": "Publish automatically.",
                "enabled": True,
                "available": True,
                "required_env": ["WORDPRESS_URL"],
            },
        })
        assert "Auto Publishing" in html
        assert 'data-toggle-key="auto_publish"' in html
        assert "Requires" not in html

    def test_unavailable_toggle_says_missing_env(self):
        html = self._render_base({
            "ai_auto_categorization": {
                "label": "AI Auto Categorization",
                "description": "Gemini picks categories.",
                "enabled": False,
                "available": False,
                "required_env": ["GEMINI_API_KEY"],
            },
        })
        assert "AI Auto Categorization" in html
        assert "Unavailable" in html
        assert "GEMINI_API_KEY" in html
        assert "Requires" in html
        # No interactive switch is rendered for an unavailable toggle.
        assert 'data-toggle-key="ai_auto_categorization"' not in html


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
