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

import app.db as db_pkg
import app.db.settings as settings_mod
import app.routes.scrape as scrape_routes
from app.db.constants import (
    DEFAULT_SETTINGS,
    RESET_TIMEZONES,
    TOGGLE_ENV_KEYS,
    TOGGLE_SETTINGS,
    toggle_is_available,
)
from app.db.models import Article, Base, Setting

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


class TestGetRefreshInterval:
    async def test_defaults_when_unseeded(self, db):
        assert await settings_mod.get_refresh_interval() == int(
            DEFAULT_SETTINGS["refresh_interval"]
        )

    async def test_stored_value_wins(self, db, monkeypatch):
        async def fake_get(key, default):
            return "300"

        monkeypatch.setattr(settings_mod, "get_setting", fake_get)
        assert await settings_mod.get_refresh_interval() == 300

    async def test_invalid_value_falls_back_to_default(self, db, monkeypatch):
        async def fake_get(key, default):
            return "bogus"

        monkeypatch.setattr(settings_mod, "get_setting", fake_get)
        assert await settings_mod.get_refresh_interval() == int(
            DEFAULT_SETTINGS["refresh_interval"]
        )

    async def test_below_floor_is_clamped(self, db, monkeypatch):
        from app.db.constants import REFRESH_MIN_INTERVAL

        async def fake_get(key, default):
            return "5"

        monkeypatch.setattr(settings_mod, "get_setting", fake_get)
        assert await settings_mod.get_refresh_interval() == REFRESH_MIN_INTERVAL


class TestGetResetTimezone:
    """reset_timezone getter: validated against the known clocks."""

    @staticmethod
    def _seed_tz(session, value: str) -> None:
        session.add(Setting(key="reset_timezone", value=value))
        session.commit()

    async def test_defaults_to_utc(self, db):
        assert await settings_mod.get_reset_timezone() == "utc"
        assert DEFAULT_SETTINGS["reset_timezone"] == "utc"

    async def test_returns_stored_local(self, db):
        with db() as session:
            self._seed_tz(session, "local")
        assert await settings_mod.get_reset_timezone() == "local"

    async def test_invalid_stored_value_falls_back(self, db):
        with db() as session:
            self._seed_tz(session, "IST")
        assert await settings_mod.get_reset_timezone() == "utc"

    def test_valid_values_are_the_declared_set(self):
        assert RESET_TIMEZONES == ("utc", "local")


class TestPendingLimits:
    """Recency limits: 0 means 'no cap', anything invalid falls back."""

    async def test_defaults_when_unseeded(self, db):
        assert await settings_mod.get_ai_publish_pending_limit() == 20
        assert await settings_mod.get_pending_display_limit() == 20
        assert DEFAULT_SETTINGS["ai_publish_pending_limit"] == "20"
        assert DEFAULT_SETTINGS["pending_display_limit"] == "20"

    async def test_stored_values_win(self, db):
        with db() as session:
            session.add(Setting(key="ai_publish_pending_limit", value="5"))
            session.add(Setting(key="pending_display_limit", value="7"))
            session.commit()
        assert await settings_mod.get_ai_publish_pending_limit() == 5
        assert await settings_mod.get_pending_display_limit() == 7

    async def test_zero_means_no_cap(self, db):
        with db() as session:
            session.add(Setting(key="ai_publish_pending_limit", value="0"))
            session.add(Setting(key="pending_display_limit", value="0"))
            session.commit()
        assert await settings_mod.get_ai_publish_pending_limit() == 0
        assert await settings_mod.get_pending_display_limit() == 0

    @pytest.mark.parametrize("value", ["bogus", ""])
    async def test_unusable_values_fall_back_to_the_default(
        self, db, monkeypatch, value
    ):
        async def fake_get(key, default):
            return value

        monkeypatch.setattr(settings_mod, "get_setting", fake_get)
        assert await settings_mod.get_ai_publish_pending_limit() == 20
        assert await settings_mod.get_pending_display_limit() == 20

    async def test_negative_clamps_to_no_cap(self, db, monkeypatch):
        async def fake_get(key, default):
            return "-3"

        monkeypatch.setattr(settings_mod, "get_setting", fake_get)
        assert await settings_mod.get_ai_publish_pending_limit() == 0
        assert await settings_mod.get_pending_display_limit() == 0


