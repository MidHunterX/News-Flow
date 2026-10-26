"""Offline tests for the print-publication page (``GET /publication``).

Covers the card data assembly (local-scrape flag + word count), the route
that reads both workspaces, and the template branches — day tabs, the
``article_layout`` layouts, and the scrape badges. No network: the route
only reads the local database and files.
"""

from __future__ import annotations

import re

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

import app.db as db_pkg
import app.routes.scrape as scrape_routes
from app.db.models import Article, PublicationArticle, Setting
from app.models import NewsItem

LAYOUTS = ("grid", "rows", "compact")


def _item(idx: int, **overrides) -> NewsItem:
    defaults = {
        "id": idx,
        "title": f"Article {idx}",
        "url": f"https://example.com/{idx}",
        "image_url": f"https://example.com/{idx}.jpg",
        "description": "desc",
        "published_at": "2026-10-25",
        "source": "kaumudi",
        "cover_file": None,
    }
    defaults.update(overrides)
    return NewsItem(**defaults)


def _card(idx: int, word_count: int | None = None, **overrides) -> dict:
    """A card exactly as the route builds it (item + cover + word count)."""
    item = _item(idx, **overrides)
    return {
        "item": item,
        "cover": item.cover_file,
        "word_count": word_count,
    }


def _render(cards, *, article_layout="grid", selected_day="previous",
            empty="Nothing here yet.") -> str:
    """Render publication.html directly, bypassing FastAPI entirely.

    Mirrors the context publication_view passes (one selected day tab).
    """
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("publication.html")
    label = "Today" if selected_day == "today" else "Previous day"
    return template.render(
        toggles={},
        article_layout=article_layout,
        selected_day=selected_day,
        count=len(cards),
        section={
            "label": label,
            "note": "note",
            "cards": cards,
            "empty": empty,
        },
    )


def _tab_class(html: str, href: str) -> str:
    """Class list of the day tab (px-4 pill) linking to *href*.

    Skips the header's px-3 nav pill, which points at the same URL.
    """
    match = re.search(
        rf'<a href="{re.escape(href)}"\s+class="(px-4[^"]*)"', html, flags=re.DOTALL
    )
    return match.group(1) if match else ""


ACTIVE = "bg-indigo-600 text-white shadow-sm"


# ---------------------------------------------------------------------------
# _build_cards / _word_count
# ---------------------------------------------------------------------------


class TestBuildCards:
    def test_scraped_article_reports_its_word_count(self, tmp_path):
        (tmp_path / "1.txt").write_text("H\n\none two three four", encoding="utf-8")

        cards = scrape_routes._build_cards([_item(1)], tmp_path, archived=False)

        assert cards[0]["word_count"] == 5  # heading + body words
        assert cards[0]["cover"] is None

    def test_missing_body_is_flagged_as_not_scraped(self, tmp_path):
        cards = scrape_routes._build_cards([_item(1)], tmp_path, archived=False)

        assert cards[0]["word_count"] is None

    def test_unreadable_body_counts_as_not_scraped(self, tmp_path):
        (tmp_path / "1.txt").write_bytes(b"\xff\xfe\x00garbage")

        cards = scrape_routes._build_cards([_item(1)], tmp_path, archived=False)

        assert cards[0]["word_count"] is None

    def test_item_without_id_never_reads_a_file(self, tmp_path):
        cards = scrape_routes._build_cards(
            [_item(0, id=None)], tmp_path, archived=False
        )

        assert cards[0]["word_count"] is None

    def test_archived_cover_points_at_the_publication_workspace(self, tmp_path):
        item = _item(1, cover_file="/covers/one.png")

        [curation] = scrape_routes._build_cards([item], tmp_path, archived=False)
        [archived] = scrape_routes._build_cards([item], tmp_path, archived=True)

        assert curation["cover"] == "/covers/one.png"
        assert archived["cover"] == "/publication/covers/one.png"

    def test_other_cover_urls_pass_through_unchanged(self, tmp_path):
        """A cover already outside /covers/ is served as-is."""
        item = _item(1, cover_file="https://cdn.example.com/x.png")

        [archived] = scrape_routes._build_cards([item], tmp_path, archived=True)

        assert archived["cover"] == "https://cdn.example.com/x.png"


