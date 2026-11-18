"""Offline tests for the print-layout sidebar (publication page).

Covers the publication-specific settings in ``app/db/layout.py`` (their own
module on purpose — kept out of the app-wide settings), the whole-layout
save behind the sidebar's drag-and-drop (page assignments + per-page order),
the view context the sidebar renders from (pages, fill feedback, the drag
handles and the card list's unplace drop target), and the JSON export the
InDesign ExtendScript consumes. Everything runs against the throwaway SQLite database conftest
provisions — never the real ``newsflow.db`` and never the network.
"""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

import app.db as db_pkg
import app.routes.scrape as scrape_routes
from app.db.layout import PUBLICATION_LAYOUT_DEFAULTS
from app.db.models import Article, PublicationArticle, Setting
from app.routes import publication as pub_routes

# The three knobs as ints (the defaults dict holds strings, the API and the
# template both speak ints).
DEFAULTS = {key: int(value) for key, value in PUBLICATION_LAYOUT_DEFAULTS.items()}


def _url_for(name: str, **params: str) -> str:
    """The app's Starlette ``url_for``, minus the routing.

    Templates call it for mounted static files (``ai-blob.js``); a bare
    route call has no ASGI app behind it, so only the static route is used.
    """
    if name != "static":
        raise AssertionError(f"tests only stub the static route, got {name!r}")
    return "/static/" + params["path"]


@pytest.fixture
def pub_workspace(tmp_path, monkeypatch):
    """Point the publication content dir at a tmp workspace and stub the
    template's ``url_for`` (no ASGI app behind a bare route call)."""
    content_dir = tmp_path / "publication" / "articles"
    content_dir.mkdir(parents=True)
    monkeypatch.setattr(pub_routes, "PUBLICATION_ARTICLES_DIR", content_dir)
    monkeypatch.setitem(pub_routes.templates.env.globals, "url_for", _url_for)
    return content_dir


def _insert(title: str, **overrides) -> int:
    """Add an archived article row, returning its (auto-increment) id."""
    with db_pkg.SessionLocal() as session:
        row = PublicationArticle(
            title=title,
            url=f"https://example.com/{title}",
            description="desc",
            published_at="2026-10-31",
            source="kaumudi",
            **overrides,
        )
        session.add(row)
        session.commit()
        return row.id


def _insert_curation(title: str, **overrides) -> int:
    """Add a *today*-workspace (curation) article row, returning its id."""
    with db_pkg.SessionLocal() as session:
        row = Article(
            title=title,
            url=f"https://example.com/{title}",
            description="desc",
            published_at="2026-10-31",
            source="kaumudi",
            **overrides,
        )
        session.add(row)
        session.commit()
        return row.id


def _write_body(content_dir, article_id: int, words: int) -> None:
    """Give an article a locally scraped body of *words* words."""
    (content_dir / f"{article_id}.txt").write_text(
        " ".join(["word"] * words), encoding="utf-8"
    )


def _page_of(article_id: int, *, archived: bool) -> int | None:
    """The stored ``page_number`` of a row in the given workspace."""
    model = PublicationArticle if archived else Article
    with db_pkg.SessionLocal() as session:
        return session.get(model, article_id).page_number


def _placement(article_id: int, *, archived: bool) -> tuple[int | None, int | None]:
    """The stored ``(page_number, page_position)`` of a row."""
    model = PublicationArticle if archived else Article
    with db_pkg.SessionLocal() as session:
        row = session.get(model, article_id)
        return row.page_number, row.page_position


async def _view(**kwargs):
    """Call the publication view the way a bare test does."""
    return await pub_routes.publication_view(request=object(), **kwargs)


# ---------------------------------------------------------------------------
# Layout settings data layer (app.db.layout)
# ---------------------------------------------------------------------------