# ---------------------------------------------------------------------------
# Feature toggles (ai_auto_categorization / auto_publish)
# ---------------------------------------------------------------------------


class TestToggleHelpers:
    def test_every_toggle_has_env_keys(self):
        assert set(TOGGLE_SETTINGS) == {
            "ai_auto_categorization", "ai_publish", "auto_publish"
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
        assert states == {
            "ai_auto_categorization": False,
            "ai_publish": True,
            "auto_publish": True,
        }


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

    async def test_reset_timezone_accepts_utc_and_local(self, recorded):
        result = await scrape_routes.update_settings({"reset_timezone": "local"})
        assert recorded == {"reset_timezone": "local"}
        assert result["reset_timezone"] == "local"

        result = await scrape_routes.update_settings({"reset_timezone": "utc"})
        assert recorded == {"reset_timezone": "utc"}
        assert result["reset_timezone"] == "utc"

    @pytest.mark.parametrize("value", ["IST", "Asia/Kolkata", "", "UTC"])
    async def test_reset_timezone_rejects_unknown_values(self, recorded, value):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"reset_timezone": value})
        assert excinfo.value.status_code == 400
        assert "reset_timezone must be one of" in excinfo.value.detail
        assert recorded == {}  # nothing was written

    async def test_interval_validation_untouched(self, recorded):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"completion_interval": "0"})
        assert excinfo.value.status_code == 400

    @pytest.mark.parametrize("value", ["0", "-5", "abc"])
    async def test_refresh_interval_rejects_below_one(self, recorded, value):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"refresh_interval": value})
        assert excinfo.value.status_code == 400
        assert recorded == {}

    async def test_refresh_interval_accepts_seconds(self, recorded):
        result = await scrape_routes.update_settings({"refresh_interval": "1200"})
        assert recorded == {"refresh_interval": "1200"}
        assert result["refresh_interval"] == "1200"

    async def test_ai_publish_count_positive(self, recorded):
        result = await scrape_routes.update_settings({"ai_publish_count": "5"})
        assert recorded == {"ai_publish_count": "5"}
        assert result["ai_publish_count"] == "5"

    async def test_ai_publish_count_rejects_zero(self, recorded):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"ai_publish_count": "0"})
        assert excinfo.value.status_code == 400
        assert recorded == {}

    async def test_ai_publish_count_rejects_non_numeric(self, recorded):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"ai_publish_count": "three"})
        assert excinfo.value.status_code == 400

    @pytest.mark.parametrize("value", ["0", "-5", "abc"])
    async def test_ai_publish_interval_rejects_below_one(self, recorded, value):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"ai_publish_interval": value})
        assert excinfo.value.status_code == 400
        assert recorded == {}

    async def test_ai_publish_interval_accepts_seconds(self, recorded):
        await scrape_routes.update_settings({"ai_publish_interval": "1800"})
        assert recorded == {"ai_publish_interval": "1800"}

    @pytest.mark.parametrize("value", ["-1", "ten"])
    async def test_ai_publish_history_rejects_negative(self, recorded, value):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"ai_publish_history": value})
        assert excinfo.value.status_code == 400
        assert recorded == {}

    async def test_ai_publish_history_accepts_zero(self, recorded):
        await scrape_routes.update_settings({"ai_publish_history": "0"})
        assert recorded == {"ai_publish_history": "0"}

    @pytest.mark.parametrize("value", ["-1", "ten"])
    async def test_ai_publish_pending_limit_rejects_negative(
        self, recorded, value
    ):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings(
                {"ai_publish_pending_limit": value}
            )
        assert excinfo.value.status_code == 400
        assert recorded == {}

    async def test_ai_publish_pending_limit_accepts_zero(self, recorded):
        result = await scrape_routes.update_settings(
            {"ai_publish_pending_limit": "0"}
        )
        assert recorded == {"ai_publish_pending_limit": "0"}
        assert result["ai_publish_pending_limit"] == "0"

    @pytest.mark.parametrize("value", ["-1", "ten"])
    async def test_pending_display_limit_rejects_negative(self, recorded, value):
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"pending_display_limit": value})
        assert excinfo.value.status_code == 400
        assert recorded == {}

    async def test_pending_display_limit_accepts_zero(self, recorded):
        result = await scrape_routes.update_settings({"pending_display_limit": "0"})
        assert recorded == {"pending_display_limit": "0"}
        assert result["pending_display_limit"] == "0"


