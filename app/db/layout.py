"""Data access for the print-layout settings and page groupings.

Publication-specific on purpose — the three knobs that describe the
paper (how many pages it has, roughly how many words fit on one page,
and how much slack that estimate allows) belong to the InDesign tooling
on ``/publication``, not to the app-wide settings. They live here
instead of growing ``app.db.settings`` and the generic settings
endpoint, but they are still plain rows in the same ``settings`` table,
seeded by ``init_db`` like every other setting.

This module also owns the ``page_number`` placement helpers (the column
both article tables share), so everything the sidebar does to group
articles has one home.
"""

from __future__ import annotations

from sqlalchemy import select, update

from app.db.engine import SessionLocal, run_in_thread
from app.db.models import Article, PublicationArticle, Setting

# Layout knobs, stored in the settings table and seeded on startup:
# total_pages — how big the newspaper is (the sidebar renders that many
#     pages to place articles on),
# page_words — rough word-count estimate of one full page (headings and
#     images resize, so it is a target, not a hard limit),
# word_allowance — how many words a page may still take over/under that
#     target before it counts as too tight/too loose.
PUBLICATION_LAYOUT_DEFAULTS = {
    "total_pages": "8",
    "page_words": "300",
    "word_allowance": "100",
}

# Inclusive bounds the settings endpoint enforces (same keys as the
# defaults). The allowance may not exceed the quota itself: the lower
# limit (page_words - word_allowance) has to stay a usable number.
PUBLICATION_LAYOUT_BOUNDS = {
    "total_pages": (1, 200),
    "page_words": (1, 100_000),
    "word_allowance": (0, 100_000),
}


def _validated(
    total_pages: int, page_words: int, word_allowance: int
) -> tuple[int, int, int]:
    """Coerce the three layout values to ints inside their bounds.

    Raises ValueError with a message fit for the API (the route turns it
    into a 400) when a value is not a whole number, out of range, or an
    allowance bigger than the quota itself.
    """
    try:
        total_pages, page_words, word_allowance = (
            int(total_pages),
            int(page_words),
            int(word_allowance),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "total_pages, page_words and word_allowance must be whole numbers"
        ) from exc
    for name, value in (
        ("total_pages", total_pages),
        ("page_words", page_words),
        ("word_allowance", word_allowance),
    ):
        low, high = PUBLICATION_LAYOUT_BOUNDS[name]
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
    if word_allowance > page_words:
        raise ValueError(
            "word_allowance can't be bigger than page_words — the lower "
            "limit would drop below zero"
        )
    return total_pages, page_words, word_allowance


def _get_layout_sync() -> dict[str, int]:
    """The three layout settings, clamped into their bounds."""
    with SessionLocal() as session:
        stored = {
            key: value
            for key, value in session.execute(
                select(Setting.key, Setting.value).where(
                    Setting.key.in_(tuple(PUBLICATION_LAYOUT_DEFAULTS))
                )
            )
        }
    layout: dict[str, int] = {}
    for key, default in PUBLICATION_LAYOUT_DEFAULTS.items():
        try:
            value = int(stored.get(key) or default)
        except (TypeError, ValueError):
            value = int(default)
        low, high = PUBLICATION_LAYOUT_BOUNDS[key]
        layout[key] = min(high, max(low, value))
    # A hand-edited row can't push the lower limit below zero.
    layout["word_allowance"] = min(layout["word_allowance"], layout["page_words"])
    return layout


async def get_publication_layout() -> dict[str, int]:
    """Return total_pages / page_words / word_allowance as ints."""
    return await run_in_thread(_get_layout_sync)


def _set_layout_sync(
    total_pages: int, page_words: int, word_allowance: int
) -> dict[str, int]:
    """Persist the layout settings; clear placements on vanished pages.

    Lowering total_pages orphans every article grouped onto a page that
    no longer exists — those are unplaced in the same transaction, so
    the articles simply return to the unassigned pool instead of
    disappearing from both the sidebar and the picker.
    """
    values = dict(
        zip(
            PUBLICATION_LAYOUT_DEFAULTS,
            _validated(total_pages, page_words, word_allowance),
            strict=True,
        )
    )
    with SessionLocal() as session:
        for key, value in values.items():
            setting = session.get(Setting, key)
            if setting is None:
                session.add(Setting(key=key, value=str(value)))
            else:
                setting.value = str(value)
        for model in (Article, PublicationArticle):
            session.execute(
                update(model)
                .where(model.page_number > values["total_pages"])
                .values(page_number=None)
            )
        session.commit()
    return values