class TestLayoutSettings:
    async def test_defaults_when_unseeded(self):
        assert await db_pkg.get_publication_layout() == DEFAULTS

    async def test_roundtrip(self):
        saved = await db_pkg.set_publication_layout(12, 450, 50)
        assert saved == {"total_pages": 12, "page_words": 450, "word_allowance": 50}
        assert await db_pkg.get_publication_layout() == saved

    @pytest.mark.parametrize(
        ("total_pages", "page_words", "word_allowance"),
        [
            (0, 300, 100),  # a paper has at least one page
            (201, 300, 100),  # …and at most 200
            (8, 0, 100),  # a page holds at least one word
            (8, 100_001, 100),
            (8, 300, -1),
            (8, 300, 100_001),
            (8, 100, 200),  # the allowance may not exceed the quota
            ("8.5", 300, 100),  # whole numbers only
            ("many", 300, 100),
        ],
    )
    async def test_invalid_values_are_rejected_and_write_nothing(
        self, total_pages, page_words, word_allowance
    ):
        with pytest.raises(ValueError):
            await db_pkg.set_publication_layout(
                total_pages, page_words, word_allowance
            )
        assert await db_pkg.get_publication_layout() == DEFAULTS

    async def test_stored_junk_falls_back_and_clamps(self):
        """A hand-edited row can't blow past the bounds or the lower limit."""
        with db_pkg.SessionLocal() as session:
            session.add(Setting(key="total_pages", value="9999"))
            session.add(Setting(key="page_words", value="abc"))
            session.add(Setting(key="word_allowance", value="5000"))
            session.commit()

        layout = await db_pkg.get_publication_layout()

        assert layout["total_pages"] == 200  # clamped into bounds
        assert layout["page_words"] == 300  # unreadable → default
        assert layout["word_allowance"] == layout["page_words"]  # never past quota

    async def test_shrinking_the_paper_unplaces_orphans_in_both_workspaces(self):
        """Lowering total_pages returns the articles that sat on vanished
        pages to the unassigned pool — in the curation *and* the archive."""
        keep_pub, orphan_pub = _insert("Archived keeper"), _insert("Archived orphan")
        keep_cur = _insert_curation("Today keeper")
        orphan_cur = _insert_curation("Today orphan")
        await db_pkg.assign_articles_to_page([keep_pub], 2, archived=True)
        await db_pkg.assign_articles_to_page([orphan_pub], 6, archived=True)
        await db_pkg.assign_articles_to_page([keep_cur], 2, archived=False)
        await db_pkg.assign_articles_to_page([orphan_cur], 6, archived=False)

        await db_pkg.set_publication_layout(3, 300, 100)

        assert (await db_pkg.get_publication_layout())["total_pages"] == 3
        assert _page_of(keep_pub, archived=True) == 2
        assert _page_of(keep_cur, archived=False) == 2
        assert _page_of(orphan_pub, archived=True) is None
        assert _page_of(orphan_cur, archived=False) is None


# ---------------------------------------------------------------------------
# Layout settings endpoints (kept out of /api/settings on purpose)
# ---------------------------------------------------------------------------


class TestLayoutSettingsEndpoint:
    async def test_get_returns_the_defaults(self):
        assert await pub_routes.get_layout_settings() == DEFAULTS

    async def test_post_roundtrip(self):
        payload = pub_routes.PublicationLayoutIn(
            total_pages=12, page_words=450, word_allowance=50
        )
        expected = {"total_pages": 12, "page_words": 450, "word_allowance": 50}

        assert await pub_routes.update_layout_settings(payload) == expected
        assert await pub_routes.get_layout_settings() == expected

    @pytest.mark.parametrize(
        "payload",
        [
            pub_routes.PublicationLayoutIn(
                total_pages=0, page_words=300, word_allowance=100
            ),
            pub_routes.PublicationLayoutIn(
                total_pages=201, page_words=300, word_allowance=100
            ),
            pub_routes.PublicationLayoutIn(
                total_pages=8, page_words=0, word_allowance=100
            ),
            pub_routes.PublicationLayoutIn(
                total_pages=8, page_words=100, word_allowance=200
            ),
        ],
    )
    async def test_post_rejects_bad_values(self, payload):
        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.update_layout_settings(payload)
        assert excinfo.value.status_code == 400
        assert await pub_routes.get_layout_settings() == DEFAULTS  # nothing saved

    async def test_layout_keys_stay_out_of_the_app_settings_api(self):
        """The refactor-away requirement: the app-wide endpoint must not
        know these keys — they belong to the print layout only."""
        with pytest.raises(HTTPException) as excinfo:
            await scrape_routes.update_settings({"total_pages": "4"})
        assert excinfo.value.status_code == 400
        assert "Unknown setting" in excinfo.value.detail


