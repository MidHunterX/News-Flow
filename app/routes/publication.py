"""Routes for the print-publication inspection page.

Split out of the scrape router: ``GET /publication`` reads the archive (or
today's curation workspace) and renders it — it neither scrapes nor mutates
anything, so it doesn't belong with the scraping API. Shared helpers it
still needs (``_toggle_context`` for the settings modal, ``_archived_cover``
for previewing archived rows) stay with the article view in
``app.routes.scrape``.

The same router also serves the InDesign tooling around that page: the
publication-specific layout settings, grouping articles onto pages, and
the JSON export an ExtendScript consumes (see ``app.db.layout`` for the
settings themselves).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.db import (
    assign_articles_to_page,
    clear_article_page,
    get_article_layout,
    get_items,
    get_publication_items,
    get_publication_layout,
    set_print_published,
    set_publication_layout,
)
from app.db.constants import now_iso
from app.models import NewsItem
from app.routes.scrape import _archived_cover, _toggle_context
from app.utils import (
    ARTICLES_DIR,
    COVERS_DIR,
    PUBLICATION_ARTICLES_DIR,
    PUBLICATION_COVERS_DIR,
    image_dimensions,
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Card orders the publication page's sort pills switch between: "id" keeps
# the default id-descending order the queries return, "word" ranks by the
# scraped body's word count.
PUBLICATION_SORTS = ("id", "word")


class PublicationLayoutIn(BaseModel):
    """Body of POST /api/publication/settings — three whole numbers."""

    total_pages: int
    page_words: int
    word_allowance: int


class PageAssignIn(BaseModel):
    """Body of POST /api/publication/assign — group articles onto a page."""

    page: int
    article_ids: list[int]


def _word_count(path: Path) -> int | None:
    """Word count of a locally scraped article body, or None when it's absent."""
    if not path.exists():
        return None
    try:
        return len(path.read_text(encoding="utf-8").split())
    except (OSError, UnicodeError):
        return None  # Unreadable counts as not scraped; the card just says so.


def _sort_cards(cards: list[dict], sort: str) -> list[dict]:
    """Order the page's cards per the requested sort.

    ``id`` (the default) returns them untouched — the queries already come
    back newest-id-first. ``word`` ranks scraped articles by word count,
    biggest first, with unscraped ones (``None``) last; the sort is stable,
    so equal counts keep the id order.
    """
    if sort != "word":
        return cards
    return sorted(
        cards,
        key=lambda card: (
            card["word_count"] if card["word_count"] is not None else -1
        ),
        reverse=True,
    )


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