async def set_publication_layout(
    total_pages: int, page_words: int, word_allowance: int
) -> dict[str, int]:
    """Save the layout settings (raises ValueError on invalid input)."""
    return await run_in_thread(
        _set_layout_sync, total_pages, page_words, word_allowance
    )


def _assign_sync(
    article_ids: list[int], page: int, archived: bool
) -> list[int]:
    """Place *article_ids* on *page*; return the ids that don't exist.

    All-or-nothing: a single unknown id leaves every placement untouched,
    so a stale picker list can't half-apply. Re-placing an article that
    already sits on a page simply moves it. Raises ValueError when the
    page number itself is outside the configured layout.
    """
    layout = _get_layout_sync()
    if not 1 <= page <= layout["total_pages"]:
        raise ValueError(
            f"page must be between 1 and {layout['total_pages']} "
            "(total_pages in the layout settings)"
        )
    ids = list(dict.fromkeys(article_ids))
    model = PublicationArticle if archived else Article
    with SessionLocal() as session:
        # The workspace holds at most a few hundred rows, so one small
        # query beats an IN() list that could outgrow SQLite's bound
        # parameters when a big selection is placed at once.
        found = set(session.scalars(select(model.id)))
        missing = [article_id for article_id in ids if article_id not in found]
        if missing:
            return missing  # nothing written: the batch is all-or-nothing
        for article_id in ids:
            article = session.get(model, article_id)
            article.page_number = page
            # A batch place appends in workspace order; a stale position
            # from an earlier drag layout would otherwise reorder it.
            article.page_position = None
        session.commit()
    return []


async def assign_articles_to_page(
    article_ids: list[int], page: int, *, archived: bool = False
) -> list[int]:
    """Group articles onto a page. Returns the ids not found (none = ok)."""
    return await run_in_thread(_assign_sync, article_ids, page, archived)


def _save_page_layout_sync(
    pages: list[tuple[int, list[int]]], archived: bool
) -> list[int]:
    """Replace a workspace's whole page layout with *pages*.

    Each ``(page, article_ids)`` pair places those articles on that page in
    the given order — the sidebar's drag-and-drop save sends every page, so
    articles missing from *pages* return to the unassigned pool. All-or-
    nothing: an unknown article id, a duplicate id, or a page outside the
    configured layout leaves every placement untouched. Returns the ids
    that don't exist (empty means saved).
    """
    layout = _get_layout_sync()
    for page, _ in pages:
        if not 1 <= page <= layout["total_pages"]:
            raise ValueError(
                f"page must be between 1 and {layout['total_pages']} "
                "(total_pages in the layout settings)"
            )
    ids = [article_id for _, page_ids in pages for article_id in page_ids]
    if len(ids) != len(set(ids)):
        raise ValueError("each article can only be placed on one page")
    model = PublicationArticle if archived else Article
    with SessionLocal() as session:
        # One small query instead of an IN() list that could outgrow
        # SQLite's bound parameters on a big layout.
        found = set(session.scalars(select(model.id)))
        missing = [article_id for article_id in ids if article_id not in found]
        if missing:
            return missing  # nothing written: the save is all-or-nothing
        session.execute(update(model).values(page_number=None, page_position=None))
        for page, page_ids in pages:
            for position, article_id in enumerate(page_ids):
                article = session.get(model, article_id)
                article.page_number = page
                article.page_position = position
        session.commit()
    return []


async def save_page_layout(
    pages: list[tuple[int, list[int]]], *, archived: bool = False
) -> list[int]:
    """Save the dragged page assignments/order; returns the ids not found."""
    return await run_in_thread(_save_page_layout_sync, pages, archived)


