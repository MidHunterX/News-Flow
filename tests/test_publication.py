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
from app.db.models import PublicationArticle
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


def _insert(title: str) -> int:
    """Add an archived article row, returning its (auto-increment) id."""
    with db_pkg.SessionLocal() as session:
        row = PublicationArticle(
            title=title,
            url=f"https://example.com/{title}",
            image_url=None,
            description="desc",
            published_at="2026-10-31",
            source="kaumudi",
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
