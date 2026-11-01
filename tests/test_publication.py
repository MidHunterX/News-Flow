"""Route tests for the print-publication page (app/routes/publication.py).

Sort behavior: the default ``id`` order is the id-descending order the
queries return, and ``sort=word`` ranks by the scraped body's word count
with unscraped articles last. Everything runs against the throwaway SQLite
database conftest provisions — never the real ``newsflow.db`` and never
the network.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.db as db_pkg
from app.db.models import Article, PublicationArticle
from app.routes import publication as pub_routes


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
            image_url=None,
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


def _card_titles(response) -> list[str]:
    return [card["item"].title for card in response.context["section"]["cards"]]


async def test_default_id_sort_keeps_newest_first(pub_workspace):
    for title in ("First", "Second", "Third"):
        _insert(title)

    response = await pub_routes.publication_view(
        request=object(), day=None, sort="id"
    )
    assert _card_titles(response) == ["Third", "Second", "First"]
    assert response.context["selected_sort"] == "id"


async def test_word_sort_ranks_by_word_count_with_unscraped_last(
    pub_workspace,
):
    short = _insert("Short")
    long = _insert("Long")
    _insert("No copy")  # Newest id, but never scraped → sorts last.
    medium = _insert("Medium")
    _write_body(pub_workspace, short, 3)
    _write_body(pub_workspace, long, 50)
    _write_body(pub_workspace, medium, 10)

    response = await pub_routes.publication_view(
        request=object(), day=None, sort="word"
    )
    assert _card_titles(response) == ["Long", "Medium", "Short", "No copy"]
    assert response.context["selected_sort"] == "word"


async def test_word_sort_keeps_id_order_for_equal_counts(pub_workspace):
    older = _insert("Older")
    newer = _insert("Newer")
    _write_body(pub_workspace, older, 5)
    _write_body(pub_workspace, newer, 5)

    response = await pub_routes.publication_view(
        request=object(), day=None, sort="word"
    )
    assert _card_titles(response) == ["Newer", "Older"]


async def test_unknown_sort_is_rejected(pub_workspace):
    with pytest.raises(HTTPException) as excinfo:
        await pub_routes.publication_view(
            request=object(), day=None, sort="title"
        )
    assert excinfo.value.status_code == 400


async def test_sort_pills_link_both_orders_keeping_the_day(pub_workspace):
    """The pills offer both orders and carry the active day tab along."""
    response = await pub_routes.publication_view(
        request=object(), day="today", sort="word"
    )
    html = response.body.decode()
    # Jinja autoescapes the query separator inside hrefs.
    assert "/publication?day=today&amp;sort=id" in html
    assert "/publication?day=today&amp;sort=word" in html
    # On the default tab there is no day param to carry.
    response = await pub_routes.publication_view(
        request=object(), day=None, sort="id"
    )
    html = response.body.decode()
    assert "/publication?sort=id" in html
    assert "/publication?sort=word" in html


# ---------------------------------------------------------------------------
# Mark as print-published
# ---------------------------------------------------------------------------


def _insert_curation(title: str, **overrides) -> int:
    """Add a *today*-workspace (curation) article row, returning its id."""
    with db_pkg.SessionLocal() as session:
        row = Article(
            title=title,
            url=f"https://example.com/{title}",
            image_url=None,
            description="desc",
            published_at="2026-10-31",
            source="kaumudi",
            **overrides,
        )
        session.add(row)
        session.commit()
        return row.id


async def test_publish_route_marks_todays_curation_row(pub_workspace):
    article_id = _insert_curation("Today article", status="accepted")

    result = await pub_routes.mark_published(article_id=article_id, day="today")
    assert result == {"ok": True}
    with db_pkg.SessionLocal() as session:
        row = session.get(Article, article_id)
        assert row.is_print_published  # Print-published…
        assert row.status == "accepted"  # …while the WP pipeline is untouched.


async def test_publish_route_marks_archived_row(pub_workspace):
    article_id = _insert("Archived article")

    result = await pub_routes.mark_published(
        article_id=article_id, day="previous"
    )
    assert result == {"ok": True}
    with db_pkg.SessionLocal() as session:
        assert session.get(PublicationArticle, article_id).is_print_published


async def test_publish_route_404s_for_unknown_article(pub_workspace):
    for day in ("previous", "today"):
        with pytest.raises(HTTPException) as excinfo:
            await pub_routes.mark_published(article_id=999_999, day=day)
        assert excinfo.value.status_code == 404


async def test_cards_show_print_state_and_offer_the_button_once(pub_workspace):
    """An unmarked card gets the Mark published button; once marked it shows
    the #print badge instead. WP-published cards say #wp, not #published."""
    marked = _insert("Already printed")
    unmarked = _insert("Not printed yet")
    with db_pkg.SessionLocal() as session:
        session.get(PublicationArticle, marked).is_print_published = True
        session.commit()

    response = await pub_routes.publication_view(
        request=object(), day=None, sort="id"
    )
    html = response.body.decode()
    assert "bg-amber-50" in html  # The #print badge on the marked card.
    assert f"/api/publication/{marked}/publish" not in html  # Button gone…
    assert f"/api/publication/{unmarked}/publish" in html  # …still offered.


async def test_wp_completed_card_keeps_its_own_badge(pub_workspace):
    """status=completed means *WordPress*-published — it renders as #wp,
    distinct from the print flag (both may appear together)."""
    _insert("WP-published", status="completed")

    response = await pub_routes.publication_view(
        request=object(), day=None, sort="id"
    )
    html = response.body.decode()
    assert 'title="Published to WordPress by the pipeline"' in html
    assert "#published" not in html  # The old ambiguous label is gone.