def _clear_page_sync(article_id: int, archived: bool) -> bool:
    """Take an article off its page. False when the row doesn't exist."""
    model = PublicationArticle if archived else Article
    with SessionLocal() as session:
        article = session.get(model, article_id)
        if article is None:
            return False
        article.page_number = None
        article.page_position = None
        session.commit()
    return True


async def clear_article_page(article_id: int, *, archived: bool = False) -> bool:
    """Remove an article from its page, back into the unassigned pool."""
    return await run_in_thread(_clear_page_sync, article_id, archived)


def _clear_all_pages_sync(archived: bool) -> int:
    """Remove every article from its page. Returns the count cleared."""
    model = PublicationArticle if archived else Article
    with SessionLocal() as session:
        rows = session.execute(
            update(model)
            .where(model.page_number.isnot(None))
            .values(page_number=None, page_position=None)
        ).rowcount
        session.commit()
    return rows


async def clear_page_layout(archived: bool = False) -> int:
    """Remove every article from its page; returns the number cleared."""
    return await run_in_thread(_clear_all_pages_sync, archived)


# ---------------------------------------------------------------------------
# Layout generation history (AI-generated page assignments)
# ---------------------------------------------------------------------------

from app.db.models import LayoutGeneration


def _save_generation_sync(
    page_assignments: list[list[int]], day: str
) -> int:
    """Persist a new layout generation; returns its id."""
    with SessionLocal() as session:
        gen = LayoutGeneration(page_assignments=page_assignments, day=day)
        session.add(gen)
        session.commit()
        session.refresh(gen)
        return gen.id


async def save_layout_generation(
    page_assignments: list[list[int]], day: str
) -> int:
    """Save a new AI-generated layout; returns the generation id."""
    return await run_in_thread(_save_generation_sync, page_assignments, day)


def _get_generations_sync(day: str) -> list[LayoutGeneration]:
    """All generations for a workspace, newest first."""
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(LayoutGeneration)
                .where(LayoutGeneration.day == day)
                .order_by(LayoutGeneration.id.desc())
            ).all()
        )


async def get_layout_generations(day: str) -> list[LayoutGeneration]:
    """Return layout generations for a workspace (newest first)."""
    return await run_in_thread(_get_generations_sync, day)


def _get_generation_sync(gen_id: int) -> LayoutGeneration | None:
    with SessionLocal() as session:
        return session.get(LayoutGeneration, gen_id)


async def get_layout_generation(gen_id: int) -> LayoutGeneration | None:
    """Return a single generation by id, or None."""
    return await run_in_thread(_get_generation_sync, gen_id)


def _apply_generation_sync(gen_id: int, day: str) -> bool:
    """Apply a saved generation's page assignments to the article table.

    Returns False when the generation doesn't exist or belongs to a
    different workspace.
    """
    gen = _get_generation_sync(gen_id)
    if gen is None or gen.day != day:
        return False

    model = PublicationArticle if day == "previous" else Article
    with SessionLocal() as session:
        # Clear existing placements first
        session.execute(update(model).values(page_number=None, page_position=None))
        # Apply the generation's assignments, keeping each page's order
        for page_num, article_ids in enumerate(gen.page_assignments, start=1):
            for position, article_id in enumerate(article_ids):
                article = session.get(model, article_id)
                if article is not None:
                    article.page_number = page_num
                    article.page_position = position
        session.commit()
    return True


async def apply_layout_generation(gen_id: int, day: str) -> bool:
    """Apply a saved layout generation to the workspace.

    Returns False when the generation doesn't exist or belongs to a
    different workspace.
    """
    return await run_in_thread(_apply_generation_sync, gen_id, day)


def _delete_generation_sync(gen_id: int) -> bool:
    """Delete a generation by id. Returns False when not found."""
    with SessionLocal() as session:
        gen = session.get(LayoutGeneration, gen_id)
        if gen is None:
            return False
        session.delete(gen)
        session.commit()
    return True


async def delete_layout_generation(gen_id: int) -> bool:
    """Delete a layout generation. Returns False when not found."""
    return await run_in_thread(_delete_generation_sync, gen_id)
