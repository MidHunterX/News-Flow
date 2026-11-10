"""Offline tests for per-source scrape health and failure isolation.

A source being down (e.g. a 404 while the site is under maintenance) must not
take the app with it. These tests cover the health bookkeeping itself, the
pipeline that records it, the routes/refresher that keep going without the
failed source, and the curation UI that flags it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape

import app.routes.scrape as scrape_routes
from app import source_health
from app.models import NewsItem
from app.scrapers.init import scrape_source
from app.source_health import (
    MAX_DETAIL_CHARS,
    STATUS_DOWN,
    STATUS_OK,
    STATUS_UNKNOWN,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _reset_health():
    source_health.reset()
    yield
    source_health.reset()


def _item(idx: int, source: str = "kaumudi") -> NewsItem:
    return NewsItem(
        id=idx,
        title=f"Article {idx}",
        url=f"https://example.com/{idx}",
        image_url=None,
        description="desc",
        published_at="2026-11-10",
        source=source,
    )


# ---------------------------------------------------------------------------
# Health bookkeeping
# ---------------------------------------------------------------------------


class TestSourceHealth:
    def test_unknown_until_an_attempt_is_made(self):
        status = source_health.get_source_status("kaumudi")
        assert status["state"] == STATUS_UNKNOWN
        assert status["detail"] is None
        assert status["checked_at"] is None

        assert not source_health.is_down("kaumudi")

    def test_failure_marks_the_source_down_with_the_reason(self):
        source_health.record_failure("mangalam", HTTPException(502, "maintenance"))

        status = source_health.get_source_status("mangalam")
        assert status["state"] == STATUS_DOWN
        assert "502" in status["detail"]
        assert status["checked_at"]
        assert source_health.is_down("mangalam")

    def test_success_clears_a_previous_failure(self):
        source_health.record_failure("mangalam", RuntimeError("boom"))
        source_health.record_success("mangalam")

        status = source_health.get_source_status("mangalam")
        assert status["state"] == STATUS_OK
        assert status["detail"] is None
        assert not source_health.is_down("mangalam")

    def test_detail_is_truncated(self):
        source_health.record_failure("mangalam", RuntimeError("boom " * 200))

        detail = source_health.get_source_status("mangalam")["detail"]
        assert len(detail) == MAX_DETAIL_CHARS
        assert detail.endswith("…")

    def test_statuses_cover_every_requested_source_in_order(self):
        source_health.record_success("kaumudi")

        statuses = source_health.get_source_statuses(["kaumudi", "mangalam"])

        assert list(statuses) == ["kaumudi", "mangalam"]
        assert statuses["kaumudi"]["state"] == STATUS_OK
        assert statuses["mangalam"]["state"] == STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# The scrape pipeline records each outcome
# ---------------------------------------------------------------------------


class TestPipelineRecordsHealth:
    async def test_successful_scrape_is_recorded(self, monkeypatch):
        html = (FIXTURES_DIR / "kaumudi_list.html").read_text(encoding="utf-8")

        async def fake_fetch(url: str) -> str:
            return html

        monkeypatch.setattr("app.utils.fetch_html", fake_fetch)

        items = await scrape_source("kaumudi")

        assert items
        assert source_health.get_source_status("kaumudi")["state"] == STATUS_OK

    async def test_failed_scrape_is_recorded_and_still_raises(self, monkeypatch):
        async def fake_fetch(url: str) -> str:
            raise HTTPException(502, "Failed to fetch: 404 Not Found")

        monkeypatch.setattr("app.utils.fetch_html", fake_fetch)

        with pytest.raises(HTTPException):
            await scrape_source("mangalam")

        status = source_health.get_source_status("mangalam")
        assert status["state"] == STATUS_DOWN
        assert "404" in status["detail"]


# ---------------------------------------------------------------------------
# Routes keep serving the healthy sources
# ---------------------------------------------------------------------------


class TestRouteResilience:
    async def test_get_articles_skips_the_failed_source(self, monkeypatch):
        saved: list[NewsItem] = []

        async def fake_cached() -> list[str]:
            return []

        async def fake_scrape(name: str) -> list[NewsItem]:
            if name == "mangalam":
                raise HTTPException(502, "down for maintenance")
            return [_item(1, source=name)]

        async def fake_save(items: list[NewsItem]) -> list[NewsItem]:
            saved.extend(items)
            return list(items)

        async def fake_items(source: str | None = None) -> list[NewsItem]:
            return list(saved)

        monkeypatch.setattr(scrape_routes, "get_cached_sources", fake_cached)
        monkeypatch.setattr(scrape_routes, "scrape_source", fake_scrape)
        monkeypatch.setattr(scrape_routes, "save_items", fake_save)
        monkeypatch.setattr(scrape_routes, "get_items", fake_items)

        items = await scrape_routes._get_articles(["kaumudi", "mangalam"])

        assert [item.source for item in items] == ["kaumudi"]

    async def test_index_page_renders_with_a_down_source(self, monkeypatch):
        """The failing source is flagged; the healthy one still fills the page."""
        html = (FIXTURES_DIR / "kaumudi_list.html").read_text(encoding="utf-8")

        async def fake_fetch(url: str) -> str:
            if "kaumudi" in url:
                return html
            raise HTTPException(502, "down for maintenance")

        async def fake_publish(interval: int) -> None:
            return None

        captured: dict = {}

        def capture_context(**kwargs) -> None:
            captured.update(kwargs.get("context") or {})

        monkeypatch.setattr("app.utils.fetch_html", fake_fetch)
        monkeypatch.setattr(scrape_routes, "publish_due_articles", fake_publish)
        monkeypatch.setattr(
            scrape_routes.templates, "TemplateResponse", capture_context
        )

        await scrape_routes.get_news_ui(request=object(), source=None)

        assert captured["source_status"]["kaumudi"]["state"] == STATUS_OK
        assert captured["down_sources"]["mangalam"]["state"] == STATUS_DOWN
        assert captured["items"]  # the healthy source's stories made it


# ---------------------------------------------------------------------------
# UI: the down source is visible in templates/index.html
# ---------------------------------------------------------------------------


def _render_index(**extra) -> str:
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["url_for"] = lambda name, **params: "/static/" + params["path"]
    context = {
        "items": [],
        "accepted_items": [],
        "completed_items": [],
        "rejected_items": [],
        "sources": ["kaumudi", "mangalam"],
        "selected_source": None,
        "count": 0,
        "items_shown": 0,
        "completion_interval": 600,
        "article_layout": "grid",
        "toggles": {},
        "ai_publish_enabled": False,
        "ai_publish_interval": 3600,
        "ai_publish_last_run": None,
        "ai_publish_running": False,
        "review_fingerprint": "",
    }
    context.update(extra)
    return env.get_template("index.html").render(**context)


def _status(state: str, detail: str | None = None) -> dict:
    return {"state": state, "detail": detail, "checked_at": "2026-11-10T00:00:00"}


class TestDownSourceUI:
    def test_down_source_gets_a_badge_and_a_banner(self):
        statuses = {
            "kaumudi": _status(STATUS_OK),
            "mangalam": _status(STATUS_DOWN, "HTTPException: 502: maintenance"),
        }
        html = _render_index(
            source_status=statuses,
            down_sources={"mangalam": statuses["mangalam"]},
        )

        assert ">Down</span>" in html
        assert "unavailable right now" in html
        assert "maintenance" in html
        # The banner promises the rest of the pipeline keeps running.
        assert "still being scraped as usual" in html

    def test_healthy_sources_show_no_down_marker(self):
        statuses = {
            "kaumudi": _status(STATUS_OK),
            "mangalam": _status(STATUS_OK),
        }
        html = _render_index(source_status=statuses, down_sources={})

        assert ">Down</span>" not in html
        assert "unavailable right now" not in html

    def test_defaults_render_without_status_context(self):
        """Templates stay renderable when the context omits the health data."""
        html = _render_index()
        assert ">Down</span>" not in html
        assert "All Sources" in html

    def test_selected_down_source_empty_state_explains_itself(self):
        statuses = {"mangalam": _status(STATUS_DOWN, "502 maintenance")}
        html = _render_index(
            sources=["mangalam"],
            selected_source="mangalam",
            source_status=statuses,
            down_sources=statuses,
        )

        assert "is unavailable right now — no cached articles either" in html
        assert "once it is back online" in html