# ---------------------------------------------------------------------------
# GET /publication (route wiring: DB + files + template)
# ---------------------------------------------------------------------------


def _bind_workspaces(monkeypatch, tmp_path):
    """Point the route at temp curation/publication content directories."""
    curation_dir = tmp_path / "articles"
    publication_dir = tmp_path / "publication" / "articles"
    curation_dir.mkdir(parents=True, exist_ok=True)
    publication_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(scrape_routes, "ARTICLES_DIR", curation_dir)
    monkeypatch.setattr(scrape_routes, "PUBLICATION_ARTICLES_DIR", publication_dir)
    return curation_dir, publication_dir


def _seed_both_workspaces(curation_dir, publication_dir) -> tuple[int, int]:
    """One article in each workspace, with a saved body for each."""
    with db_pkg.SessionLocal() as session:
        today = Article(
            title="Today Story",
            url="https://example.com/today",
            image_url=None,
            description="today desc",
            published_at="2026-10-26",
            source="mangalam",
        )
        previous = PublicationArticle(
            title="Yesterday Story",
            url="https://example.com/yesterday",
            image_url=None,
            description="yesterday desc",
            published_at="2026-10-25",
            source="madhyamam",
            cover_file="/covers/old.png",
        )
        session.add_all([today, previous])
        session.commit()
        today_id, previous_id = today.id, previous.id

    (curation_dir / f"{today_id}.txt").write_text(
        "Head one two three", encoding="utf-8"
    )
    (publication_dir / f"{previous_id}.txt").write_text(
        "Head four five six seven", encoding="utf-8"
    )
    return today_id, previous_id


class TestPublicationRoute:
    async def test_default_tab_is_the_previous_day(
        self, monkeypatch, tmp_path
    ):
        curation_dir, publication_dir = _bind_workspaces(monkeypatch, tmp_path)
        _seed_both_workspaces(curation_dir, publication_dir)

        response = await scrape_routes.publication_view(request=object())
        body = response.body.decode()

        assert response.status_code == 200
        # Previous day is the default: its archive shows, today's does not.
        assert "Previous day" in body and "Yesterday Story" in body
        assert "Today Story" not in body
        # Both tabs are present, previous one first.
        previous_at = body.find('href="/publication"\n', body.find("<main"))
        today_at = body.find('href="/publication?day=today"', body.find("<main"))
        assert 0 < previous_at < today_at
        assert ACTIVE in _tab_class(body, "/publication")
        assert ACTIVE not in _tab_class(body, "/publication?day=today")

    async def test_today_tab_shows_the_ongoing_workspace(
        self, monkeypatch, tmp_path
    ):
        curation_dir, publication_dir = _bind_workspaces(monkeypatch, tmp_path)
        _seed_both_workspaces(curation_dir, publication_dir)

        response = await scrape_routes.publication_view(
            request=object(), day="today"
        )
        body = response.body.decode()

        assert response.status_code == 200
        assert "Today Story" in body
        assert "Yesterday Story" not in body
        # The Today pill is now the active one.
        assert ACTIVE in _tab_class(body, "/publication?day=today")
        assert ACTIVE not in _tab_class(body, "/publication")

    async def test_scraped_word_counts_render_per_card(
        self, monkeypatch, tmp_path
    ):
        curation_dir, publication_dir = _bind_workspaces(monkeypatch, tmp_path)
        _seed_both_workspaces(curation_dir, publication_dir)

        previous = await scrape_routes.publication_view(request=object())
        today = await scrape_routes.publication_view(request=object(), day="today")

        previous_body, today_body = previous.body.decode(), today.body.decode()
        assert "Locally scraped" in previous_body and "5 words" in previous_body
        assert "Locally scraped" in today_body and "4 words" in today_body
        # The archived card's cover is served from the publication workspace.
        assert "/publication/covers/old.png" in previous_body

    async def test_article_without_content_is_marked_not_scraped(
        self, monkeypatch, tmp_path
    ):
        _bind_workspaces(monkeypatch, tmp_path)

        with db_pkg.SessionLocal() as session:
            session.add(
                Article(
                    title="Never Accepted",
                    url="https://example.com/none",
                    image_url=None,
                    description="desc",
                    published_at="2026-10-26",
                    source="kaumudi",
                )
            )
            session.commit()

        response = await scrape_routes.publication_view(
            request=object(), day="today"
        )
        body = response.body.decode()

        assert response.status_code == 200
        assert "Not scraped locally" in body
        assert "Locally scraped" not in body

    async def test_empty_tabs_render_their_empty_states(
        self, monkeypatch, tmp_path
    ):
        # Directories left missing on purpose: nothing exists yet.
        monkeypatch.setattr(
            scrape_routes, "ARTICLES_DIR", tmp_path / "absent" / "articles"
        )
        monkeypatch.setattr(
            scrape_routes,
            "PUBLICATION_ARTICLES_DIR",
            tmp_path / "absent" / "publication",
        )

        previous = await scrape_routes.publication_view(request=object())
        today = await scrape_routes.publication_view(request=object(), day="today")

        assert "Nothing archived yet" in previous.body.decode()
        assert "No articles in today&#39;s workspace yet." in today.body.decode()

    async def test_respects_the_article_layout_setting(
        self, monkeypatch, tmp_path
    ):
        """The publication page renders the same layout the curation UI uses."""
        curation_dir, _publication_dir = _bind_workspaces(monkeypatch, tmp_path)
        _seed_both_workspaces(curation_dir, tmp_path / "publication" / "articles")
        with db_pkg.SessionLocal() as session:
            session.add(Setting(key="article_layout", value="rows"))
            session.commit()

        response = await scrape_routes.publication_view(request=object())
        body = response.body.decode()

        assert response.status_code == 200
        assert "ROWS layout" in body

    async def test_unknown_day_falls_back_to_the_previous_day(
        self, monkeypatch, tmp_path
    ):
        curation_dir, publication_dir = _bind_workspaces(monkeypatch, tmp_path)
        _seed_both_workspaces(curation_dir, publication_dir)

        response = await scrape_routes.publication_view(
            request=object(), day="yesterday-please"
        )
        body = response.body.decode()

        assert response.status_code == 200
        assert "Yesterday Story" in body