# ---------------------------------------------------------------------------
# Template layout branches
# ---------------------------------------------------------------------------


def _item(idx: int, **overrides) -> SimpleNamespace:
    defaults = {
        "id": idx,
        "title": f"Article {idx}",
        "url": f"https://example.com/{idx}",
        "image_url": f"https://example.com/{idx}.jpg",
        "description": "desc",
        "published_at": "2026-09-19",
        "source": "kaumudi",
        "status": None,
        "accepted_at": None,
        "cover_file": None,
        "wp_category_ids": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _url_for(name: str, **params: str) -> str:
    """The app's Starlette ``url_for``, minus the routing.

    Templates call it for mounted static files (``ai-blob.js``); only the
    ``static`` route is used here, matching ``app.mount("/static", ...)``.
    """
    if name != "static":
        raise AssertionError(f"tests only stub the static route, got {name!r}")
    return "/static/" + params["path"]


def _template_env() -> Environment:
    """Bare Jinja environment carrying the globals Jinja2Templates adds."""
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["url_for"] = _url_for
    return env


def _render(layout: str, items: list, **extra) -> str:
    env = _template_env()
    template = env.get_template("index.html")
    context = {
        "items": items,
        "accepted_items": [],
        "completed_items": [],
        "rejected_items": [],
        "sources": ["kaumudi"],
        "selected_source": None,
        "count": len(items),
        "items_shown": len(items),
        "completion_interval": 600,
        "article_layout": layout,
        "toggles": {},
        "ai_publish_enabled": False,
        "ai_publish_interval": 3600,
        "ai_publish_last_run": None,
        "ai_publish_running": False,
        "review_fingerprint": "",
    }
    context.update(extra)
    return template.render(**context)


class TestPendingLimitRendering:
    """The queue header admits when it is showing a truncated list."""

    def test_header_flags_the_truncated_queue(self):
        html = _render("grid", [_item(1), _item(2)], count=9, items_shown=2)
        assert "9 items found" in html
        assert "showing latest 2" in html

    def test_untruncated_header_stays_quiet(self):
        html = _render("grid", [_item(1)], count=1, items_shown=1)
        assert "1 items found" in html
        assert "showing latest" not in html


class TestSettingsModalPendingInputs:
    """Both recency fields are present in the settings modal."""

    def test_inputs_render(self):
        env = _template_env()
        html = env.get_template("base.html").render(
            toggles={
                "ai_publish": {
                    "label": "AI Publish",
                    "description": "",
                    "enabled": True,
                    "available": True,
                    "required_env": [],
                }
            }
        )
        assert 'id="pending-display-limit-input"' in html
        assert 'id="ai-publish-pending-limit-input"' in html


class TestSettingsModalToggles:
    """The settings modal renders toggle switches with env messaging."""

    def _render_base(self, toggles: dict) -> str:
        env = _template_env()
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
        # AI Publish knobs only render when the ai_publish toggle exists.
        assert 'id="ai-publish-count-input"' not in html

    def test_ai_publish_available_renders_setting_inputs(self):
        html = self._render_base({
            "ai_publish": {
                "label": "AI Publish",
                "description": "Gemini picks articles.",
                "enabled": True,
                "available": True,
                "required_env": ["GEMINI_API_KEY"],
            },
        })
        assert 'id="ai-publish-count-input"' in html
        assert 'id="ai-publish-interval-input"' in html
        assert 'id="ai-publish-history-input"' in html
        assert "Articles per prompt" in html

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


class TestResetTimezonePicker:
    """The settings modal renders a UTC/Local picker for the daily reset."""

    def _render_base(self, toggles: dict) -> str:
        env = _template_env()
        template = env.get_template("base.html")
        return template.render(toggles=toggles)

    def test_picker_renders_both_options(self):
        html = self._render_base({})
        assert 'id="reset-timezone-picker"' in html
        assert "Daily reset clock" in html
        assert 'data-reset-tz="utc"' in html
        assert 'data-reset-tz="local"' in html
        assert "selectResetTimezone('utc')" in html
        assert "selectResetTimezone('local')" in html

    def test_save_sends_reset_timezone(self):
        """The save payload includes the selected clock."""
        html = self._render_base({})
        assert "reset_timezone: selectedResetTimezone" in html

    def test_open_populates_picker_from_settings(self):
        html = self._render_base({})
        assert "selectResetTimezone(settings.reset_timezone" in html


class TestThemePicker:
    """The theme mode is a local-only setting: picker + localStorage."""

    def _render_base(self, toggles: dict | None = None) -> str:
        env = _template_env()
        template = env.get_template("base.html")
        return template.render(toggles=toggles or {})

    def test_picker_renders_all_three_modes(self):
        html = self._render_base()
        assert 'id="theme-picker"' in html
        for mode in ("light", "dark", "system"):
            assert f'data-theme="{mode}"' in html
            assert f"selectTheme('{mode}')" in html

    def test_default_is_system(self):
        """Missing/unreadable storage falls back to following the OS."""
        html = self._render_base()
        # The pre-paint snippet: anything but light/dark means "system".
        assert 'mode !== "light" && mode !== "dark"' in html
        assert 'return THEME_MODES.includes(stored) ? stored : "system";' in html

    def test_theme_never_reaches_the_server(self):
        """Purely local: not in the save payload, not seeded from the API."""
        html = self._render_base()
        payload = html.split("const payload = {", 1)[1].split("};", 1)[0]
        assert "theme" not in payload.lower()
        # The picker is seeded from localStorage, before the settings fetch.
        open_body = html.split("async function openSettings() {", 1)[1]
        assert open_body.index("renderThemePicker(readTheme());") < open_body.index(
            'fetch("/api/settings")'
        )

    def test_changing_it_applies_immediately(self):
        """No Save, no reload: the choice applies and persists on the spot."""
        html = self._render_base()
        theme_fn = html.split("function selectTheme(", 1)[1].split(
            "\n      }", 1
        )[0]
        assert "localStorage.setItem(THEME_KEY, mode)" in theme_fn
        assert "applyTheme(mode);" in theme_fn
        assert "renderThemePicker(mode);" in theme_fn
        assert "reload" not in theme_fn
        assert "fetch(" not in theme_fn

    def test_applies_the_class_before_first_paint(self):
        html = self._render_base()
        assert 'tailwind.config = { darkMode: "class" }' in html
        # The head snippet runs before <body>, so there is no flash.
        head = html.split("</head>", 1)[0]
        assert 'localStorage.getItem("newsflow-theme")' in head
        assert 'classList.toggle("dark", dark)' in head
        assert "prefers-color-scheme: dark" in head

    def test_system_mode_tracks_os_changes_live(self):
        html = self._render_base()
        assert 'systemThemeQuery.addEventListener("change"' in html
        assert 'if (readTheme() === "system") applyTheme("system");' in html

    def test_shell_carries_dark_styles(self):
        """Header, modals and footer are restyled for dark; cards stay light."""
        html = self._render_base()
        assert "dark:bg-gray-900" in html  # page background
        assert "dark:bg-gray-800" in html  # header / modals / footer
        # Cards keep their light surfaces and dark text.
        assert 'class="bg-white rounded-xl shadow-xl w-full max-w-md' in html


class TestInnerPageDarkStyles:
    """Dark mode reaches every page, not just the shell.

    Each assertion pairs a dark variant with the light class it answers,
    so a half-styled card (dark surface, dark text) gets caught.
    """

    def _env(self) -> Environment:
        return _template_env()

    def test_curation_cards_go_dark(self):
        html = _render("grid", [_item(1)])
        assert "dark:bg-gray-800" in html       # cards + side panel
        assert "dark:text-gray-100" in html      # titles
        assert "dark:text-gray-300" in html      # descriptions
        assert "dark:border-gray-700" in html
        assert "dark:bg-indigo-950" in html      # panel header / source chips

    def test_publication_cards_go_dark(self):
        html = self._env().get_template("publication.html").render(
            toggles={},
            article_layout="grid",
            selected_day="previous",
            count=1,
            section={
                "label": "Previous day",
                "note": "n",
                "cards": [{"item": _item(1), "cover": None, "word_count": 7}],
                "empty": "e",
            },
        )
        assert "dark:bg-gray-800" in html
        assert "dark:text-gray-100" in html
        assert "dark:border-green-800" in html   # scrape badge

    def test_article_page_goes_dark(self):
        html = self._env().get_template("article.html").render(
            toggles={},
            article=_item(1),
            scraped_heading="Heading",
            scraped_content="Body",
            cover_file=None,
        )
        assert "dark:bg-gray-800" in html        # copy zones
        assert "dark:text-gray-100" in html       # heading
        assert "dark:text-gray-300" in html       # body copy

    def test_light_classes_are_left_alone(self):
        """Every addition is a dark: variant — light mode is untouched."""
        html = _render("grid", [_item(1)])
        assert "bg-white rounded-xl border border-gray-200" in html
        assert "text-gray-900" in html


class TestCoverImageCopy:
    """Clicking the cover copies the image itself, not its file path."""

    def _render(self) -> str:
        env = _template_env()
        return env.get_template("article.html").render(
            toggles={},
            article=_item(1),
            scraped_heading="Heading",
            scraped_content="Body",
            cover_file="/covers/img-1.jpg",
        )

    def test_cover_zone_calls_copy_image(self):
        html = self._render()
        assert 'onclick="copyImage(this.dataset.path)"' in html
        assert 'copyText(this.dataset.path' not in html
        assert "Click to copy image" in html
        assert "Click to copy image path" not in html

    def test_copy_image_writes_a_png_clipboard_item(self):
        """The image lands on the clipboard as pixels, via write() — never
        writeText(), and the toast reports success or failure either way."""
        html = self._render()
        fn = html.split("async function copyImage(", 1)[1].split("\n    }", 1)[0]
        assert 'navigator.clipboard.write([new ClipboardItem(' in fn
        assert '"image/png"' in fn
        assert "writeText" not in fn
        assert 'showToast("Image copied!")' in fn
        assert 'showToast("Couldn\'t copy the image.")' in fn

    def test_non_png_covers_are_reencoded_first(self):
        """Covers are whatever the source served (jpeg/webp), so anything
        that is not image/png goes through the canvas converter."""
        html = self._render()
        assert 'blob.type === "image/png" ? blob : await toPng(blob)' in html
        assert "function toPng(blob)" in html
        assert 'canvas.toBlob(' in html


class TestArticleView:
    """The article view route must not reference missing imports."""

    async def test_renders_without_name_error(self, db, monkeypatch, tmp_path):
        """Regression: article_view used ARTICLES_DIR after its import was
        removed during the accept-flow refactor, 500ing every article page."""
        from app.models import NewsItem

        articles_dir = tmp_path / "articles"
        articles_dir.mkdir()
        monkeypatch.setattr(scrape_routes, "ARTICLES_DIR", articles_dir)

        async def fake_article(article_id):
            return NewsItem(
                title="T", url="https://example.com/a", image_url=None,
                description="d", published_at="2026-09-19",
                source="kaumudi", id=article_id, cover_file=None,
            )

        async def fake_toggles():
            return {"toggles": {}}

        monkeypatch.setattr(scrape_routes, "get_article_by_id", fake_article)
        monkeypatch.setattr(scrape_routes, "_toggle_context", fake_toggles)

        # Would raise NameError before the fix; Jinja reads the template from
        # disk so no app startup is needed.
        response = await scrape_routes.article_view(request=object(), article_id=1)
        assert response.status_code == 200


class TestRefreshIntervalInput:
    """The settings modal renders the backend refresh interval field."""

    def _render_base(self, toggles: dict) -> str:
        env = _template_env()
        template = env.get_template("base.html")
        return template.render(toggles=toggles)

    def test_refresh_interval_input_renders(self):
        html = self._render_base({})
        assert 'id="refresh-interval-input"' in html
        assert "Refresh interval (minutes)" in html

    def test_frontend_refresh_trigger_removed(self):
        """The JS timer + Refresh Now button are gone; backend refreshes now."""
        html = self._render_base({})
        assert "refreshNews" not in html
        assert "timer-display" not in html
        assert "Refresh Now" not in html
        assert "autoRefreshSeconds" not in html


class TestAiPublishMeter:
    """The AI Publish countdown meter renders only when the feature is on."""

    def test_meter_hidden_when_disabled(self):
        html = _render("grid", [_item(1)], ai_publish_enabled=False)
        assert 'id="ai-publish-meter"' not in html

    def test_meter_renders_when_enabled(self):
        html = _render("grid", [_item(1)], ai_publish_enabled=True,
                       ai_publish_interval=1800, ai_publish_last_run=None)
        assert 'id="ai-publish-meter"' in html
        assert 'data-interval="1800"' in html
        assert "AI Publish" in html

    def test_meter_carries_last_run_timestamp(self):
        html = _render("grid", [], ai_publish_enabled=True,
                       ai_publish_last_run="2026-09-25T10:00:00+00:00")
        assert 'data-last-run="2026-09-25T10:00:00+00:00"' in html

    def test_meter_without_last_run_has_empty_marker(self):
        html = _render("grid", [], ai_publish_enabled=True,
                       ai_publish_last_run=None)
        assert 'data-last-run=""' in html


# ---------------------------------------------------------------------------
# Curation auto-refresh: GET /api/state + the page's poll
# ---------------------------------------------------------------------------


def _seed_article(session, *, source: str = "kaumudi", status: str | None = None):
    """One row in the curation workspace; returns its ID."""
    article = Article(
        title="Story",
        url="https://example.com/story",
        image_url=None,
        description="desc",
        published_at="2026-10-26",
        source=source,
        status=status,
    )
    session.add(article)
    session.commit()
    return article.id


class TestReviewFingerprint:
    """The fingerprint identifies the rows the panel renders, not its count."""

    def test_empty_panel_is_a_stable_string(self):
        assert scrape_routes._review_fingerprint([], [], [], None) == ";;;"

    def test_ids_make_swapping_articles_visible(self):
        """Same count, different article → the panel really did change."""
        one = scrape_routes._review_fingerprint([_item(1)], [], [], None)
        two = scrape_routes._review_fingerprint([_item(2)], [], [], None)

        assert one == "1;;;"
        assert one != two

    def test_carries_the_ai_publish_stamp(self):
        """A round that accepted nothing still restarts the meter."""
        never = scrape_routes._review_fingerprint([], [], [], None)
        stamped = scrape_routes._review_fingerprint(
            [], [], [], "2026-10-26T10:00:00+00:00"
        )

        assert never != stamped


class TestCurationState:
    """GET /api/state drives the page's reload-when-something-changed poll."""

    async def test_reports_an_empty_workspace(self):
        state = await scrape_routes.get_curation_state(source=None)

        assert state["running"] is False
        assert state["counts"] == {"accepted": 0, "completed": 0, "rejected": 0}
        assert state["fingerprint"] == scrape_routes._review_fingerprint(
            [], [], [], None
        )

    async def test_accepting_an_article_changes_the_fingerprint(self):
        with db_pkg.SessionLocal() as session:
            article_id = _seed_article(session)

        before = await scrape_routes.get_curation_state(source=None)

        with db_pkg.SessionLocal() as session:
            session.get(Article, article_id).status = "accepted"
            session.commit()
        after = await scrape_routes.get_curation_state(source=None)

        assert before["fingerprint"] != after["fingerprint"]
        assert after["counts"]["accepted"] == 1

    async def test_new_pending_stories_never_trigger_a_reload(self):
        """The background refresher adding articles must not interrupt."""
        empty = await scrape_routes.get_curation_state(source=None)

        with db_pkg.SessionLocal() as session:
            _seed_article(session)

        still = await scrape_routes.get_curation_state(source=None)

        assert still["fingerprint"] == empty["fingerprint"]

    async def test_source_filter_narrows_the_panel(self):
        with db_pkg.SessionLocal() as session:
            _seed_article(session, source="kaumudi", status="accepted")
            _seed_article(session, source="mangalam", status="accepted")

        every_source = await scrape_routes.get_curation_state(source=None)
        one_source = await scrape_routes.get_curation_state(source="kaumudi")

        assert every_source["counts"]["accepted"] == 2
        assert one_source["counts"]["accepted"] == 1
        assert one_source["fingerprint"] != every_source["fingerprint"]


class TestAutoRefreshScript:
    """The curation page ships a lazy poll and the meter's working state."""

    def test_page_embeds_the_fingerprint_seed(self):
        html = _render("grid", [], review_fingerprint="1,2;;;")

        assert 'let lastFingerprint = "1,2;;;";' in html

    def test_polling_starts_only_when_it_is_needed(self):
        """No timer at load: startPolling() is what creates it, and only the
        meter reaching "Due" (or opening the page mid-round) calls it."""
        html = _render("grid", [], review_fingerprint="")

        assert html.count("setInterval(pollCurationState, 3000)") == 1
        assert html.index("function startPolling()") < html.index(
            "pollTimer = setInterval(pollCurationState, 3000)"
        )
        due = html.index('label.textContent = "Due";')
        assert html.index("startPolling();", due) < html.index("} else {", due)

    def test_page_polls_the_state_endpoint(self):
        html = _render("grid", [], review_fingerprint="")

        assert "`/api/state${qs}`" in html

    def test_polling_pauses_while_the_tab_is_hidden(self):
        html = _render("grid", [], review_fingerprint="")

        assert "if (document.hidden) return;" in html
        assert '"visibilitychange"' in html

    def test_page_scopes_the_poll_to_the_selected_source(self):
        html = _render("grid", [], selected_source="mangalam")

        assert 'const reviewSource = "mangalam";' in html

    def test_meter_shows_curating_while_a_round_runs(self):
        html = _render("grid", [], review_fingerprint="")

        assert 'label.textContent = "Curating…";' in html
        # The poll defers its reload so one round means one refresh.
        assert "if (!state.running && state.fingerprint !== lastFingerprint)" in html

    def test_page_seeds_a_round_already_in_flight(self):
        """A page opened mid-round sees "Curating…", not the countdown the
        round just reset — and starts watching so it knows when to land."""
        running = _render("grid", [], ai_publish_running=True)
        idle = _render("grid", [], ai_publish_running=False)

        assert "let aiRoundRunning = true;" in running
        assert "if (aiRoundRunning) startPolling();" in running
        assert "let aiRoundRunning = false;" in idle


class TestIndexRefreshSeed:
    """The route feeds the page's poll from the same source as /api/state."""

    async def test_page_is_seeded_from_the_server(self, monkeypatch):
        import app.curator as curator_mod

        async def fake_scrape(name):
            return []

        async def fake_publish(interval):
            return None

        captured = {}

        def capture_context(**kwargs):
            captured.update(kwargs.get("context") or {})

        monkeypatch.setattr(scrape_routes, "scrape_source", fake_scrape)
        monkeypatch.setattr(scrape_routes, "publish_due_articles", fake_publish)
        monkeypatch.setattr(scrape_routes.templates, "TemplateResponse",
                            capture_context)
        monkeypatch.setattr(curator_mod, "_SELECTING", True)

        await scrape_routes.get_news_ui(request=object(), source=None)

        assert captured["ai_publish_running"] is True
        # The seed must match what the first /api/state poll would return,
        # or the page would reload itself the instant it starts watching.
        assert captured["review_fingerprint"] == scrape_routes._review_fingerprint(
            [], [], [], captured["ai_publish_last_run"]
        )


class TestPendingDisplayLimit:
    """The pending queue shows only the newest N articles (0 = all)."""

    @staticmethod
    def _fake_route(monkeypatch, captured, pending, limit):
        async def fake_scrape(name):
            return []

        async def fake_publish(interval):
            return None

        async def fake_pending(source=None):
            return list(pending)

        async def fake_limit():
            return limit

        def capture_context(**kwargs):
            captured.update(kwargs.get("context") or {})

        monkeypatch.setattr(scrape_routes, "scrape_source", fake_scrape)
        monkeypatch.setattr(scrape_routes, "publish_due_articles", fake_publish)
        monkeypatch.setattr(scrape_routes, "get_pending_items", fake_pending)
        monkeypatch.setattr(
            scrape_routes, "get_pending_display_limit", fake_limit
        )
        monkeypatch.setattr(scrape_routes.templates, "TemplateResponse",
                            capture_context)

    async def test_limit_shows_only_the_newest(self, monkeypatch):
        captured = {}
        pending = [_item(i) for i in range(1, 8)]  # newest first
        self._fake_route(monkeypatch, captured, pending, limit=3)

        await scrape_routes.get_news_ui(request=object(), source=None)

        assert [item.id for item in captured["items"]] == [1, 2, 3]
        # The header still reports the real backlog, so the truncation is
        # visible instead of silently hiding articles.
        assert captured["count"] == 7
        assert captured["items_shown"] == 3

    async def test_zero_shows_every_pending_article(self, monkeypatch):
        captured = {}
        pending = [_item(i) for i in range(1, 8)]
        self._fake_route(monkeypatch, captured, pending, limit=0)

        await scrape_routes.get_news_ui(request=object(), source=None)

        assert len(captured["items"]) == 7
        assert captured["count"] == 7
        assert captured["items_shown"] == 7