# ---------------------------------------------------------------------------
# Grouping endpoints (legacy per-page assign/unassign API)
# ---------------------------------------------------------------------------


class TestAssignEndpoints:
    async def test_assigns_archived_articles_to_a_page(self):
        first, second = _insert("First"), _insert("Second")

        result = await pub_routes.assign_articles(
            pub_routes.PageAssignIn(page=3, article_ids=[first, second]),
            day="previous",
        )

        assert result == {"ok": True, "page": 3, "assigned": 2}
        assert _page_of(first, archived=True) == 3
        assert _page_of(second, archived=True) == 3

    async def test_assigns_todays_articles_when_the_day_is_not_previous(self):
        article_id = _insert_curation("Today article")

        await pub_routes.assign_articles(
            pub_routes.PageAssignIn(page=1, article_ids=[article_id])
        )

        assert _page_of(article_id, archived=False) == 1

    async def test_unknown_ids_404_and_write_nothing(self):
        """All-or-nothing: a stale picker list can't half-apply a batch."""
        real = _insert("Real")

        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.assign_articles(
                pub_routes.PageAssignIn(page=1, article_ids=[real, 999_999]),
                day="previous",
            )

        assert excinfo.value.status_code == 404
        assert "999999" in str(excinfo.value.detail)
        assert _page_of(real, archived=True) is None

    async def test_page_outside_the_layout_is_rejected(self):
        article_id = _insert("A")

        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.assign_articles(
                pub_routes.PageAssignIn(page=999, article_ids=[article_id]),
                day="previous",
            )

        assert excinfo.value.status_code == 400
        assert _page_of(article_id, archived=True) is None

    async def test_empty_selection_is_rejected(self):
        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.assign_articles(
                pub_routes.PageAssignIn(page=1, article_ids=[]),
                day="previous",
            )
        assert excinfo.value.status_code == 400

    async def test_reassigning_moves_the_article(self):
        article_id = _insert("A")
        await pub_routes.assign_articles(
            pub_routes.PageAssignIn(page=1, article_ids=[article_id]),
            day="previous",
        )

        await pub_routes.assign_articles(
            pub_routes.PageAssignIn(page=4, article_ids=[article_id]),
            day="previous",
        )

        assert _page_of(article_id, archived=True) == 4

    async def test_unassign_returns_it_to_the_pool(self):
        article_id = _insert("A")
        await pub_routes.assign_articles(
            pub_routes.PageAssignIn(page=2, article_ids=[article_id]),
            day="previous",
        )

        result = await pub_routes.unassign_article(
            article_id=article_id, day="previous"
        )

        assert result == {"ok": True}
        assert _page_of(article_id, archived=True) is None

    async def test_unassign_404s_for_unknown_article(self):
        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.unassign_article(
                article_id=999_999, day="previous"
            )
        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Whole-layout save (the sidebar's drag-and-drop)
# ---------------------------------------------------------------------------


class TestSaveLayout:
    """POST /api/publication/layout replaces every page in one write."""

    async def test_saves_pages_and_the_order_inside_each(self):
        first, second, third = _insert("A"), _insert("B"), _insert("C")

        result = await pub_routes.save_layout(
            pub_routes.PageLayoutIn(
                pages=[
                    pub_routes.PageLayoutPageIn(
                        page=2, article_ids=[third, first]
                    ),
                    pub_routes.PageLayoutPageIn(page=5, article_ids=[second]),
                ]
            ),
            day="previous",
        )

        assert result == {"ok": True, "pages": 2, "placed": 3}
        assert _placement(third, archived=True) == (2, 0)
        assert _placement(first, archived=True) == (2, 1)
        assert _placement(second, archived=True) == (5, 0)

    async def test_is_a_full_replacement(self):
        """Articles missing from the payload (dragged to the free pool)
        return to unassigned, and earlier positions are cleared."""
        kept, dropped = _insert("Kept"), _insert("Dropped")
        await db_pkg.assign_articles_to_page([kept, dropped], 1, archived=True)

        await pub_routes.save_layout(
            pub_routes.PageLayoutIn(
                pages=[
                    pub_routes.PageLayoutPageIn(page=3, article_ids=[kept])
                ]
            ),
            day="previous",
        )

        assert _placement(kept, archived=True) == (3, 0)
        assert _placement(dropped, archived=True) == (None, None)

    async def test_empty_layout_clears_every_page(self):
        article_id = _insert("A")
        await db_pkg.assign_articles_to_page([article_id], 1, archived=True)

        result = await pub_routes.save_layout(
            pub_routes.PageLayoutIn(pages=[]), day="previous"
        )

        assert result == {"ok": True, "pages": 0, "placed": 0}
        assert _placement(article_id, archived=True) == (None, None)

    async def test_saves_todays_workspace_when_day_is_not_previous(self):
        article_id = _insert_curation("Today article")

        await pub_routes.save_layout(
            pub_routes.PageLayoutIn(
                pages=[
                    pub_routes.PageLayoutPageIn(page=1, article_ids=[article_id])
                ]
            )
        )

        assert _placement(article_id, archived=False) == (1, 0)

    async def test_unknown_ids_404_and_write_nothing(self):
        real = _insert("Real")

        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.save_layout(
                pub_routes.PageLayoutIn(
                    pages=[
                        pub_routes.PageLayoutPageIn(
                            page=1, article_ids=[real, 999_999]
                        )
                    ]
                ),
                day="previous",
            )

        assert excinfo.value.status_code == 404
        assert "999999" in str(excinfo.value.detail)
        assert _placement(real, archived=True) == (None, None)

    async def test_duplicate_articles_are_rejected(self):
        article_id = _insert("A")

        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.save_layout(
                pub_routes.PageLayoutIn(
                    pages=[
                        pub_routes.PageLayoutPageIn(page=1, article_ids=[article_id]),
                        pub_routes.PageLayoutPageIn(page=2, article_ids=[article_id]),
                    ]
                ),
                day="previous",
            )

        assert excinfo.value.status_code == 400
        assert "only be placed on one page" in str(excinfo.value.detail)
        assert _placement(article_id, archived=True) == (None, None)

    async def test_same_page_twice_is_rejected(self):
        first, second = _insert("A"), _insert("B")

        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.save_layout(
                pub_routes.PageLayoutIn(
                    pages=[
                        pub_routes.PageLayoutPageIn(page=1, article_ids=[first]),
                        pub_routes.PageLayoutPageIn(page=1, article_ids=[second]),
                    ]
                ),
                day="previous",
            )

        assert excinfo.value.status_code == 400
        assert "only appear once" in str(excinfo.value.detail)
        assert _placement(first, archived=True) == (None, None)

    async def test_page_outside_the_layout_is_rejected(self):
        article_id = _insert("A")

        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.save_layout(
                pub_routes.PageLayoutIn(
                    pages=[
                        pub_routes.PageLayoutPageIn(
                            page=999, article_ids=[article_id]
                        )
                    ]
                ),
                day="previous",
            )

        assert excinfo.value.status_code == 400
        assert _placement(article_id, archived=True) == (None, None)


# ---------------------------------------------------------------------------
# Sidebar view context (pages, fill feedback, the free pool)
# ---------------------------------------------------------------------------


class TestSidebarView:
    async def test_pages_group_the_placed_cards_with_fill_feedback(
        self, pub_workspace
    ):
        first, second = _insert("First"), _insert("Second")
        loose = _insert("Loose")
        await db_pkg.assign_articles_to_page([first, second], 1, archived=True)
        await db_pkg.assign_articles_to_page([loose], 2, archived=True)
        _write_body(pub_workspace, first, 100)
        _write_body(pub_workspace, second, 200)

        response = await _view(day=None, sort="id")
        pages = response.context["pages"]

        # Every configured page shows, in order — empty ones included.
        assert [row["page"] for row in pages] == list(range(1, 9))
        assert response.context["layout_settings"] == DEFAULTS

        page_one = pages[0]
        assert {card["item"].title for card in page_one["cards"]} == {
            "First",
            "Second",
        }
        assert page_one["words"] == 300  # 100 + 200
        assert page_one["percent"] == 100  # exactly the quota → green
        assert page_one["color"] == "#22c55e"
        assert page_one["fill"] == 75.0  # bar end = the upper limit (400)
        assert page_one["tick"] == 75.0  # quota hairline

        page_two = pages[1]
        assert [card["item"].title for card in page_two["cards"]] == ["Loose"]
        assert page_two["words"] == 0  # unscraped counts as zero words
        assert page_two["percent"] == 0
        assert page_two["color"] == "#6366f1"  # still filling up → indigo

    async def test_sidebar_has_no_unplaced_pool(self, pub_workspace):
        """Unplacing happens by dropping a chip on the card list, so the
        sidebar has no free-pool section and no context for one."""
        _insert("Placed")

        response = await _view(day=None, sort="id")
        html = response.body.decode()

        assert "unassigned" not in response.context
        assert "Unplaced" not in html
        assert 'data-pub-list="free"' not in html
        assert 'id="pub-free-count"' not in html

    async def test_card_list_marks_placed_articles_with_their_page(
        self, pub_workspace
    ):
        placed = _insert("Placed")
        await db_pkg.assign_articles_to_page([placed], 5, archived=True)

        response = await _view(day=None, sort="id")
        html = response.body.decode()

        assert "#p5" in html
        assert 'title="Placed on page 5 of the print layout"' in html
        # The tag lives in the card (not only in the sidebar) so a drag can
        # retarget it in place.
        assert f'data-pub-card-id="{placed}"' in html
        assert "data-card-page" in html

    async def test_sidebar_chips_link_their_headings_and_drag_by_thumb(
        self, pub_workspace
    ):
        """Chips drag from the thumbnail only, leaving the heading free to
        open the article preview."""
        article_id = _insert("Linked")
        await db_pkg.assign_articles_to_page([article_id], 1, archived=True)

        response = await _view(day=None, sort="id")
        html = response.body.decode()
        pool = html.split('data-pub-list="1"', 1)[1].split("</ul>", 1)[0]

        assert f'<a href="/article/{article_id}?day=previous"' in pool
        assert "data-chip-thumb" in pool

    async def test_cover_image_is_the_drag_handle(self, pub_workspace):
        """Cards are dragged by their image, not the whole card, so the
        heading's preview link stays clickable."""
        article_id = _insert(
            "Draggable", image_url="https://example.com/cover.jpg"
        )

        response = await _view(day=None, sort="id")
        html = response.body.decode()

        # The drag payload rides on the image container, which is grabbable.
        match = re.search(
            r"<div\s+data-pub-drag[^>]*cursor-grab[^>]*>", html, re.DOTALL
        )
        assert match is not None
        block = match.group(0)
        assert f'data-article-id="{article_id}"' in block
        assert 'data-article-title="Draggable"' in block
        assert 'data-article-image="https://example.com/cover.jpg"' in block
        # The fallback grip is not rendered when there is an image.
        assert not re.search(r"<span\s+data-pub-drag", html)
        # The heading is still a plain link, outside the drag handle.
        assert f'<a href="/article/{article_id}?day=previous"' in html

    async def test_image_less_cards_keep_a_grip_handle(self, pub_workspace):
        """Without a cover there is nothing to grab, so a small grip stands
        in — every article stays placeable."""
        article_id = _insert("No cover")

        response = await _view(day=None, sort="id")
        html = response.body.decode()

        assert re.search(r"<span\s+data-pub-drag", html)
        assert f'data-article-id="{article_id}"' in html
        assert 'data-article-title="No cover"' in html

    async def test_pages_render_in_the_saved_drag_order(self, pub_workspace):
        """The view follows ``page_position`` (set by Save layout), not the
        workspace's default newest-id-first order."""
        first, second = _insert("First"), _insert("Second")
        await pub_routes.save_layout(
            pub_routes.PageLayoutIn(
                pages=[
                    pub_routes.PageLayoutPageIn(
                        page=1, article_ids=[first, second]
                    )
                ]
            ),
            day="previous",
        )

        response = await _view(day=None, sort="id")
        titles = [card["item"].title for card in response.context["pages"][0]["cards"]]

        assert titles == ["First", "Second"]

    async def test_sidebar_offers_settings_drag_and_save(self, pub_workspace):
        response = await _view(day=None, sort="id")
        html = response.body.decode()

        assert 'onclick="openPubSettings()"' in html  # gear → settings modal
        assert 'id="pub-settings-modal"' in html
        assert 'id="pub-slider"' in html  # quota slider + allowance ring
        assert 'id="save-layout-button"' in html
        assert "Save layout" in html
        assert "@shopify/draggable" in html  # the DnD bundle
        assert 'data-pub-list="1"' in html  # a page's sortable list
        assert "data-pub-page-drop" in html  # a card's drop target
        assert "data-pub-cards" in html  # the unplace drop target
        # The plus-button picker, the Export JSON button and the old
        # unplaced pool are gone.
        assert 'id="pub-picker-modal"' not in html
        assert 'id="pub-export-modal"' not in html
        assert "Export JSON" not in html
        assert "Unplaced" not in html
        assert "300 wc/page" in html  # header echoes the layout settings
        assert "8 pages" in html


# ---------------------------------------------------------------------------
# Fill-bar color (the sidebar's tight/loose feedback)
# ---------------------------------------------------------------------------


class TestFillColor:
    def test_under_quota_is_still_filling_up(self):
        assert pub_routes._fill_color(0, 300, 100) == "#6366f1"
        assert pub_routes._fill_color(99, 300, 100) == "#6366f1"

    def test_exactly_the_quota_is_green(self):
        assert pub_routes._fill_color(100, 300, 100) == "#22c55e"

    def test_eases_through_orange_to_red_at_the_upper_limit(self):
        # allowance == quota puts the upper limit at 200%, so 150% is
        # exactly the orange hinge between green and red.
        assert pub_routes._fill_color(125, 300, 300) not in {
            "#6366f1",
            "#22c55e",
            "#f97316",
            "#ef4444",
        }
        assert pub_routes._fill_color(150, 300, 300) == "#f97316"
        upper = 100 + 300 / 300 * 100
        assert pub_routes._fill_color(upper, 300, 300) == "#ef4444"
        assert pub_routes._fill_color(250, 300, 300) == "#ef4444"

    def test_no_allowance_means_over_quota_is_immediately_red(self):
        assert pub_routes._fill_color(100, 300, 0) == "#ef4444"


# ---------------------------------------------------------------------------
# Export: the JSON an ExtendScript places into InDesign
# ---------------------------------------------------------------------------


class TestExport:
    async def test_shape_counts_unassigned_and_lists_every_page(
        self, pub_workspace
    ):
        placed = _insert("Placed")
        _insert("Free")  # never placed → counted, but not exported
        await db_pkg.assign_articles_to_page([placed], 3, archived=True)
        _write_body(pub_workspace, placed, 40)

        payload = await pub_routes.export_layout(day="previous")

        assert payload["version"] == 1
        assert payload["day"] == "previous"
        assert payload["settings"] == {
            **DEFAULTS,
            "lower_limit": DEFAULTS["page_words"] - DEFAULTS["word_allowance"],
            "upper_limit": DEFAULTS["page_words"] + DEFAULTS["word_allowance"],
        }
        assert payload["unassigned_count"] == 1
        assert [row["page"] for row in payload["pages"]] == list(range(1, 9))
        assert all(
            row["articles"] == []
            for row in payload["pages"]
            if row["page"] != 3
        )
        page_three = payload["pages"][2]
        assert [article["id"] for article in page_three["articles"]] == [placed]
        assert page_three["word_count"] == 40
        assert page_three["fill_percent"] == round(40 / 300 * 100, 1)

    async def test_article_splits_heading_from_body(self, pub_workspace):
        article_id = _insert("Stored title")
        await db_pkg.assign_articles_to_page([article_id], 1, archived=True)
        body = " ".join(f"w{i}" for i in range(10))
        (pub_workspace / f"{article_id}.txt").write_text(
            f"Real heading\n\n{body}", encoding="utf-8"
        )

        payload = await pub_routes.export_layout(day="previous")
        article = payload["pages"][0]["articles"][0]

        assert article["heading"] == "Real heading"
        assert article["body"] == body
        assert article["word_count"] == 12  # 2 heading + 10 body, like the bar
        assert article["title"] == "Stored title"
        assert article["url"].startswith("https://example.com/")
        assert article["source"] == "kaumudi"

    async def test_unscraped_article_exports_with_no_body(self, pub_workspace):
        article_id = _insert("Never scraped")
        await db_pkg.assign_articles_to_page([article_id], 1, archived=True)

        payload = await pub_routes.export_layout(day="previous")
        article = payload["pages"][0]["articles"][0]

        assert article["heading"] == "Never scraped"  # the stored title stands in
        assert article["body"] == ""
        assert article["word_count"] is None
        assert article["cover"] is None

    async def test_cover_carries_url_path_and_pixels(
        self, pub_workspace, tmp_path, monkeypatch
    ):
        covers = tmp_path / "publication" / "covers"
        covers.mkdir(parents=True)
        monkeypatch.setattr(pub_routes, "PUBLICATION_COVERS_DIR", covers)
        Image.new("RGB", (40, 20)).save(covers / "img.png")
        article_id = _insert("With cover", cover_file="/covers/img.png")
        await db_pkg.assign_articles_to_page([article_id], 1, archived=True)

        payload = await pub_routes.export_layout(day="previous")
        cover = payload["pages"][0]["articles"][0]["cover"]

        assert cover["url"] == "/publication/covers/img.png"  # mirrored workspace
        assert cover["file"] == str((covers / "img.png").resolve())
        assert (cover["width"], cover["height"]) == (40, 20)

    async def test_missing_cover_file_exports_null_dimensions(
        self, pub_workspace, tmp_path, monkeypatch
    ):
        covers = tmp_path / "publication" / "covers"
        covers.mkdir(parents=True)
        monkeypatch.setattr(pub_routes, "PUBLICATION_COVERS_DIR", covers)
        article_id = _insert("Lost cover", cover_file="/covers/gone.png")
        await db_pkg.assign_articles_to_page([article_id], 1, archived=True)

        payload = await pub_routes.export_layout(day="previous")
        cover = payload["pages"][0]["articles"][0]["cover"]

        assert cover["url"] == "/publication/covers/gone.png"
        assert cover["width"] is None
        assert cover["height"] is None

    async def test_today_export_reads_the_curation_workspace(
        self, monkeypatch, tmp_path
    ):
        articles_dir = tmp_path / "articles"
        articles_dir.mkdir()
        monkeypatch.setattr(pub_routes, "ARTICLES_DIR", articles_dir)
        article_id = _insert_curation("Today placed")
        await db_pkg.assign_articles_to_page([article_id], 1, archived=False)
        (articles_dir / f"{article_id}.txt").write_text(
            "Head\n\nbody words", encoding="utf-8"
        )

        payload = await pub_routes.export_layout()

        assert payload["day"] == "today"
        article = payload["pages"][0]["articles"][0]
        assert article["heading"] == "Head"
        assert article["body"] == "body words"
        assert article["word_count"] == 3


# ---------------------------------------------------------------------------
# Bare template render (the sidebar context is optional)
# ---------------------------------------------------------------------------


def _render_bare() -> str:
    """publication.html with just the context a bare test render needs."""
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["url_for"] = _url_for
    return env.get_template("publication.html").render(
        toggles={},
        article_layout="grid",
        selected_day="previous",
        count=0,
        section={
            "label": "Previous day",
            "note": "Archived by the daily reset",
            "empty": "Nothing archived yet",
            "cards": [],
        },
    )


class TestBareRender:
    """Regression: both template blocks fall back to the sidebar defaults,
    so a minimal render (tests that don't pass the sidebar context) never
    trips over an Undefined."""

    def test_content_block_uses_the_sidebar_defaults(self):
        html = _render_bare()
        assert "300 wc/page" in html
        assert "8 pages" in html
        assert "No pages configured" in html  # pages defaults to []
        assert "Undefined" not in html

    def test_scripts_block_repeats_the_defaults(self):
        """The scripts block has its own scope — it must set the defaults
        again or ``pubLayout`` would serialize an Undefined."""
        html = _render_bare()
        # Jinja's tojson sorts the keys.
        assert (
            'const pubLayout = {"page_words": 300, "total_pages": 8, '
            '"word_allowance": 100};' in html
        )
        assert "Undefined" not in html


# ---------------------------------------------------------------------------
# Drag-and-drop wiring (static checks — there is no JS runtime offline)
# ---------------------------------------------------------------------------


class TestDragAndDropWiring:
    def test_uses_the_draggable_bundle_sortable_and_draggable(self):
        html = _render_bare()
        assert "new Draggable.Sortable(" in html  # pool + page lists
        assert "new Draggable.Draggable(" in html  # card grips
        # The settled DOM (source for mirror) only exists after this event.
        assert '"drag:stopped"' in html
        assert '"sortable:stop"' not in html
        assert 'data-pub-list' in html  # the sortable containers
        assert 'data-pub-list="free"' not in html  # no unplaced pool
        assert "cardListAt(lastChipPoint)" in html  # drop back on the cards
        assert "chip.remove()" in html  # …which unplaces it
        assert 'handle: "[data-chip-thumb]"' in html  # chips drag by image

    def test_save_is_enabled_only_when_the_layout_changes(self):
        html = _render_bare()
        assert "const savedLayout = layoutSnapshot();" in html
        assert "layoutSnapshot() !== savedLayout" in html
        assert "button.disabled = !layoutDirty;" in html

    def test_moved_cards_sync_opacity_and_page_tag(self):
        html = _render_bare()
        assert "syncCardState(article.id, pageOfList(targetList))" in html
        assert "syncCardState(articleId, page)" in html
        assert 'card.classList.toggle("opacity-25", Boolean(page))' in html
        assert 'badge.textContent = `#p${page}`' in html

    def test_save_posts_every_page_in_display_order(self):
        html = _render_bare()
        assert "/api/publication/layout?day=" in html
        assert 'document.querySelectorAll("[data-sidebar-page]")' in html
        assert "article_ids:" in html

    def test_unsaved_layout_guards_against_reload(self):
        assert 'window.addEventListener("beforeunload"' in _render_bare()