# ---------------------------------------------------------------------------
# Template branch coverage (no route involved)
# ---------------------------------------------------------------------------


class TestPublicationTemplate:
    @pytest.mark.parametrize("layout", LAYOUTS)
    def test_each_layout_renders_its_branch(self, layout):
        html = _render([_card(1), _card(2)], article_layout=layout)

        assert f"{layout.upper()} layout" in html

    def test_unknown_layout_renders_grid_branch(self):
        html = _render([_card(1)], article_layout="bogus")

        assert "GRID layout" in html

    @pytest.mark.parametrize("layout", LAYOUTS)
    def test_every_layout_carries_the_scrape_badge(self, layout):
        """The local-scrape status shows up whatever the layout is."""
        html = _render(
            [_card(1, word_count=123), _card(2)], article_layout=layout
        )

        assert "Locally scraped" in html
        assert "123 words" in html
        assert "Not scraped locally" in html

    @pytest.mark.parametrize("href", ["/publication", "/publication?day=today"])
    def test_both_day_tabs_render(self, href):
        assert _tab_class(_render([]), href)

    def test_previous_day_tab_is_active_by_default(self):
        html = _render([])

        assert ACTIVE in _tab_class(html, "/publication")
        assert ACTIVE not in _tab_class(html, "/publication?day=today")

    def test_today_tab_is_active_when_selected(self):
        html = _render([], selected_day="today")

        assert ACTIVE in _tab_class(html, "/publication?day=today")
        assert ACTIVE not in _tab_class(html, "/publication")

    def test_previous_day_tab_is_listed_before_today(self):
        html = _render([])

        previous_at = html.find('href="/publication"\n')
        today_at = html.find('href="/publication?day=today"')
        assert 0 < previous_at < today_at

    def test_results_header_shows_the_item_count(self):
        html = _render([_card(1), _card(2), _card(3)])

        assert "(3 items found)" in html

    def test_empty_section_renders_its_message(self):
        html = _render([], empty="Nothing here yet.")

        assert "Nothing here yet." in html

    def test_card_links_to_the_original_article(self):
        html = _render([_card(3)])

        assert 'href="https://example.com/3"' in html
        assert "Article 3" in html

    def test_archived_cover_is_used_when_present(self):
        html = _render([_card(4, cover_file="/publication/covers/x.png")])

        assert 'src="/publication/covers/x.png"' in html
