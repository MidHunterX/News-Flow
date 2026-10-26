"""Offline tests for the print-publication page (``GET /publication``).

Covers the card data assembly (local-scrape flag + word count), the route
that reads both workspaces, and the template branches. No network: the
route only reads the local database and files.
"""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader, select_autoescape

import app.db as db_pkg
import app.routes.scrape as scrape_routes
from app.db.models import Article, PublicationArticle
from app.models import NewsItem


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


def _render(sections) -> str:
    """Render publication.html directly, bypassing FastAPI entirely.

    *sections* is a list of ``(label, cards)`` pairs; the other header
    fields get filler values since the assertions don't depend on them.
    """
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("publication.html")
    context = {
        "toggles": {},
        "sections": [
            {
                "label": label,
                "note": "note",
                "cards": cards,
                "empty": "Nothing here yet.",
            }
            for label, cards in sections
        ],
    }
    return template.render(**context)


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


class TestPublicationRoute:
    async def test_shows_today_and_previous_day_with_word_counts(
        self, monkeypatch, tmp_path
    ):
        curation_dir, publication_dir = _bind_workspaces(monkeypatch, tmp_path)

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

        response = await scrape_routes.publication_view(request=object())
        body = response.body.decode()

        assert response.status_code == 200
        # Both sections render, each with its own article.
        assert "Today" in body and "Previous day" in body
        assert "Today Story" in body and "Yesterday Story" in body
        # Local-scrape flag with a per-card word count.
        assert "Locally scraped" in body
        assert "4 words" in body
        assert "5 words" in body
        # The archived card's cover is served from the publication workspace.
        assert "/publication/covers/old.png" in body

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

        response = await scrape_routes.publication_view(request=object())
        body = response.body.decode()

        assert response.status_code == 200
        assert "Not scraped locally" in body
        assert "Locally scraped" not in body

    async def test_empty_workspaces_render_the_empty_states(
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

        response = await scrape_routes.publication_view(request=object())
        body = response.body.decode()

        assert response.status_code == 200
        assert "No articles in today&#39;s workspace yet." in body
        assert "Nothing archived yet" in body


# ---------------------------------------------------------------------------
# Template branch coverage (no route involved)
# ---------------------------------------------------------------------------


class TestPublicationTemplate:
    def test_scraped_card_shows_the_word_count(self):
        html = _render([("Today", [_card(1, word_count=123)])])

        assert "Locally scraped" in html
        assert "123 words" in html

    def test_card_without_content_renders_not_scraped(self):
        html = _render([("Previous day", [_card(2)])])

        assert "Not scraped locally" in html
        assert "Locally scraped" not in html

    def test_empty_section_renders_its_message(self):
        html = _render([("Today", [])])

        assert "Nothing here yet." in html

    def test_card_links_to_the_original_article(self):
        html = _render([("Today", [_card(3)])])

        assert 'href="https://example.com/3"' in html
        assert "Article 3" in html

    def test_archived_cover_is_used_when_present(self):
        html = _render(
            [("Previous day", [_card(4, cover_file="/publication/covers/x.png")])]
        )

        assert 'src="/publication/covers/x.png"' in html