def _lerp_hex(start: str, end: str, t: float) -> str:
    """Linear blend between two #rrggbb colors (t in [0, 1])."""
    lo = [int(start[i : i + 2], 16) for i in (1, 3, 5)]
    hi = [int(end[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(
        f"{round(a + (b - a) * t):02x}" for a, b in zip(lo, hi, strict=True)
    )


def _fill_color(percent: float, page_words: int, allowance: int) -> str:
    """Bar color for a page's fill — the sidebar's tight/loose feedback.

    At exactly 100% (the quota) the page is green; from there it eases
    through orange to red at the upper limit (page_words + allowance) and
    stays red beyond it. Under quota the page is still filling up, which
    reads as the app's indigo.
    """
    if percent < 100:
        return "#6366f1"
    # How many percentage points separate the quota from the upper limit.
    span = allowance / page_words * 100
    if span <= 0 or percent >= 100 + span:
        return "#ef4444"
    t = (percent - 100) / span
    if t < 0.5:
        return _lerp_hex("#22c55e", "#f97316", t * 2)  # green → orange
    return _lerp_hex("#f97316", "#ef4444", (t - 0.5) * 2)  # orange → red


def _build_pages(cards: list[dict], layout: dict[str, int]) -> list[dict]:
    """Group the page's cards into the sidebar's per-page rows.

    Every configured page appears (even an empty one) with its assigned
    cards, the summed scraped word count, the fill percentage against
    ``page_words``, the bar geometry — fill width and quota tick, both
    relative to the *upper* limit so the track's end really is "too
    tight" — and the color from :func:`_fill_color`. Unscraped articles
    count as zero words. A card pointing past ``total_pages`` (only
    reachable if the settings moved underneath it) is clamped into the
    last page rather than hidden.
    """
    total_pages = layout["total_pages"]
    page_words = layout["page_words"]
    upper = page_words + layout["word_allowance"]
    grouped: dict[int, list[dict]] = {
        page: [] for page in range(1, total_pages + 1)
    }
    for card in cards:
        page = card["item"].page_number
        if page is None:
            continue
        grouped[min(max(page, 1), total_pages)].append(card)

    pages = []
    for page, page_cards in grouped.items():
        words = sum(card["word_count"] or 0 for card in page_cards)
        percent = words / page_words * 100
        pages.append(
            {
                "page": page,
                "cards": page_cards,
                "words": words,
                "percent": round(percent),
                "fill": min(words / upper * 100, 100),
                "tick": page_words / upper * 100,
                "color": _fill_color(
                    percent, page_words, layout["word_allowance"]
                ),
            }
        )
    return pages


def _unassigned_cards(cards: list[dict]) -> list[dict]:
    """Picker payload: cards not grouped onto any page yet.

    Assigned articles are deliberately absent — the sidebar already
    shows them beside their page, and the picker only ever adds.
    """
    return [
        {
            "id": card["item"].id,
            "title": card["item"].title,
            "source": card["item"].source,
            "image": card["cover"] or card["item"].image_url,
            "word_count": card["word_count"],
            "status": card["item"].status,
        }
        for card in cards
        if card["item"].page_number is None
    ]


@router.get("/publication", response_class=HTMLResponse)
async def publication_view(
    request: Request,
    day: str | None = Query(
        None, description="Tab to show: 'today' for the ongoing workspace"
    ),
    sort: str = Query(
        "id",
        description="Card order: 'id' (default, newest id first) or "
        "'word' (scraped word count, biggest first)",
    ),
):
    """Read-only view of the print-publication workspace.

    Structured like the curation UI: a "Previous day" / "Today" pill
    switch (previous day first and the default), a results header, and the
    same ``article_layout`` branches (grid / rows / compact). The data comes
    from today's curation workspace or the previous day's archive
    (``publication_articles`` + ``public/publication/`` files). Each card
    flags whether the article body was scraped locally and, when it was, its
    word count. ``sort`` picks the card order (see :func:`_sort_cards`).
    Nothing is scraped or mutated from here — inspection only.

    The right-hand sidebar adds the print-layout tooling: the configured
    pages with their grouped articles and fill feedback
    (``layout_settings`` + ``pages``) and the unassigned cards the
    picker modal offers (``unassigned``).
    """
    if sort not in PUBLICATION_SORTS:
        raise HTTPException(400, f"sort must be one of: {', '.join(PUBLICATION_SORTS)}")
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
    cards = _sort_cards(cards, sort)
    layout = await get_publication_layout()

    return templates.TemplateResponse(
        request=request,
        name="publication.html",
        context={
            "request": request,
            "active_nav": "publication",
            "selected_day": "today" if show_today else "previous",
            "selected_sort": sort,
            "section": {**section, "cards": cards},
            "count": len(cards),
            "article_layout": await get_article_layout(),
            # Sidebar: the layout knobs, the grouped pages, and the cards
            # still waiting to be placed (the picker modal's list).
            "layout_settings": layout,
            "pages": _build_pages(cards, layout),
            "unassigned": _unassigned_cards(cards),
            **await _toggle_context(),
        },
    )


@router.post("/api/publication/{article_id}/publish")
async def mark_published(
    article_id: int,
    day: str | None = Query(
        None, description="'previous' to mark an archived publication article"
    ),
    # Plain default (not Query) so calling the handler directly passes a real
    # bool; FastAPI still parses it as an optional query parameter, where
    # published=false clears the mark (unmark) instead of setting it.
    published: bool = True,
):
    """Set — or, with ``published=false``, clear — the print-published mark.

    "Published" on this page means *printed* — distinct from the WordPress
    pipeline, where the ``completed`` status is what means published. The
    flag lands on the row the given tab shows: ``day=previous`` targets the
    archived ``publication_articles`` row, anything else today's curation
    row. Returns 404 when the row doesn't exist; the article's status is
    never touched.
    """
    if not await set_print_published(
        article_id, published, archived=day == "previous"
    ):
        raise HTTPException(404, "Article not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Print-layout settings (publication-specific — see app.db.layout)
# ---------------------------------------------------------------------------


@router.get("/api/publication/settings")
async def get_layout_settings():
    """The three layout knobs: total_pages, page_words, word_allowance.

    Kept away from ``/api/settings`` on purpose — these belong to the
    print layout, not the app.
    """
    return await get_publication_layout()


@router.post("/api/publication/settings")
async def update_layout_settings(layout: PublicationLayoutIn):
    """Save the print-layout settings; 400 when a value is out of range.

    Lowering ``total_pages`` also un-places the articles that sat on the
    now-gone pages (they return to the unassigned pool), so the sidebar
    never points at a page that doesn't exist.
    """
    try:
        return await set_publication_layout(
            layout.total_pages, layout.page_words, layout.word_allowance
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# Grouping articles into pages (the sidebar's picker + remove buttons)
# ---------------------------------------------------------------------------


@router.post("/api/publication/assign")
async def assign_articles(
    payload: PageAssignIn,
    day: str | None = Query(
        None, description="'previous' to group archived publication articles"
    ),
):
    """Put articles on one page of the layout (the picker's Add button).

    All-or-nothing: when any id is unknown nothing is written and the
    route answers 404 naming the ids — a stale picker list can't
    half-apply a selection. An article already on another page is moved.
    """
    if not payload.article_ids:
        raise HTTPException(400, "article_ids must not be empty")
    try:
        missing = await assign_articles_to_page(
            payload.article_ids, payload.page, archived=day == "previous"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if missing:
        raise HTTPException(
            404,
            "Articles not found: " + ", ".join(str(i) for i in missing),
        )
    return {
        "ok": True,
        "page": payload.page,
        "assigned": len(set(payload.article_ids)),
    }


@router.post("/api/publication/{article_id}/unassign")
async def unassign_article(
    article_id: int,
    day: str | None = Query(
        None, description="'previous' to ungroup an archived publication article"
    ),
):
    """Take an article off its page, back into the unassigned pool."""
    if not await clear_article_page(article_id, archived=day == "previous"):
        raise HTTPException(404, "Article not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Export: the JSON an ExtendScript places into InDesign
# ---------------------------------------------------------------------------


def _scraped_content(
    content_dir: Path, item: NewsItem
) -> tuple[str, str, int | None]:
    """(heading, body, word_count) of an article's local copy.

    The workspace file is "heading\\n\\nbody" (written by the accept
    flow). Without one — the card says "Not scraped" — the stored title
    stands in as the heading, the body is empty and the word count is
    None. ``word_count`` counts the whole file, exactly what the sidebar
    fills pages by, so the JSON and the bar can never disagree.
    """
    if item.id is None:
        return item.title, "", None
    path = content_dir / f"{item.id}.txt"
    if not path.exists():
        return item.title, "", None
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return item.title, "", None
    parts = raw.split("\n\n", 1)
    heading = parts[0].strip() or item.title
    body = parts[1].strip() if len(parts) > 1 else ""
    return heading, body, len(raw.split())


def _export_cover(cover_file: str | None, archived: bool) -> dict | None:
    """Cover of an export article: web URL, on-disk path, and pixel size.

    The rows are copied verbatim by the daily reset, so a mirrored
    ``/covers/...`` path resolves into the publication workspace's own
    covers directory. Dimensions come back as null when the file is
    missing or unreadable — the export still ships.
    """
    if not cover_file:
        return None
    name = cover_file.rsplit("/", 1)[-1]
    directory = PUBLICATION_COVERS_DIR if archived else COVERS_DIR
    path = directory / name
    dimensions = image_dimensions(path)
    return {
        "url": _archived_cover(cover_file) if archived else cover_file,
        # Absolute, so ExtendScript can link the file directly.
        "file": str(path.resolve()),
        "width": dimensions[0] if dimensions else None,
        "height": dimensions[1] if dimensions else None,
    }


def _export_article(item: NewsItem, content_dir: Path, archived: bool) -> dict:
    """One placed article, with everything ExtendScript needs to lay it out."""
    heading, body, word_count = _scraped_content(content_dir, item)
    return {
        "id": item.id,
        "title": item.title,
        "heading": heading,
        "body": body,
        "word_count": word_count,
        "source": item.source,
        "url": item.url,
        "published_at": item.published_at,
        "cover": _export_cover(item.cover_file, archived),
    }


@router.get("/api/publication/export")
async def export_layout(
    day: str | None = Query(
        None, description="'previous' to export the archived workspace"
    ),
):
    """Build the layout JSON for the ExtendScript: settings, pages, articles.

    Pages come out in sidebar order with their articles in workspace
    order (newest id first), each carrying its heading, body, word count
    and cover (URL, absolute file path, pixel dimensions). Articles that
    aren't placed are counted but not exported — the layout is what the
    script lays out.
    """
    archived = day == "previous"
    items = await get_publication_items() if archived else await get_items()
    content_dir = PUBLICATION_ARTICLES_DIR if archived else ARTICLES_DIR
    layout = await get_publication_layout()
    page_words = layout["page_words"]
    total_pages = layout["total_pages"]

    grouped: dict[int, list[NewsItem]] = {
        page: [] for page in range(1, total_pages + 1)
    }
    unassigned_count = 0
    for item in items:
        if item.page_number is None:
            unassigned_count += 1
            continue
        grouped[min(max(item.page_number, 1), total_pages)].append(item)

    pages = []
    for page, page_items in grouped.items():
        articles = [
            _export_article(item, content_dir, archived) for item in page_items
        ]
        words = sum(article["word_count"] or 0 for article in articles)
        pages.append(
            {
                "page": page,
                "word_count": words,
                "fill_percent": round(words / page_words * 100, 1),
                "articles": articles,
            }
        )

    return {
        "version": 1,
        "generated_at": now_iso(),
        "day": "previous" if archived else "today",
        "settings": {
            **layout,
            "lower_limit": page_words - layout["word_allowance"],
            "upper_limit": page_words + layout["word_allowance"],
        },
        "unassigned_count": unassigned_count,
        "pages": pages,
    }
