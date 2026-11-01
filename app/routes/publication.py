"""Routes for the print-publication inspection page.

Split out of the scrape router: ``GET /publication`` reads the archive (or
today's curation workspace) and renders it — it neither scrapes nor mutates
anything, so it doesn't belong with the scraping API. Shared helpers it
still needs (``_toggle_context`` for the settings modal, ``_archived_cover``
for previewing archived rows) stay with the article view in
``app.routes.scrape``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import get_article_layout, get_items, get_publication_items
from app.models import NewsItem
from app.routes.scrape import _archived_cover, _toggle_context
from app.utils import ARTICLES_DIR, PUBLICATION_ARTICLES_DIR

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _word_count(path: Path) -> int | None:
    """Word count of a locally scraped article body, or None when it's absent."""
    if not path.exists():
        return None
    try:
        return len(path.read_text(encoding="utf-8").split())
    except (OSError, UnicodeError):
        return None  # Unreadable counts as not scraped; the card just says so.


def _build_cards(
    items: list[NewsItem], content_dir: Path, *, archived: bool
) -> list[dict]:
    """Assemble the publication page's card data.

    Each card carries the scraped-content word count (None when nothing has
    saved a body for that article yet) and the cover URL to render.
    Archived cards read their files from the publication workspace and get
    their cover URL pointed at ``/publication/covers/``, since the curation
    copy of both is wiped at the daily reset.
    """
    cards = []
    for item in items:
        cover = _archived_cover(item.cover_file) if archived else item.cover_file
        cards.append(
            {
                "item": item,
                "cover": cover,
                "word_count": (
                    _word_count(content_dir / f"{item.id}.txt")
                    if item.id is not None
                    else None
                ),
            }
        )
    return cards


@router.get("/publication", response_class=HTMLResponse)
async def publication_view(
    request: Request,
    day: str | None = Query(
        None, description="Tab to show: 'today' for the ongoing workspace"
    ),
):
    """Read-only view of the print-publication workspace.

    Structured like the curation UI: a "Previous day" / "Today" pill
    switch (previous day first and the default), a results header, and the
    same ``article_layout`` branches (grid / rows / compact). The data comes
    from today's curation workspace or the previous day's archive
    (``publication_articles`` + ``public/publication/`` files). Each card
    flags whether the article body was scraped locally and, when it was, its
    word count. Nothing is scraped or mutated from here — inspection only.
    """
    show_today = day == "today"
    if show_today:
        cards = _build_cards(await get_items(), ARTICLES_DIR, archived=False)
        section = {
            "label": "Today",
            "note": "ongoing — curation workspace",
            "empty": "No articles in today's workspace yet.",
        }
    else:
        cards = _build_cards(
            await get_publication_items(), PUBLICATION_ARTICLES_DIR, archived=True
        )
        section = {
            "label": "Previous day",
            "note": "Archived by the daily reset",
            "empty": "Nothing archived yet",
        }

    return templates.TemplateResponse(
        request=request,
        name="publication.html",
        context={
            "request": request,
            "active_nav": "publication",
            "selected_day": "today" if show_today else "previous",
            "section": {**section, "cards": cards},
            "count": len(cards),
            "article_layout": await get_article_layout(),
            **await _toggle_context(),
        },
    )
